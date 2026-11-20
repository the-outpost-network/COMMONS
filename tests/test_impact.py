"""Contribution page, participation badges and the member's data export.

Who may call /me and /me/export.json is in tests/test_authz_matrix.py. Here:
the counters count taking part and never use, each badge is awarded once and
dated when it was earned, archived rules stop awarding, the page and export
show only the member's own data, the chat answers only about the sender, and
community.yaml rejects counters that aren't participation.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from tests.conftest import INTERNAL_TOKEN, SERVER, sign_in

from app import bookables, categories, community, impact, ledger, members
from app import calendar as cal
from app.assistant import parse_rules
from app.db import upgrade_database
from app.i18n import en
from app.models import BadgeRule, ConfigKV, MemberBadge, utcnow

H = timedelta(hours=1)
D = timedelta(days=1)


def rule(db, slug: str, counter: str, n: int, position: int = 0, name: str | None = None):
    r = BadgeRule(
        slug=slug,
        name=name or slug.title(),
        description="",
        rule_json={"count": counter, "min": n},
        position=position,
    )
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def world(db):
    db.add(ConfigKV(key="community.timezone", value_json="Europe/Stockholm"))
    water = categories.create(
        db, categories.CategorySpec(slug="water", name="Water", unit="l", kind="stock")
    )
    wood = categories.create(
        db, categories.CategorySpec(slug="firewood", name="Firewood", unit="m³", kind="stock")
    )
    for name, type_ in (("Theatre", "space"), ("Saw", "tool")):
        bookables.create(db, bookables.ThingSpec(name=name, type=type_))
    db.commit()
    things = {t.slug: t for t in bookables.list_active(db)}
    return {"water": water, "wood": wood, **things}


def log(db, member, category, when: datetime, entry_type="reading", amount=500.0, channel="matrix"):
    return ledger.add_entry(
        db, member, category, entry_type, amount, channel=channel, occurred_at=when, now=when
    )


# --- counters --------------------------------------------------------------------


def test_counters_match_the_counters_community_yaml_accepts():
    assert set(get_args(community.BadgeCounter)) == set(impact.COUNTERS)


@pytest.mark.parametrize("counter", ["resource_use", "litres_used", "amount", "uses"])
def test_community_yaml_refuses_a_counter_that_is_not_participation(counter):
    text_ = (
        "resource_categories:\n  - {slug: water, name: Water, unit: l, kind: stock}\n"
        f"badges:\n  - {{slug: big, name: Big, rule: {{count: {counter}, min: 1}}}}\n"
    )
    with pytest.raises(community.CommunityConfigError, match=r"badges\[0\] \(big\)\.rule\.count"):
        community.parse(text_)


def test_entries_count_logging_not_amount_and_not_undone_or_sensor(db, world, nora):
    start = utcnow() - 5 * D
    log(db, nora, world["water"], start, "use", 9000)  # a big use counts as one entry, like any
    log(db, nora, world["wood"], start + H, "add", 1)
    undone = log(db, nora, world["water"], start + 2 * H)
    ledger.correct_entry(db, nora, undone, 0, channel="matrix", void=True, now=start + 3 * H)
    log(db, nora, world["water"], start + 4 * H, channel="sensor")
    corrected = log(db, nora, world["water"], start + 5 * H)
    ledger.correct_entry(db, nora, corrected, 400, channel="matrix", now=start + 6 * H)
    db.commit()
    counted = impact.counted_entries(db, nora)
    assert [e.amount for e in counted] == [9000, 1, 500]  # corrected entry still counts once


def test_streak_is_the_longest_run_of_days_in_vienna(db, world, nora):
    # 22:30 UTC on 1 Dec is 23:30 in Stockholm; 23:30 UTC is already 2 Dec there.
    base = datetime(2026, 12, 1, 22, 30, tzinfo=UTC)
    for t in (base, base + H, base + D + H, base + 3 * D):
        log(db, nora, world["water"], t)
    db.commit()
    moments = impact._streak_moments(db, nora, cal.timezone(db), utcnow())
    # Stockholm days: 1, 2, 3 Dec in a row (the 2nd-day entry is the 23:30 UTC one), then 4 Dec.
    assert moments == [base, base + H, base + D + H, base + 3 * D]


def test_streak_restarts_after_a_gap(db, world, nora):
    base = datetime(2026, 11, 1, 9, tzinfo=UTC)
    for t in (base, base + D, base + 5 * D, base + 6 * D, base + 7 * D):
        log(db, nora, world["water"], t)
    db.commit()
    moments = impact._streak_moments(db, nora, cal.timezone(db), utcnow())
    assert moments == [base, base + D, base + 7 * D]  # best run reached 3 on the 3rd day in a row


def test_honoured_bookings(db, world, nora):
    now = utcnow()
    past = now - 3 * D
    space_done = cal.book(db, nora, world["theatre"], past, past + H, channel="web", now=past - H)
    space_now = cal.book(
        db, nora, world["theatre"], now - H, now + H, channel="web", now=now - 2 * H
    )
    on_time = cal.book(db, nora, world["saw"], past, past + 2 * H, channel="web", now=past - H)
    cal.return_item(db, nora, on_time, now=past + H)
    late = cal.book(db, nora, world["saw"], past + D, past + D + H, channel="web", now=past)
    cal.return_item(db, nora, late, now=past + D + 3 * H)
    kept = cal.book(
        db, nora, world["saw"], now - 5 * H, now - 4 * H, channel="web", now=now - 6 * H
    )
    cancelled = cal.book(db, nora, world["theatre"], now + D, now + D + H, channel="web", now=now)
    cal.cancel(db, nora, cancelled, now=now)
    db.commit()
    assert impact.honoured(space_done, now) == past + H
    assert impact.honoured(space_now, now) is None  # not over yet
    assert impact.honoured(on_time, now) == past + H
    assert impact.honoured(late, now) is None  # back after its end: no badge, no penalty
    assert impact.honoured(kept, now) is None  # still out, not given back
    assert impact.honoured(cancelled, now) is None


# --- awarding --------------------------------------------------------------------


def test_badge_is_awarded_once_dated_when_earned(db, world, nora):
    rule(db, "first-log", "ledger_entries", 1)
    rule(db, "three-logs", "ledger_entries", 3, position=1)
    t0 = utcnow() - 10 * D
    for i in range(3):
        log(db, nora, world["water"], t0 + i * D)
    db.commit()

    views = impact.sync(db, nora, now=utcnow())
    db.commit()
    assert [(v.rule.slug, v.earned_at) for v in views] == [
        ("first-log", t0),
        ("three-logs", t0 + 2 * D),
    ]
    impact.sync(db, nora)  # again: nothing new, nothing duplicated
    db.commit()
    rows = db.scalars(select(MemberBadge).order_by(MemberBadge.awarded_at)).all()
    assert [(r.badge_rule_id, r.awarded_at) for r in rows] == [
        (views[0].rule.id, t0),
        (views[1].rule.id, t0 + 2 * D),
    ]


def test_progress_until_earned(db, world, nora):
    rule(db, "ten-logs", "ledger_entries", 10)
    log(db, nora, world["water"], utcnow() - H)
    db.commit()
    (view,) = impact.sync(db, nora)
    assert (view.value, view.target, view.earned, view.counted) == (1, 10, False, True)


def test_votes_rule_is_shown_but_never_awarded(db, world, nora):
    rule(db, "voter", "votes", 1)
    log(db, nora, world["water"], utcnow() - H)
    db.commit()
    (view,) = impact.sync(db, nora)
    assert (view.counted, view.earned) == (False, False)
    assert db.scalar(select(func.count()).select_from(MemberBadge)) == 0


def test_earned_badge_stays_after_undo_and_archive(db, world, nora):
    first = rule(db, "first-log", "ledger_entries", 1)
    entry = log(db, nora, world["water"], utcnow() - 2 * H)
    db.commit()
    impact.sync(db, nora)
    ledger.correct_entry(db, nora, entry, 0, channel="matrix", void=True)
    first.archived_at = utcnow()
    db.commit()
    (view,) = impact.sync(db, nora)
    assert view.earned


def test_archived_rule_stops_awarding_and_disappears_if_not_earned(db, world, nora):
    rule(db, "first-log", "ledger_entries", 1).archived_at = utcnow()
    log(db, nora, world["water"], utcnow() - H)
    db.commit()
    assert impact.sync(db, nora) == []
    assert db.scalar(select(func.count()).select_from(MemberBadge)) == 0


def test_rule_with_unknown_counter_in_the_db_is_skipped(db, world, nora):
    db.add(BadgeRule(slug="odd", name="Odd", description="", rule_json={"count": "x"}, position=0))
    db.commit()
    assert impact.sync(db, nora) == []


def test_badges_are_per_member(db, world, nora, log_signer):
    rule(db, "first-log", "ledger_entries", 1)
    ana = members.add_member(db, f"@ana:{SERVER}", "Ana", signer=log_signer)
    log(db, ana, world["water"], utcnow() - H)
    db.commit()
    (mine,) = impact.sync(db, nora)
    (hers,) = impact.sync(db, ana)
    assert (mine.earned, hers.earned) == (False, True)


# --- seeding and migration ---------------------------------------------------------


def test_migration_moves_badges_from_config_into_the_table(pg_engine):
    with pg_engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE")
        c.exec_driver_sql("CREATE SCHEMA public")
    cfg = Config("alembic.ini")
    with pg_engine.connect() as c:
        cfg.attributes["connection"] = c
        command.upgrade(cfg, "0006_calendar_tokens")
    with pg_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO config_kv (key, value_json, updated_at) VALUES ('badges', :v, now())"
            ),
            {
                "v": json.dumps(
                    [
                        {
                            "slug": "first-log",
                            "name": "First log",
                            "description": "d",
                            "rule": {"count": "ledger_entries", "min": 1},
                        },
                        {
                            "slug": "voter",
                            "name": "Voter",
                            "description": "",
                            "rule": {"count": "votes", "min": 5},
                        },
                    ]
                )
            },
        )
    upgrade_database(pg_engine)
    with pg_engine.connect() as c:
        rows = c.execute(
            text("SELECT slug, rule_json, position FROM badge_rule ORDER BY position")
        ).all()
        assert [tuple(r) for r in rows] == [
            ("first-log", {"count": "ledger_entries", "min": 1}, 0),
            ("voter", {"count": "votes", "min": 5}, 1),
        ]
        assert c.execute(text("SELECT count(*) FROM config_kv WHERE key = 'badges'")).scalar() == 0


# --- page and export -------------------------------------------------------------


def _signed_in(app, db, fake_matrix, log_signer, name: str, roles=()) -> TestClient:
    client = TestClient(app, base_url="https://commons.example.test")
    client.member = members.add_member(
        db, f"@{name.lower()}:{SERVER}", name, roles, signer=log_signer
    )
    db.commit()
    fake_matrix.user_id = f"@{name.lower()}:{SERVER}"
    assert sign_in(client, fake_matrix).status_code == 303
    return client


def test_page_shows_my_badges_entries_and_bookings(app, client, db, world, fake_matrix, log_signer):
    rule(db, "first-log", "ledger_entries", 1, name="First log")
    rule(db, "regular", "ledger_entries", 5, position=1, name="Regular")
    rule(db, "voter", "votes", 5, position=2, name="Voter")
    db.commit()
    ana = _signed_in(app, db, fake_matrix, log_signer, "Ana")
    log(db, ana.member, world["water"], utcnow() - 2 * H, amount=812)
    cal.book(
        db,
        ana.member,
        world["theatre"],
        utcnow() - 3 * H,
        utcnow() - 2 * H,
        channel="web",
        note="rehearsal",
        now=utcnow() - 4 * H,
    )
    db.commit()

    page = ana.get("/me")
    assert page.status_code == 200
    html = page.text
    assert 'aria-current="page">My contributions' in html
    assert "First log" in html
    assert "Earned " in html
    assert "1 of 5" in html  # Regular, in progress
    assert "Not counted yet" in html  # Voter
    assert "Water: reading 812 l" in html
    assert "1 of the 1 water entries in the last 30 days were yours." in html
    assert "Theatre" in html
    assert "rehearsal" in html
    assert "1 bookings, 1 kept as agreed" in html
    assert db.scalar(select(func.count()).select_from(MemberBadge)) == 1


def test_page_never_shows_another_members_data(app, client, db, world, fake_matrix, log_signer):
    frida = members.add_member(db, f"@frida:{SERVER}", "Frida", signer=log_signer)
    log(db, frida, world["water"], utcnow() - H, amount=777)
    cal.book(
        db,
        frida,
        world["theatre"],
        utcnow() + D,
        utcnow() + D + H,
        channel="web",
        note="frida-secret-party",
    )
    db.commit()
    ana = _signed_in(app, db, fake_matrix, log_signer, "Ana", roles=("admin",))
    html = ana.get("/me").text
    # A standalone 777: asset hashes (?v=…) may contain the digits by chance.
    assert not FRIDA_AMOUNT.search(html)
    assert "frida-secret-party" not in html
    assert "Nothing logged yet" in html
    # There's no way to ask for someone else's page.
    assert not FRIDA_AMOUNT.search(ana.get(f"/me?member_id={frida.id}").text)
    assert ana.get(f"/me/{frida.id}").status_code == 404


FRIDA_AMOUNT = re.compile(r"\b777\b")


def _leaves(value):
    """Every scalar inside a JSON value."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v)
    else:
        yield value


