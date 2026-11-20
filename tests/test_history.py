"""Usage history and CSV export for admins (app/history.py).

Covers: the history derives the same level as the chat (one shared
calculation), the level on the first day counts everything before it,
corrections show up as corrections, archived resources keep their history, the
chart, the period checks, and the CSV (columns, numbers, formula escaping).
Who may call the routes (admin only) is in tests/test_authz_matrix.py.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import SERVER, sign_in

from app import categories, history, ledger, members
from app.categories import CategorySpec as Spec
from app.models import ConfigKV, utcnow

TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture
def vienna(db):
    db.add(ConfigKV(key="community.timezone", value_json="Europe/Stockholm"))
    db.commit()


@pytest.fixture
def firewood(db, vienna):
    c = categories.create(
        db,
        Spec(slug="firewood", name="Firewood", unit="m³", kind="stock", shortage_threshold=2),
    )
    db.commit()
    return c


@pytest.fixture
def gas(db, vienna):
    c = categories.create(db, Spec(slug="gas", name="Gas", unit="kg", kind="flow"))
    db.commit()
    return c


@pytest.fixture
def ada(db, log_signer):
    m = members.add_member(db, f"@ada:{SERVER}", "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


@pytest.fixture
def bob(db):
    m = members.add_member(db, f"@bob:{SERVER}", "Bob")
    db.commit()
    return m


@pytest.fixture
def admin(client, fake_matrix, ada):
    fake_matrix.user_id = ada.matrix_user_id
    assert sign_in(client, fake_matrix).status_code == 303
    return client


def days_ago(n: float):
    return utcnow() - timedelta(days=n)


def log(db, member, category, kind, amount, ago, note=None, channel="matrix"):
    when = days_ago(ago)
    e = ledger.add_entry(
        db, member, category, kind, amount, channel=channel, note=note, occurred_at=when, now=when
    )
    db.commit()
    return e


def last(n_days: int) -> history.Period:
    today = utcnow().astimezone(TZ).date()
    return history.Period(today - timedelta(days=n_days - 1), today, TZ)


@pytest.fixture
def logged(db, firewood, bob):
    """A month of firewood, logged in chat: 10 m³ read, then use and a refill."""
    return {
        "reading": log(db, bob, firewood, "reading", 10, 20),
        "use1": log(db, bob, firewood, "use", 1.5, 15),
        "add": log(db, bob, firewood, "add", 4, 10),
        "use2": log(db, bob, firewood, "use", 2, 3),
    }


# --- the numbers ---------------------------------------------------------------


def test_level_matches_the_live_level(db, firewood, logged):
    h = history.history(db, firewood, last(90))
    live = ledger.level(db, firewood).value
    assert live == 10.5
    assert h.level_at_end == live
    assert h.buckets[-1].level == live
    assert h.used == 3.5
    assert h.added == 4
    assert h.entries == 4
    assert h.corrections == 0
    assert [r.level_after for r in h.rows] == [10, 8.5, 12.5, 10.5]


def test_level_at_start_counts_what_came_before(db, firewood, logged):
    # The last 12 days: the reading and the first use are before the period.
    h = history.history(db, firewood, last(12))
    assert h.level_at_start == 8.5
    assert [r.entry.entry_type for r in h.rows] == ["add", "use"]
    assert h.level_at_end == ledger.level(db, firewood).value
    # Days before the first entry have no level; afterwards it carries forward.
    full = history.history(db, firewood, last(30))
    assert full.buckets[0].level is None
    assert full.buckets[-1].level == 10.5


def test_corrections_show_as_corrections(db, firewood, logged, ada, bob):
    ledger.correct_entry(db, bob, logged["use1"], 0.5, channel="matrix", note="typo")
    ledger.correct_entry(db, ada, logged["add"], 0, channel="web", void=True)
    db.commit()
    h = history.history(db, firewood, last(90))
    by_status = [(r.entry.entry_type, r.status, r.counted) for r in h.rows]
    assert by_status == [
        ("reading", "counted", 10),
        ("use", "corrected", 0.5),
        ("correction", "correction", None),
        ("add", "undone", 0),
        ("correction", "correction", None),
        ("use", "counted", 2),
    ]
    assert h.rows[2].entry.corrects_id == logged["use1"].id
    assert h.corrections == 2
    # The undone refill no longer counts, and the level still matches the chat.
    assert h.used == 2.5
    assert h.added == 0
    assert h.level_at_end == ledger.level(db, firewood).value == 7.5


def test_flow_resources_show_use_per_day(db, gas, bob):
    log(db, bob, gas, "use", 3, 2)
    log(db, bob, gas, "use", 2, 2)
    log(db, bob, gas, "add", 11, 1)
    h = history.history(db, gas, last(7))
    assert h.level_at_start is None
    assert h.level_at_end is None
    assert h.used == 5
    assert h.added == 11
    assert [b.used for b in h.buckets if b.used] == [5]
    assert len(h.chart["bars"]) == 1


def test_long_periods_are_drawn_per_week(db, firewood, logged):
    h = history.history(db, firewood, last(200))
    assert h.weekly
    assert all(b.start.weekday() == 0 for b in h.buckets[1:])
    assert h.buckets[-1].level == 10.5
    assert not history.history(db, firewood, last(90)).weekly


def test_chart_shows_threshold_and_levels_below_zero(db, firewood, bob):
    log(db, bob, firewood, "reading", 1, 5)
    log(db, bob, firewood, "use", 3, 4)  # uses outran the reading: -2
    ch = history.history(db, firewood, last(10)).chart
    assert ch["threshold"] is not None
    assert min(t["label"] for t in ch["ticks"]) <= -2
    assert all(ch["top"] <= p["y"] <= ch["bottom"] for p in ch["points"])
    assert re.fullmatch(r"[\d., ]+", ch["line"])


def test_empty_period(db, firewood):
    h = history.history(db, firewood, last(30))
    assert h.rows == []
    assert h.chart["empty"]


# --- the period ----------------------------------------------------------------


def test_period_defaults_to_90_days():
    today = utcnow().astimezone(TZ).date()
    p = history.period(None, None, TZ, today)
    assert (p.start, p.end, p.days) == (today - timedelta(days=89), today, 90)


@pytest.mark.parametrize(
    ("raw_from", "raw_to"),
    [("2026-11-10", "2026-11-01"), ("2025-01-01", "2026-11-01"), ("10.11.26", None)],
)
def test_bad_periods_are_refused(raw_from, raw_to):
    with pytest.raises(history.PeriodError):
        history.period(raw_from, raw_to, TZ, utcnow().date())


# --- the pages -----------------------------------------------------------------


def test_history_page(admin, firewood, logged):
    r = admin.get(f"/admin/resources/{firewood.id}/history")
    assert r.status_code == 200
    assert "Firewood: history" in r.text
    assert "<svg" in r.text
    assert 'class="level"' in r.text
    assert f"/admin/resources/{firewood.id}/history.csv?from=" in r.text
    assert "Bob" in r.text


def test_resource_list_links_to_history(admin, firewood):
    assert f"/admin/resources/{firewood.id}/history" in admin.get("/admin/resources").text


def test_archived_resources_keep_their_history(admin, db, firewood, logged):
    categories.archive(db, firewood)
    db.commit()
    r = admin.get(f"/admin/resources/{firewood.id}/history")
    assert r.status_code == 200
    assert "archived" in r.text
    rows = list(
        csv.reader(
            io.StringIO(
                admin.get(f"/admin/resources/{firewood.id}/history.csv").content.decode("utf-8-sig")
            )
        )
    )
    assert len(rows) == 5


def test_bad_period_on_the_page_and_csv(admin, firewood):
    url = f"/admin/resources/{firewood.id}/history"
    r = admin.get(url, params={"from": "2026-11-10", "to": "2026-11-01"})
    assert r.status_code == 400
    assert "after the" in r.text
    assert admin.get(url + ".csv", params={"from": "nonsense"}).status_code == 400


def test_unknown_resource_is_404(admin, vienna):
    assert admin.get("/admin/resources/999/history").status_code == 404
    assert admin.get("/admin/resources/999/history.csv").status_code == 404


# --- the CSV -------------------------------------------------------------------


def _csv(response) -> list[dict]:
    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff")
    return list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"))))


def test_csv_matches_what_members_logged(admin, db, firewood, logged):
    """Done when: an admin downloads a firewood CSV for the last month and the
    numbers match what members logged in chat."""
    p = last(31)
    r = admin.get(
        f"/admin/resources/{firewood.id}/history.csv",
        params={"from": p.start.isoformat(), "to": p.end.isoformat()},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"] == (
        f'attachment; filename="firewood_{p.start.isoformat()}_{p.end.isoformat()}.csv"'
    )
    rows = _csv(r)
    assert tuple(rows[0]) == history.CSV_COLUMNS
    assert [(x["type"], x["amount"], x["counted_amount"], x["level_after"]) for x in rows] == [
        ("reading", "10", "10", "10"),
        ("use", "1.5", "1.5", "8.5"),
        ("add", "4", "4", "12.5"),
        ("use", "2", "2", "10.5"),
    ]
    assert {x["unit"] for x in rows} == {"m³"}
    assert {x["member"] for x in rows} == {"Bob"}
    assert {x["channel"] for x in rows} == {"matrix"}
    assert rows[1]["entry_id"] == str(logged["use1"].id)
    occurred = rows[1]["occurred_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0[12]:00", occurred)


def test_csv_lists_corrections(admin, db, firewood, logged, ada):
    ledger.correct_entry(db, ada, logged["use1"], 1, channel="web")
    ledger.correct_entry(db, ada, logged["add"], 0, channel="web", void=True)
    db.commit()
    rows = _csv(admin.get(f"/admin/resources/{firewood.id}/history.csv"))
    fixes = [x for x in rows if x["type"] == "correction"]
    assert [(x["corrects_id"], x["amount"], x["status"]) for x in fixes] == [
        (str(logged["use1"].id), "1", "correction"),
        (str(logged["add"].id), "", "undo"),
    ]
    use1 = next(x for x in rows if x["entry_id"] == str(logged["use1"].id))
    assert (use1["amount"], use1["counted_amount"], use1["status"]) == ("1.5", "1", "corrected")


def test_csv_neutralises_formulas(admin, db, firewood):
    evil = members.add_member(db, f"@evil:{SERVER}", "+cmd|' /C calc'!A0")
    db.commit()
    for i, note in enumerate(['=HYPERLINK("http://x")', "@SUM(1)", "-2+3", "+1", "fine"]):
        log(db, evil, firewood, "add", 1, 5 - i, note=note)
    rows = _csv(admin.get(f"/admin/resources/{firewood.id}/history.csv"))
    assert [x["note"] for x in rows] == [
        '\'=HYPERLINK("http://x")',
        "'@SUM(1)",
        "'-2+3",
        "'+1",
        "fine",
    ]
    assert {x["member"] for x in rows} == {"'+cmd|' /C calc'!A0"}
    # (Tabs and line breaks are covered by test_safe_text: the ledger strips them.)
    # Numbers stay numbers (a negative level must not be turned into text).
    assert [x["level_after"] for x in rows] == ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize(
    ("raw", "safe"),
    [("=1", "'=1"), ("+1", "'+1"), ("-1", "'-1"), ("@a", "'@a"), ("\r1", "'\r1"),
     ("a=1", "a=1"), ("", ""), (None, "")],
)  # fmt: skip
def test_safe_text(raw, safe):
    assert history.safe_text(raw) == safe
