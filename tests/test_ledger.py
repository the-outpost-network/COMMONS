"""Ledger: entry rules, derived levels, edits and corrections, guards, events, board."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from tests.conftest import SERVER, sign_in

from app import categories, events, ledger, members
from app.i18n import en
from app.models import ResourceEntry, utcnow

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
Spec = categories.CategorySpec


@pytest.fixture
def water(db):
    c = categories.create(
        db,
        Spec(
            slug="water",
            name="Water",
            unit="l",
            kind="stock",
            shortage_threshold=1000,
            capacity=3000,
        ),
    )
    db.commit()
    return c


@pytest.fixture
def gas(db):
    """A flow resource: only use and add."""
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


def log(db, member, category, entry_type, amount, minutes_ago=0, **kw):
    return ledger.add_entry(
        db,
        member,
        category,
        entry_type,
        amount,
        channel=kw.pop("channel", "matrix"),
        occurred_at=NOW - timedelta(minutes=minutes_ago),
        now=NOW,
        **kw,
    )


# -- levels ---------------------------------------------------------------------


def test_nothing_logged_no_level(db, water):
    lv = ledger.level(db, water, NOW)
    assert (lv.value, lv.from_zero, lv.below_threshold) == (None, False, False)


def test_without_a_reading_the_level_counts_from_zero(db, water, nora):
    log(db, nora, water, "add", 1500, minutes_ago=60)
    log(db, nora, water, "use", 200, minutes_ago=30)
    lv = ledger.level(db, water, NOW)
    assert (lv.value, lv.reading, lv.from_zero) == (1300, None, True)
    assert not lv.below_threshold
    log(db, nora, water, "use", 400, minutes_ago=10)
    assert ledger.level(db, water, NOW).below_threshold  # 900 < 1,000


def test_the_first_reading_replaces_counting_from_zero(db, water, nora):
    log(db, nora, water, "add", 1500, minutes_ago=60)
    log(db, nora, water, "reading", 2400, minutes_ago=30)
    log(db, nora, water, "use", 100, minutes_ago=10)
    lv = ledger.level(db, water, NOW)
    assert (lv.value, lv.from_zero) == (2300, False)


def test_undoing_the_only_reading_counts_from_zero_again(db, water, nora):
    log(db, nora, water, "add", 500, minutes_ago=60)
    r = log(db, nora, water, "reading", 2400, minutes_ago=30)
    ledger.correct_entry(db, nora, r, 0, channel="web", void=True, now=NOW)
    lv = ledger.level(db, water, NOW)
    assert (lv.value, lv.from_zero) == (500, True)


def test_level_is_the_latest_reading_plus_adds_minus_uses(db, water, nora):
    log(db, nora, water, "use", 999, minutes_ago=300)  # before the reading: ignored
    log(db, nora, water, "reading", 800, minutes_ago=240)
    log(db, nora, water, "use", 50, minutes_ago=180)
    log(db, nora, water, "add", 200, minutes_ago=120)
    lv = ledger.level(db, water, NOW)
    assert lv.value == 950
    assert lv.reading.amount == 800
    assert lv.below_threshold  # 950 l is under the 1,000 l threshold
    assert lv.fill == pytest.approx(950 / 3000)


def test_a_new_reading_starts_over_even_when_logged_late(db, water, nora):
    log(db, nora, water, "reading", 800, minutes_ago=240)
    log(db, nora, water, "use", 100, minutes_ago=60)
    # Logged now, but it happened two hours ago: the use after it still counts.
    log(db, nora, water, "reading", 500, minutes_ago=120)
    assert ledger.level(db, water, NOW).value == 400


def test_below_threshold(db, water, nora):
    log(db, nora, water, "reading", 900)
    assert ledger.level(db, water, NOW).below_threshold


def test_flow_resource_counts_use_of_the_last_week(db, gas, nora):
    with pytest.raises(ledger.LedgerError, match="takes use, add"):
        log(db, nora, gas, "reading", 5)
    log(db, nora, gas, "use", 3, minutes_ago=60 * 24 * 8)  # older than 7 days
    log(db, nora, gas, "use", 2, minutes_ago=60)
    log(db, nora, gas, "add", 11, minutes_ago=30)
    lv = ledger.level(db, gas, NOW)
    assert (lv.value, lv.used_recently) == (None, 2)


def test_levels_lists_active_resources_in_order(db, water, gas, nora):
    categories.archive(db, gas)
    log(db, nora, water, "reading", 800)
    assert [lv.category.slug for lv in ledger.levels(db, NOW)] == ["water"]


# -- rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry_type", "amount", "message"),
    [
        ("use", -1, "positive"),
        ("use", 0, "more than 0"),
        ("add", float("nan"), "positive"),
        ("reading", float("inf"), "positive"),
        ("reading", 6001, "more than Water can hold"),
        ("reading", "lots", "must be a number"),
        ("drink", 5, "takes reading, use, add"),
    ],
)
def test_amount_and_type_rules(db, water, nora, entry_type, amount, message):
    with pytest.raises(ledger.LedgerError, match=message):
        log(db, nora, water, entry_type, amount)
    assert db.query(ResourceEntry).count() == 0


def test_zero_reading_is_fine_and_bound_without_capacity(db, water, gas, nora):
    log(db, nora, water, "reading", 0)
    log(db, nora, gas, "use", 999_999)
    with pytest.raises(ledger.LedgerError):
        log(db, nora, gas, "use", 1_000_001)


@pytest.mark.parametrize(
    ("when", "ok"),
    [
        (NOW + timedelta(minutes=4), True),  # clock leeway
        (NOW + timedelta(minutes=6), False),
        (NOW - timedelta(days=29), True),
        (NOW - timedelta(days=31), False),
        (datetime(2026, 11, 10, 11, 0), False),  # noqa: DTZ001 - no timezone on purpose
    ],
)
def test_time_rules(db, water, nora, when, ok):
    def go():
        ledger.add_entry(db, nora, water, "reading", 1, channel="web", occurred_at=when, now=NOW)

    if ok:
        go()
    else:
        with pytest.raises(ledger.LedgerError):
            go()


def test_note_is_one_line_and_short(db, water, nora):
    e = log(db, nora, water, "reading", 1, note="  from   the\ngauge ")
    assert e.note == "from the gauge"
    with pytest.raises(ledger.LedgerError, match="at most 140"):
        log(db, nora, water, "reading", 1, note="x" * 141)


def test_refused_for_deactivated_members_archived_resources_unknown_channels(
    db, water, nora, log_signer
):
    with pytest.raises(ledger.LedgerError, match="unknown channel"):
        log(db, nora, water, "reading", 1, channel="email")
    categories.archive(db, water)
    with pytest.raises(ledger.LedgerError, match="archived"):
        log(db, nora, water, "reading", 1)
    categories.restore(db, water)
    members.deactivate(db, nora, signer=log_signer, channel="cli")
    with pytest.raises(ledger.NotAllowed):
        log(db, nora, water, "reading", 1)


def test_entry_is_attributed(db, water, nora):
    e = log(db, nora, water, "reading", 800, channel="web")
    assert (e.member_id, e.channel, e.category_id) == (nora.id, "web", water.id)


# -- edits and corrections ------------------------------------------------------


def test_author_edits_within_15_minutes(db, water, nora, bob):
    # Real time: the database checks the window against its own clock too.
    now = utcnow()
    e = ledger.add_entry(db, nora, water, "reading", 800, channel="web", now=now)
    later = now + timedelta(minutes=14)
    ledger.edit_entry(db, nora, e, amount=850, now=later)
    assert (e.amount, e.edited_at) == (850, later)
    with pytest.raises(ledger.NotAllowed, match="only the person"):
        ledger.edit_entry(db, bob, e, amount=1, now=now)
    with pytest.raises(ledger.NotAllowed, match="add a correction"):
        ledger.edit_entry(db, nora, e, amount=1, now=now + timedelta(minutes=15))


def test_admin_cannot_edit_but_can_correct(db, water, nora, bob, ada):
    e = log(db, nora, water, "reading", 800)
    with pytest.raises(ledger.NotAllowed):
        ledger.edit_entry(db, ada, e, amount=1, now=NOW)
    with pytest.raises(ledger.NotAllowed, match="or an admin"):
        ledger.correct_entry(db, bob, e, 700, channel="web", now=NOW)
    fix = ledger.correct_entry(db, ada, e, 700, channel="web", now=NOW + timedelta(hours=1))
    assert (fix.entry_type, fix.corrects_id, fix.member_id) == ("correction", e.id, ada.id)
    assert e.amount == 800  # the original stays as it was
    assert ledger.level(db, water, NOW + timedelta(hours=1)).value == 700


def test_latest_correction_wins_and_a_correction_of_a_correction_hits_the_original(db, water, nora):
    e = log(db, nora, water, "reading", 800)
    first = ledger.correct_entry(db, nora, e, 700, channel="matrix", now=NOW + timedelta(hours=1))
    second = ledger.correct_entry(
        db, nora, first, 650, channel="matrix", now=NOW + timedelta(hours=2)
    )
    assert second.corrects_id == e.id
    assert ledger.level(db, water, NOW).value == 650
    (c,) = ledger.counted_entries(db, water)
    assert (c.entry.id, c.amount, c.correction.id) == (e.id, 650, second.id)


def test_correction_follows_the_original_rules(db, water, nora):
    e = log(db, nora, water, "use", 50)
    with pytest.raises(ledger.LedgerError, match="more than Water can hold"):
        ledger.correct_entry(db, nora, e, 7000, channel="web", now=NOW)


def test_a_wrong_use_or_add_can_be_corrected_to_zero(db, water, nora):
    """A refill logged by mistake: correcting it to 0 means it didn't happen."""
    log(db, nora, water, "reading", 1434, minutes_ago=60)
    wrong = log(db, nora, water, "add", 2000, minutes_ago=30)
    assert ledger.level(db, water, NOW).value == 3434
    ledger.correct_entry(db, nora, wrong, 0, channel="web", now=NOW)
    assert ledger.level(db, water, NOW).value == 1434
    with pytest.raises(ledger.LedgerError, match="more than 0"):
        log(db, nora, water, "add", 0)  # a new entry still needs an amount


