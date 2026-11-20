"""#53: chain heads in the community room and the daily ledger digest (app/anchors.py)."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from tests.conftest import SERVER, VOTING, sign_in

from app import anchors, categories, chain_verify, cli, decision_log, governance, ledger, members
from app.models import ConfigKV, DecisionLog, EventFeed
from app.stream import ROOM_KEY

TZ = ZoneInfo("Europe/Stockholm")
ROOM = f"!village:{SERVER}"
NORA = f"@nora:{SERVER}"


@pytest.fixture
def people(db, log_signer):
    db.add(ConfigKV(key="voting", value_json=VOTING))
    p = {
        "ada": members.add_member(db, f"@ada:{SERVER}", "Ada", ("admin",), signer=log_signer),
        "nora": members.add_member(db, NORA, "Nora", signer=log_signer),
    }
    db.commit()
    return p


@pytest.fixture
def room(db):
    db.add(ConfigKV(key=ROOM_KEY, value_json=ROOM))
    db.commit()


@pytest.fixture
def water(db):
    c = categories.create(
        db, categories.CategorySpec(slug="water", name="Water", unit="l", kind="stock")
    )
    db.commit()
    return c


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A time on 2026-11-<day> in community time."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def log_entry(db, member, category, amount, when):
    entry = ledger.add_entry(
        db, member, category, "reading", amount, channel="matrix", occurred_at=when, now=when
    )
    db.commit()
    return entry


def closed_vote(db, signer, people):
    p = governance.open_proposal(
        db,
        people["nora"],
        "Buy a pump?",
        ("Yes", "No"),
        vote_type_slug="rules",
        signer=signer,
        channel="web",
    )
    governance.cast_vote(db, people["ada"], p.id, p.options[0].id, signer=signer, channel="web")
    governance.close_if_due(db, p.id, signer=signer, now=p.closes_at)
    db.commit()
    return p


def kinds(db) -> list[str]:
    return list(db.scalars(select(DecisionLog.kind).order_by(DecisionLog.seq)))


def last(db, kind) -> DecisionLog:
    return db.scalar(
        select(DecisionLog).where(DecisionLog.kind == kind).order_by(DecisionLog.seq.desc())
    )


# --- after a vote closes -------------------------------------------------------------


def test_a_closed_vote_is_anchored_in_the_room(db, log_signer, people, room):
    closed_vote(db, log_signer, people)
    assert kinds(db)[-2:] == ["proposal.closed", "log.anchored"]
    closed, anchored = last(db, "proposal.closed"), last(db, "log.anchored")
    assert anchored.payload_json["anchored_seq"] == closed.seq
    assert anchored.payload_json["anchored_hash"] == closed.entry_hash
    assert anchored.payload_json["reason"] == "proposal.closed"
    assert anchored.actor_member_id is None
    line = db.scalar(select(EventFeed).where(EventFeed.kind == "log.anchored"))
    assert f"{closed.seq}:{closed.entry_hash}" in line.summary
    # Members can check the log against what they were sent.
    assert decision_log.verify_db(db, [(closed.seq, closed.entry_hash)]).ok


def test_no_room_no_anchor(db, log_signer, people):
    closed_vote(db, log_signer, people)
    assert "log.anchored" not in kinds(db)
    assert db.scalar(select(EventFeed).where(EventFeed.kind == "log.anchored")) is None


# --- daily ----------------------------------------------------------------------------


def run_daily(db, signer, now):
    done = anchors.run_daily(db, signer, TZ, now)
    db.commit()
    return done


def test_nothing_happens_before_the_quiet_hours_end(db, log_signer, people, room, water):
    log_entry(db, people["nora"], water, 800, local(17, 18))
    assert run_daily(db, log_signer, local(18, 7, 59)) == {"digests": 0, "anchors": 0}


def test_daily_digest_then_anchor_once_a_day(db, log_signer, people, room, water):
    log_entry(db, people["nora"], water, 800, local(16, 18))
    log_entry(db, people["ada"], water, 700, local(17, 9))
    log_entry(db, people["ada"], water, 650, local(17, 20))
    log_entry(db, people["nora"], water, 600, local(18, 7))  # today: tomorrow's digest
    assert run_daily(db, log_signer, local(18, 8, 0)) == {"digests": 2, "anchors": 1}
    digests = db.scalars(
        select(DecisionLog).where(DecisionLog.kind == "ledger.digest").order_by(DecisionLog.seq)
    ).all()
    assert [(d.payload_json["day"], d.payload_json["entries"]) for d in digests] == [
        ("2026-11-16", 1),
        ("2026-11-17", 2),
    ]
    anchored = last(db, "log.anchored")
    assert anchored.payload_json["anchored_seq"] == digests[-1].seq
    assert anchored.payload_json["reason"] == "daily"
    assert anchored.payload_json["day"] == "2026-11-18"
    # Later the same day: nothing new.
    assert run_daily(db, log_signer, local(18, 15)) == {"digests": 0, "anchors": 0}
    # The next morning: one more digest (with today's early entry) and one anchor.
    assert run_daily(db, log_signer, local(19, 9)) == {"digests": 1, "anchors": 1}
    assert last(db, "ledger.digest").payload_json["entries"] == 1
    assert decision_log.verify_db(db).ok


def test_a_quiet_day_still_gets_a_digest(db, log_signer, people, water):
    log_entry(db, people["nora"], water, 800, local(16, 18))
    run_daily(db, log_signer, local(19, 8))  # the box was off: days are caught up
    days = [
        (r.payload_json["day"], r.payload_json["entries"], r.payload_json["sha256"])
        for r in db.scalars(select(DecisionLog).where(DecisionLog.kind == "ledger.digest"))
    ]
    empty = hashlib.sha256(b"[]").hexdigest()
    assert [(d, n) for d, n, _ in days] == [("2026-11-16", 1), ("2026-11-17", 0), ("2026-11-18", 0)]
    assert days[1][2] == empty
    assert "log.anchored" not in kinds(db)  # no room: digests only


def test_the_digest_is_deterministic_and_catches_a_changed_entry(db, log_signer, people, water):
    entry = log_entry(db, people["nora"], water, 800, local(17, 18))
    log_entry(db, people["ada"], water, 750, local(17, 19))
    run_daily(db, log_signer, local(18, 9))
    logged = last(db, "ledger.digest").payload_json
    assert anchors.digest_payload(db, local(17, 0).date(), TZ) == logged
    assert anchors.digest_payload(db, local(17, 0).date(), TZ) == logged  # again: the same
    db.execute(text("ALTER TABLE resource_entry DISABLE TRIGGER USER"))
    db.execute(text("UPDATE resource_entry SET amount = 900 WHERE id = :id"), {"id": entry.id})
    db.execute(text("ALTER TABLE resource_entry ENABLE TRIGGER USER"))
    db.commit()
    db.expire_all()
    assert anchors.digest_payload(db, local(17, 0).date(), TZ)["sha256"] != logged["sha256"]


def test_timestamps_in_the_digest_are_utc(db, log_signer, people, water):
    entry = log_entry(db, people["nora"], water, 800, local(17, 18))
    record = anchors.entry_record(entry)
    assert record["created_at"] == local(17, 18).astimezone(UTC).isoformat()
    assert set(record) == set(anchors.DIGEST_FIELDS)


# --- what an anchor protects against --------------------------------------------------


def test_a_cut_or_rewritten_log_fails_against_a_posted_anchor(db, log_signer, people, room):
    closed_vote(db, log_signer, people)
    p = last(db, "log.anchored").payload_json
    posted = (p["anchored_seq"], p["anchored_hash"])
    entries, keys = decision_log.load_entries(db), decision_log.public_keys(db)
    assert chain_verify.verify_chain(entries, keys, [posted]).ok
    # Cut back to before the vote closed: fine on its own, not against the anchor.
    cut = entries[: posted[0] - 1]
    assert chain_verify.verify_chain(cut, keys).ok
    assert not chain_verify.verify_chain(cut, keys, [posted]).ok
    # Rewritten and re-signed with the box's own key from that entry on: caught.
    forged = copy.deepcopy(entries)
    forged[posted[0] - 1]["payload"]["question"] = "Buy two pumps?"
    prev = forged[posted[0] - 2]["entry_hash"]
    for e in forged[posted[0] - 1 :]:
        e["prev_hash"] = prev
        e["entry_hash"] = chain_verify.entry_hash(chain_verify.entry_body(e))
        e["signature"] = log_signer.sign(e["entry_hash"])
        prev = e["entry_hash"]
    assert chain_verify.verify_chain(forged, keys, replay=False).ok
    result = chain_verify.verify_chain(forged, keys, [posted], replay=False)
    assert not result.ok
    assert "anchor" in result.reason


# --- command line and page ------------------------------------------------------------


def run_cli(app, *argv: str) -> int:
    return cli.main(
        list(argv),
        sessionmaker=app.state.sessionmaker,
        server_name=SERVER,
        key_dir=app.state.settings.key_dir,
    )


def test_anchor_log_command(app, db, log_signer, people, capsys):
    assert run_cli(app, "anchor-log") == 1
    assert "no community room" in capsys.readouterr().err
    db.add(ConfigKV(key=ROOM_KEY, value_json=ROOM))
    db.commit()
    head = decision_log.load_entries(db)[-1]
    assert run_cli(app, "anchor-log") == 0
    assert f"{head['seq']}:{head['entry_hash']}" in capsys.readouterr().out
    db.expire_all()
    assert last(db, "log.anchored").payload_json["reason"] == "manual"


def test_the_log_page_shows_the_last_checkpoint(client, fake_matrix, db, log_signer, people, room):
    closed_vote(db, log_signer, people)
    p = last(db, "log.anchored").payload_json
    fake_matrix.user_id = NORA
    assert sign_in(client, fake_matrix).status_code == 303
    page = client.get("/log").text
    assert "codes posted in the chat would stop matching" in page
    assert f"--anchor {p['anchored_seq']}:{p['anchored_hash']}" in page


def test_late_runs_are_bounded(db, log_signer, people, water):
    log_entry(db, people["nora"], water, 800, local(17, 18))
    far = local(17, 9) + timedelta(days=anchors.MAX_CATCH_UP_DAYS + 30)
    done = run_daily(db, log_signer, far)
    assert done["digests"] == anchors.MAX_CATCH_UP_DAYS + 1