def test_export_has_my_data_only(app, client, db, world, fake_matrix, log_signer):
    rule(db, "first-log", "ledger_entries", 1, name="First log")
    db.commit()
    frida = members.add_member(db, f"@frida:{SERVER}", "Frida", signer=log_signer)
    log(db, frida, world["water"], utcnow() - H, amount=777)
    ana = _signed_in(app, db, fake_matrix, log_signer, "Ana")
    log(db, ana.member, world["wood"], utcnow() - H, "add", 2)
    db.commit()
    ana.get("/me")  # earns the badge

    r = ana.get("/me/export.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["member"]["matrix_user_id"] == f"@ana:{SERVER}"
    assert [(e["resource"], e["amount"]) for e in data["resource_entries"]] == [("firewood", 2)]
    assert [b["slug"] for b in data["badges"]] == ["first-log"]
    # Frida's amount is nowhere: compare values, not text (a timestamp's
    # microseconds can contain "777").
    assert 777 not in list(_leaves(data))
    assert "frida" not in r.text.lower()


def test_page_has_no_inline_script(app, client, db, world, fake_matrix, log_signer):
    ana = _signed_in(app, db, fake_matrix, log_signer, "Ana")
    html = ana.get("/me").text
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<[^>]+\son[a-z]+=", html)  # no inline event handlers either


# --- chat --------------------------------------------------------------------------

AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
NORA = f"@nora:{SERVER}"


def say(client, text_: str) -> str:
    r = client.post(
        "/internal/assistant",
        json={"sender": NORA, "room_id": f"!dm:{SERVER}", "text": text_},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return r.json()["reply"]


@pytest.mark.parametrize(
    ("text_", "intent"),
    [
        ("my badges", "my_contributions"),
        ("Show me my contributions!", "my_contributions"),
        ("what have I contributed?", "my_contributions"),
        ("what badges do I have", "my_contributions"),
        ("my badges are shiny, log 2 m3 wood", None),
    ],
)
def test_rules_understand_contribution_questions(text_, intent):
    got = parse_rules(text_)
    assert (got.name if got else None) == intent


def test_chat_summary_is_about_the_sender(client, db, world, nora, fake_llm, log_signer):
    rule(db, "first-log", "ledger_entries", 1, name="First log")
    rule(db, "regular", "ledger_entries", 5, position=1, name="Regular")
    rule(db, "voter", "votes", 5, position=2, name="Voter")
    frida = members.add_member(db, f"@frida:{SERVER}", "Frida", signer=log_signer)
    for _ in range(3):
        log(db, frida, world["water"], utcnow() - H)
    log(db, nora, world["water"], utcnow() - 2 * H)
    log(db, nora, world["wood"], utcnow() - 2 * H)
    db.commit()
    text_ = say(client, "my badges")  # model down: the rules answer
    assert text_.startswith("You logged 2 entries (1 water, 1 firewood)")
    assert "First log ✓, Regular 2/5, Voter (not counted yet)" in text_
    assert text_.rstrip().endswith("/me")


def test_chat_refuses_someone_elses_contributions(client, db, world, nora, fake_llm):
    fake_llm.queue("my_contributions", about="someone_else")
    assert say(client, "what has Frida contributed?") == en.ASSISTANT_ONLY_YOURS