@pytest.mark.parametrize(
    ("who", "minutes_ago", "mode"),
    [("nora", 2, "edited"), ("nora", 20, "corrected"), ("ada", 2, "corrected"),
     ("ada", 20, "corrected"), ("bob", 2, None), ("bob", 20, None)],
)  # fmt: skip
def test_fix_entry_author_other_member_admin(db, water, nora, bob, ada, who, minutes_ago, mode):
    """Author inside the window edits; later, or an admin, adds a correction; others can't."""
    member = {"nora": nora, "bob": bob, "ada": ada}[who]
    now = utcnow()
    e = ledger.add_entry(
        db, nora, water, "use", 300, channel="matrix", now=now - timedelta(minutes=minutes_ago)
    )
    db.commit()
    if mode is None:
        with pytest.raises(ledger.NotAllowed):
            ledger.fix_entry(db, member, e, 200, channel="matrix", now=now)
        with pytest.raises(ledger.NotAllowed):
            ledger.fix_entry(db, member, e, None, channel="matrix", now=now)
        return
    done = ledger.fix_entry(db, member, e, 200, channel="matrix", now=now)
    db.commit()
    assert (done.mode, done.before, done.after) == (mode, 300, 200)
    assert ledger.effective_amount(db, e) == 200
    if mode == "edited":
        assert done.row is e
        assert e.amount == 200
    else:
        assert (done.row.corrects_id, done.row.member_id, e.amount) == (e.id, member.id, 300)
    undo = ledger.fix_entry(db, member, e, None, channel="matrix", now=now)
    assert (undo.mode, undo.before, undo.after, undo.row.void) == ("undone", 200, None, True)
    assert ledger.effective_amount(db, e) is None


