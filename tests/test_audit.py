"""The decision log page (/log): readable by every member, verified on every load."""

from __future__ import annotations

import re
from decimal import Decimal
from html import unescape

import pytest
from sqlalchemy import text
from tests.conftest import SERVER, sign_in

from app import audit, chain_verify, decision_log, members

ADMIN = f"@ada:{SERVER}"
NORA = f"@nora:{SERVER}"


@pytest.fixture
def ada(db, log_signer):
    m = members.add_member(db, ADMIN, "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


def as_member(client, fake_matrix, user_id: str = NORA):
    fake_matrix.user_id = user_id
    assert sign_in(client, fake_matrix).status_code == 303
    return client


def opened(db, signer, n: int = 1, actor: int | None = None, question: str = "Buy a pump?"):
    """A complete `proposal.opened` entry: the replay checks the vote against it."""
    payload = {
        "proposal_id": n,
        "question": question,
        "options": [
            {"option_id": 2 * n + 1, "position": 1, "label": "Yes"},
            {"option_id": 2 * n + 2, "position": 2, "label": "No"},
        ],
        "quorum_pct": None,
        "threshold_pct": 50,
    }
    return decision_log.append(db, signer, "proposal.opened", payload, actor)


def voted(db, signer, member, n: int = 1, kind: str = "vote.cast"):
    payload = {"proposal_id": n, "question": "Buy a pump?", "member_id": member.id}
    return decision_log.append(db, signer, kind, payload, member.id)


def closed(db, signer, yes: int, no: int, abstained: int, eligible: int, n: int = 1):
    """A `proposal.closed` entry whose outcome follows from the counts, as the replay demands."""
    counts = {2 * n + 1: yes, 2 * n + 2: no}
    result = chain_verify.tally(counts, eligible, None, Decimal(50), abstained)
    labels = {2 * n + 1: "Yes", 2 * n + 2: "No"}
    payload = {
        "proposal_id": n,
        "question": "Buy a pump?",
        "trigger": "window_ended",
        "tally": [{"option_id": o, "label": labels[o], "votes": v} for o, v in counts.items()],
        "cast": result.cast,
        "abstained": abstained,
        "turnout": result.turnout,
        "eligible": eligible,
        "quorum_pct": None,
        "threshold_pct": 50,
        "quorum_met": result.quorum_met,
        "outcome": result.outcome,
        "result": "passed" if result.decided else "rejected",
        "outcome_option_id": result.option_id,
        "outcome_label": labels.get(result.option_id),
    }
    return decision_log.append(db, signer, "proposal.closed", payload)


def test_member_reads_the_votes_in_the_log(client, fake_matrix, db, log_signer, ada, nora):
    opened(db, log_signer, actor=ada.id)
    voted(db, log_signer, ada)
    voted(db, log_signer, nora)
    closed(db, log_signer, yes=1, no=0, abstained=1, eligible=2)
    db.commit()
    page = as_member(client, fake_matrix).get("/log")
    assert page.status_code == 200
    body = unescape(page.text)
    assert "Chain verified: all 5 entries" in body
    assert "Opened the vote “Buy a pump?” (#1)" in body
    assert "Closed “Buy a pump?”: passed with “Yes”, 1 votes cast, 1 abstained" in body
    assert re.search(r'data-label="Who"[^>]*>Ada<', body)  # the proposer
    assert re.search(r'data-label="Who"[^>]*>Commons<', body)  # closing follows the rules
    assert "Ada · " in body  # the phone line: who · when · #seq
    assert log_signer.key_id in body
    assert log_signer.public_key_hex in body
    entry = decision_log.load_entries(db)[-1]
    assert entry["entry_hash"] in body
    assert entry["signature"] in body
    assert 'href="/log"' in body  # in the main nav for everyone


def test_only_vote_milestones_are_listed(client, fake_matrix, db, log_signer, ada, nora):
    members.grant_role(db, nora, "board", ada.id, signer=log_signer, channel="web")  # 2
    opened(db, log_signer, actor=ada.id)  # 3
    voted(db, log_signer, nora)  # 4
    voted(db, log_signer, nora, kind="vote.changed")  # 5
    decision_log.append(db, log_signer, "config.changed", {"key": "k", "new": 1})  # 6
    opened(db, log_signer, 2, actor=ada.id, question="Paint it?")  # 7
    closed(db, log_signer, yes=0, no=0, abstained=1, eligible=2)  # 8
    decision_log.append(  # 9
        db,
        log_signer,
        "proposal.cancelled",
        {"proposal_id": 2, "question": "Paint it?", "reason": "Duplicate"},
        nora.id,
    )
    db.commit()
    body = unescape(as_member(client, fake_matrix).get("/log").text)
    assert re.findall(r'<tr id="entry-(\d+)"', body) == ["9", "8", "7", "3"]
    assert "Closed “Buy a pump?”: rejected, 0 votes cast, 1 abstained" in body
    assert "Cancelled “Paint it?”: Duplicate" in body
    for hidden in ("role.granted", "vote.cast", "vote.changed", "config.changed", "Voted on"):
        assert hidden not in body
    assert "Chain verified: all 9 entries" in body  # hidden entries are still verified


def test_newest_first(client, fake_matrix, db, log_signer, nora):
    opened(db, log_signer, 1)
    opened(db, log_signer, 2)
    db.commit()
    body = as_member(client, fake_matrix).get("/log").text
    assert body.index('id="entry-2"') < body.index('id="entry-1"')


def test_empty_log(client, fake_matrix, db, nora):
    body = as_member(client, fake_matrix).get("/log").text
    assert "The log is empty so far." in body
    assert "No votes have been opened yet." in body


def test_broken_chain_is_shown(client, fake_matrix, db, log_signer, ada, nora):
    members.grant_role(db, nora, "board", ada.id, signer=log_signer, channel="web")
    db.commit()
    db.execute(text("ALTER TABLE decision_log DISABLE TRIGGER USER"))
    db.execute(
        text(
            "UPDATE decision_log SET payload_json = jsonb_set(payload_json, '{role}', '\"admin\"')"
            " WHERE seq = 2"
        )
    )
    db.execute(text("ALTER TABLE decision_log ENABLE TRIGGER USER"))
    db.commit()
    body = as_member(client, fake_matrix).get("/log").text
    assert "Chain verified" not in body
    assert "The chain does not verify at entry 2" in body
    assert 'role="alert"' in body


def test_pages_of_entries(client, fake_matrix, db, log_signer, nora):
    for i in range(audit.PAGE_SIZE + 5):
        opened(db, log_signer, i)
    db.commit()
    as_member(client, fake_matrix)
    first = client.get("/log").text
    assert len(re.findall(r'<tr id="entry-', first)) == audit.PAGE_SIZE
    assert 'id="entry-55"' in first
    assert 'href="/log?before=6"' in first
    second = client.get("/log?before=6").text
    assert re.findall(r'<tr id="entry-(\d+)"', second) == ["5", "4", "3", "2", "1"]
    assert "Older entries" not in second
    assert "Back to the newest" in second
    assert client.get("/log?before=0").status_code == 400


def test_payload_text_is_escaped(client, fake_matrix, db, log_signer, nora):
    opened(db, log_signer, question="<script>x</script>")
    db.commit()
    body = as_member(client, fake_matrix).get("/log").text
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt;" in body


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        ("member.invited", {"invite_id": 3}, "Created an invite to join"),
        (
            "role.revoked",
            {"matrix_user_id": NORA, "role": "board"},
            "Took the board role from Nora",
        ),
        ("member.deactivated", {"matrix_user_id": NORA}, "Deactivated Nora"),
        # Not known (any more): the short name from the Matrix id.
        ("member.deactivated", {"matrix_user_id": f"@bob:{SERVER}"}, "Deactivated bob"),
        ("role.granted", {"role": "board"}, "role.granted"),  # field missing: the kind
        ("vote.cast", {}, "vote.cast"),  # no line yet: the kind
        ("config.changed", {"key": "{0.__class__}"}, "Changed the setting {0.__class__}"),
        (
            "config.changed",
            {"key": "resource_categories.water", "old": None, "new": {"name": "Water"}},
            "Added the resource Water",
        ),
        (
            "config.changed",
            {
                "key": "resource_categories.water",
                "old": {"name": "Water", "archived": False, "shortage_threshold": 900},
                "new": {"name": "Water", "archived": False, "shortage_threshold": 1000},
            },
            "Changed the resource Water: warning level",
        ),
        (
            "config.changed",
            {
                "key": "bookable.saw",
                "old": {"name": "Saw", "archived": False},
                "new": {"name": "Saw", "archived": True},
            },
            "Archived Saw: it can't be booked any more",
        ),
        (
            "booking.cancelled_by_admin",
            {"booking_id": 4, "bookable": "saw", "member_id": 7, "reason": "broken"},
            "Cancelled Nora's booking of Saw: broken",
        ),
    ],
)
def test_summaries(kind, payload, expected):
    names = audit.Names(
        by_matrix_id={NORA: "Nora"}, by_member_id={7: "Nora"}, things={"saw": "Saw"}
    )
    assert audit.summarize(kind, payload, names) == expected