def test_undo_drops_an_entry_from_the_level_and_a_later_correction_restores_it(db, water, nora):
    first = log(db, nora, water, "reading", 800, minutes_ago=90)
    wrong = log(db, nora, water, "reading", 2000, minutes_ago=60)
    used = log(db, nora, water, "use", 100, minutes_ago=30)
    assert ledger.level(db, water, NOW).value == 1900
    ledger.correct_entry(db, nora, wrong, 0, channel="web", void=True, now=NOW)
    assert ledger.level(db, water, NOW).value == 700  # back to the 800 reading
    assert ledger.level(db, water, NOW).reading.id == first.id
    by_id = {c.entry.id: c for c in ledger.counted_entries(db, water)}
    assert by_id[wrong.id].void
    assert by_id[wrong.id].amount == 0
    assert not by_id[used.id].void
    ledger.correct_entry(db, nora, wrong, 1500, channel="web", now=NOW + timedelta(minutes=1))
    assert ledger.level(db, water, NOW).value == 1400


def test_undo_of_a_flow_entry(db, gas, nora):
    e = log(db, nora, gas, "use", 5, minutes_ago=60)
    ledger.correct_entry(db, nora, e, 0, channel="web", void=True, now=NOW)
    assert ledger.level(db, gas, NOW).used_recently == 0


def test_fix_entry_refuses_no_change_and_double_undo(db, water, nora):
    e = log(db, nora, water, "use", 300, minutes_ago=60)
    with pytest.raises(ledger.LedgerError, match="already counts as 300"):
        ledger.fix_entry(db, nora, e, 300, channel="web", now=NOW)
    ledger.fix_entry(db, nora, e, None, channel="web", now=NOW)
    with pytest.raises(ledger.LedgerError, match="already undone"):
        ledger.fix_entry(db, nora, e, None, channel="web", now=NOW)


def test_fix_to_zero_inside_the_window_is_a_correction(db, water, nora):
    """An edit can't set a use to 0, so "it was 0" becomes a correction."""
    now = utcnow()
    e = ledger.add_entry(db, nora, water, "use", 300, channel="web", now=now)
    done = ledger.fix_entry(db, nora, e, 0, channel="web", now=now)
    assert (done.mode, e.amount, done.row.amount) == ("corrected", 300, 0)


def test_last_entry_is_the_members_newest_original(db, water, gas, nora, bob):
    a = log(db, nora, water, "use", 1, minutes_ago=30)
    log(db, bob, water, "use", 2, minutes_ago=20)
    g = log(db, nora, gas, "use", 3, minutes_ago=10)
    ledger.correct_entry(db, nora, a, 5, channel="web", now=NOW)
    assert ledger.last_entry(db, nora).id == g.id
    assert ledger.last_entry(db, nora, water).id == a.id


def test_can_edit(db, water, nora, bob):
    e = log(db, nora, water, "reading", 800)
    assert ledger.can_edit(e, nora, NOW + timedelta(minutes=10))
    assert not ledger.can_edit(e, nora, NOW + timedelta(minutes=16))
    assert not ledger.can_edit(e, bob, NOW)


# -- the database refuses to rewrite history ---------------------------------------


def _real_entry(db, member, category, minutes_old: int) -> ResourceEntry:
    """An entry written `minutes_old` minutes ago by the database's clock."""
    written = utcnow() - timedelta(minutes=minutes_old)
    e = ledger.add_entry(db, member, category, "reading", 800, channel="web", now=written)
    db.commit()
    return e


def test_database_refuses_delete(db, water, nora):
    e = _real_entry(db, nora, water, 0)
    with pytest.raises(DBAPIError, match="never deleted"):
        db.execute(text("DELETE FROM resource_entry WHERE id = :id"), {"id": e.id})
    db.rollback()


def test_database_refuses_update_after_the_window(db, water, nora):
    e = _real_entry(db, nora, water, 20)
    with pytest.raises(DBAPIError, match="past its edit window"):
        db.execute(update(ResourceEntry).where(ResourceEntry.id == e.id).values(amount=1))
    db.rollback()


def test_database_refuses_changing_who_or_what(db, water, nora, bob):
    e = _real_entry(db, nora, water, 0)
    with pytest.raises(DBAPIError, match="only amount, note and time"):
        db.execute(update(ResourceEntry).where(ResourceEntry.id == e.id).values(member_id=bob.id))
    db.rollback()


def test_correction_needs_a_target_in_the_database(db, water, nora):
    db.add(
        ResourceEntry(
            category_id=water.id,
            member_id=nora.id,
            channel="web",
            entry_type="correction",
            amount=1,
            occurred_at=NOW,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_history_blocks_deleting_the_resource_and_the_member(db, water, nora, ada):
    log(db, nora, water, "reading", 800)
    db.commit()
    with pytest.raises(categories.HasHistory):
        categories.delete(db, water)
    with pytest.raises(members.HasHistory):
        members.delete_member(db, nora, ada)


# -- events -----------------------------------------------------------------------


@pytest.fixture
def heard():
    got: list[events.Event] = []
    stop = events.subscribe(got.append)
    yield got
    stop()


def test_event_only_after_commit(db, water, nora, heard):
    e = log(db, nora, water, "reading", 800)
    assert heard == []
    db.commit()
    entries = [h for h in heard if h.kind.startswith("ledger.entry_")]  # alerts: test_feed.py
    assert [(h.kind, h.data["entry_id"]) for h in entries] == [("ledger.entry_added", e.id)]


def test_no_event_after_rollback(db, water, nora, heard):
    log(db, nora, water, "reading", 800)
    db.rollback()
    db.commit()
    assert heard == []


def test_a_failing_subscriber_breaks_nothing(db, water, nora, heard):
    def boom(_):
        raise RuntimeError("subscriber bug")

    stop = events.subscribe(boom)
    try:
        log(db, nora, water, "reading", 800)
        db.commit()
    finally:
        stop()
    assert len([h for h in heard if h.kind.startswith("ledger.entry_")]) == 1
    assert db.query(ResourceEntry).count() == 1


# -- the board --------------------------------------------------------------------


def csrf(client) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text).group(1)


@pytest.fixture
def member_client(client, fake_matrix, nora, water):
    sign_in(client, fake_matrix)
    client.token = csrf(client)
    return client


def post_log(client, category, **kw):
    data = {"csrf_token": client.token, "category_id": str(category.id), "entry_type": "reading",
            "amount": "800", "when": "", "note": "", **kw}  # fmt: skip
    return client.post("/ledger/entries", data=data, follow_redirects=False)


def test_board_counts_from_zero_without_a_reading(member_client, db, water, nora):
    assert en.BOARD_NOTHING_LOGGED in member_client.get("/").text
    post_log(member_client, water, entry_type="add", amount="1500")
    page = member_client.get("/").text
    assert '<span class="num">1,500</span>' in page
    assert en.BOARD_FROM_ZERO in page
    assert en.BOARD_NOTHING_LOGGED not in page


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 14, 23, 30, tzinfo=UTC), "2026-11-15T00:30"),
        (datetime(2026, 7, 14, 22, 30, tzinfo=UTC), "2026-07-15T00:30"),
    ],
)
def test_board_prefills_when_in_community_time(member_client, gas, monkeypatch, now, expected):
    monkeypatch.setattr("app.board.utcnow", lambda: now)
    page = member_client.get("/").text
    inputs = re.findall(r'<input\b[^>]*name="when"[^>]*>', page)
    assert len(inputs) == 2
    assert all(f'value="{expected}"' in tag for tag in inputs)
    assert "leave empty for now" not in page


def test_board_untouched_prefill_means_the_moment_of_saving(member_client, db, water):
    # The page was opened hours ago; the member saves without touching "When".
    stale = (utcnow() - timedelta(hours=10)).astimezone(ZoneInfo("Europe/Stockholm"))
    stale_value = stale.strftime("%Y-%m-%dT%H:%M")
    before = utcnow()
    r = post_log(member_client, water, when=stale_value, when_prefill=stale_value)
    assert r.status_code == 303
    entry = db.query(ResourceEntry).one()
    assert entry.occurred_at >= before - timedelta(seconds=1)


def test_board_changed_when_is_kept(member_client, db, water):
    prefill = (utcnow() - timedelta(hours=10)).astimezone(ZoneInfo("Europe/Stockholm"))
    chosen = prefill - timedelta(days=2)
    r = post_log(
        member_client,
        water,
        when=chosen.strftime("%Y-%m-%dT%H:%M"),
        when_prefill=prefill.strftime("%Y-%m-%dT%H:%M"),
    )
    assert r.status_code == 303
    entry = db.query(ResourceEntry).one()
    assert entry.occurred_at == chosen.replace(second=0, microsecond=0)


def test_board_error_keeps_the_original_prefill(member_client, db, water):
    # A rejected save must not turn an untouched time into a "changed" one.
    prefill = "2026-11-14T08:00"
    r = post_log(member_client, water, amount="eight", when=prefill, when_prefill=prefill)
    assert r.status_code == 400
    assert f'name="when_prefill" value="{prefill}"' in r.text
    assert db.query(ResourceEntry).count() == 0


def test_board_logs_and_shows_the_level(member_client, db, water, nora, heard):
    r = post_log(member_client, water, amount="800,5", note="gauge")
    assert r.status_code == 303
    (e,) = db.query(ResourceEntry).all()
    assert r.headers["location"] == f"/?logged={e.id}#res-water"
    assert (e.amount, e.channel, e.member_id, e.note) == (800.5, "web", nora.id, "gauge")
    assert [h.kind for h in heard if h.kind.startswith("ledger.entry_")] == ["ledger.entry_added"]
    page = member_client.get(r.headers["location"]).text
    assert "Logged: Water reading 800.5 l" in page
    assert 'class="ok-box snackbar" data-focus-target="res-water"' in page
    assert '<span role="status">Logged:' in page
    assert f'type="button" aria-label="{en.DISMISS}" hidden>' in page
    assert re.search(r'<script src="/static/snackbar.js\?v=[0-9a-f]{12}" defer></script>', page)
    assert "You can change it for 15 minutes" not in page
    assert '<span class="num">800.5</span>' in page
    assert "Running low" in page  # below 1,000 l
    assert "of 3,000 l" in page  # the level against the capacity
    assert "Warning level: 1,000 l" in page  # the tick on the bar, for screen readers
    assert "Water is running low: 800.5 l, below the 1,000 l warning level." in page
    assert "Recent entries" in page
    assert en.BOARD_FROM_ZERO not in page  # a real reading
    # Reloading the board (a GET) never logs again.
    assert 'class="ok-box snackbar"' not in member_client.get("/").text
    assert db.query(ResourceEntry).count() == 1


@pytest.mark.parametrize("viewer", ["other_member", "signed_out", "missing_entry"])
def test_board_confirmation_only_for_the_authors_entry(member_client, db, water, bob, viewer):
    response = post_log(member_client, water)
    location = response.headers["location"]
    if viewer == "other_member":
        entry = ledger.add_entry(db, bob, water, "reading", 900, channel="web")
        db.commit()
        location = f"/?logged={entry.id}"
    elif viewer == "signed_out":
        member_client.cookies.clear()
    else:
        location = "/?logged=999999"
    page = member_client.get(location).text
    assert 'class="ok-box snackbar"' not in page
    assert "/static/snackbar.js" not in page


@pytest.mark.parametrize(
    ("change", "status", "message", "field"),
    [
        ({"amount": "eight"}, 400, "Enter a number", "amount"),
        ({"amount": "-5"}, 400, "positive number", "amount"),
        ({"amount": "99999"}, 400, "more than Water can hold", "amount"),
        ({"entry_type": "drink"}, 400, "takes reading, use, add", None),
        ({"when": "2099-01-01T10:00"}, 400, "in the future", "when"),
        ({"when": "yesterday"}, 400, "isn", "when"),
        ({"when": "2000-01-01T10:00"}, 400, "at most 30 days", "when"),
        ({"when": "2026-11-10T10:00+01:00"}, 400, "isn", "when"),
        ({"note": "x" * 141}, 400, "at most 140 characters", "note"),
    ],
)
def test_board_form_errors(member_client, db, water, gas, change, status, message, field):
    r = post_log(member_client, water, **change)
    assert r.status_code == status
    assert message in r.text.replace("&#39;", "'")
    assert '<details class="log" open>' in r.text  # the form stays open
    assert r.text.count('<details class="log" open>') == 1
    assert r.text.count('role="alert"') == 1
    for name in ("amount", "when", "note"):
        inputs = re.findall(rf'<input\b[^>]*name="{name}"[^>]*>', r.text)
        assert len(inputs) == 2  # only the affected resource gets an invalid field
        for index, input_tag in enumerate(inputs):
            invalid = name == field and index == 0
            assert ('aria-invalid="true"' in input_tag) == invalid
            assert ("bad" in re.search(r'class="([^"]*)"', input_tag).group(1).split()) == invalid
            assert (f'aria-describedby="err-{water.id}"' in input_tag) == invalid
        # Rejected values stay available to correct, including escaped notes.
        assert f'value="{change.get(name, "800" if name == "amount" else "")}"' in inputs[0]
    if field:
        assert re.search(
            rf'name="{field}"[^>]*>.*?</label>\s*'
            rf'<span class="err" id="err-{water.id}" role="alert">',
            r.text,
            re.DOTALL,
        )
    else:
        assert r.text.index('role="alert"') < r.text.index('class="choices"')
    # time and note sit behind "more"; it opens for an error in either of them
    assert ('<details class="more" open>' in r.text) == (field in ("when", "note"))
    assert db.query(ResourceEntry).count() == 0


def test_board_when_is_local_time(member_client, db, water):
    post_log(member_client, water, when="2026-11-10T08:30")
    (e,) = db.query(ResourceEntry).all()
    if e.occurred_at > utcnow():
        pytest.skip("test clock is before 10 Nov 2026 08:30 Stockholm")
    assert e.occurred_at == datetime(2026, 11, 10, 7, 30, tzinfo=UTC)  # CET = UTC+1


def test_board_unknown_or_archived_resource(member_client, db, water):
    assert post_log(member_client, water, category_id="999").status_code == 404
    categories.archive(db, water)
    db.commit()
    r = post_log(member_client, water)
    assert r.status_code == 400
    assert 'aria-invalid="true"' not in r.text


def test_board_escapes_notes(member_client, db, water):
    post_log(member_client, water, note="<script>x</script>")
    assert "<script>x" not in member_client.get("/").text


def test_board_puts_low_resources_first_and_says_so(member_client, db, water, nora):
    wood = categories.create(db, Spec(slug="firewood", name="Firewood", unit="m³", kind="stock",
                                      shortage_threshold=2))  # fmt: skip
    db.commit()
    ledger.add_entry(db, nora, water, "reading", 2000, channel="web")
    ledger.add_entry(db, nora, wood, "reading", 6, channel="web")
    db.commit()
    page = member_client.get("/").text
    assert en.BOARD_ALL_OK in page
    assert page.index('id="res-water"') < page.index('id="res-firewood"')  # configured order
    assert page.count('<svg class="bar"') == 1  # firewood has no capacity: no empty bar
    assert "Low below 2 m³" in page
    ledger.add_entry(db, nora, wood, "use", 5, channel="web")
    db.commit()
    page = member_client.get("/").text
    assert "Firewood is running low: 1 m³, below the 2 m³ warning level." in page
    assert en.BOARD_ALL_OK not in page
    assert page.index('id="res-firewood"') < page.index('id="res-water"')  # low first


def test_board_flags_old_readings(member_client, db, water, nora):
    ledger.add_entry(db, nora, water, "reading", 2000, channel="web",
                     occurred_at=utcnow() - timedelta(days=8))  # fmt: skip
    db.commit()
    page = member_client.get("/").text
    assert en.BOARD_STATUS_OLD in page
    assert en.BOARD_MAYBE_OLD in page
    assert "Reading by Nora, 8 days ago" in page


def test_board_says_when_a_backdated_entry_happened(member_client, db, water, nora):
    ledger.add_entry(db, nora, water, "use", 20, channel="matrix",
                     occurred_at=utcnow() - timedelta(days=3))  # fmt: skip
    bob = members.add_member(db, f"@bob:{SERVER}")  # no display name
    ledger.add_entry(db, bob, water, "use", 30, channel="web")
    db.commit()
    page = member_client.get("/").text
    assert page.count("happened ") == 1  # only the backdated one
    assert "<b>bob</b>" in page  # the short name, as on the calendar
    assert "@bob:" not in page
    assert "via chat" in page
    assert "· web" not in page
    assert "matrix" not in page.split("Recent entries")[-1]


def test_board_shows_corrections(member_client, db, water, nora):
    e = ledger.add_entry(db, nora, water, "reading", 800, channel="matrix")
    ledger.correct_entry(db, nora, e, 750, channel="matrix")
    db.commit()
    page = member_client.get("/").text
    assert "corrected Water (reading) to 750 l" in page
    assert '<span class="num">750</span>' in page
