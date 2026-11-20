"""Bookings of bookable things: overlaps, the optional end,
the open-booking window, changes, returns, cancels, database guards, room lines."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from tests.conftest import SERVER

from app import bookables, events, members
from app import calendar as cal
from app import decision_log as dl
from app.models import Bookable, Booking, ConfigKV, DecisionLog, EventFeed, Member, utcnow
from app.textfmt import DASH

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
STOCKHOLM = ZoneInfo("Europe/Stockholm")
H = timedelta(hours=1)


@pytest.fixture
def things(db):
    """The DRAFT Outpost set: the theatre, the car and the saw."""
    for name, type_, where in (
        ("Theatre", "space", None),
        ("Car", "vehicle", "the barn"),
        ("Saw", "tool", None),
    ):
        bookables.create(db, bookables.ThingSpec(name=name, type=type_, location=where))
    db.commit()
    return {t.slug: t for t in bookables.list_active(db)}


@pytest.fixture
def ana(db):
    m = members.add_member(db, f"@ana:{SERVER}", "Ana")
    db.commit()
    return m


@pytest.fixture
def bob(db):
    m = members.add_member(db, f"@bob:{SERVER}", "Bob")
    db.commit()
    return m


@pytest.fixture
def ada(db, log_signer):
    m = members.add_member(db, f"@ada:{SERVER}", "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


@pytest.fixture
def heard():
    got: list[events.Event] = []
    stop = events.subscribe(got.append)
    yield got
    stop()


def book(db, member, thing, start_h, end_h=None, **kw):
    """Book `thing` from NOW + start_h hours to NOW + end_h hours (None: no end)."""
    return cal.book(
        db,
        member,
        thing,
        NOW + start_h * H,
        None if end_h is None else NOW + end_h * H,
        channel=kw.pop("channel", "matrix"),
        now=kw.pop("now", NOW),
        **kw,
    )


# --- overlaps ------------------------------------------------------------------


def test_overlapping_booking_rejected_with_what_is_in_the_way(db, things, ana, bob):
    theatre = things["theatre"]
    first = book(db, ana, theatre, 24, 48)
    db.commit()
    with pytest.raises(cal.Conflict, match="Theatre is already booked") as e:
        book(db, bob, theatre, 36, 60)
    assert [b.id for b in e.value.blocking] == [first.id]
    db.rollback()
    assert len(db.scalars(select(Booking)).all()) == 1


def test_back_to_back_bookings_do_not_clash(db, things, ana, bob):
    book(db, ana, things["theatre"], 24, 48)
    book(db, bob, things["theatre"], 48, 50)
    book(db, bob, things["theatre"], 20, 24)
    db.commit()


def test_other_things_are_independent(db, things, ana, bob):
    book(db, ana, things["theatre"], 24, 48)
    book(db, bob, things["saw"], 24, 48)
    db.commit()


def test_whole_day_theatre_booking_from_chat_dates(db, things, ana, bob):
    """Two members book the theatre for overlapping days; one gets it."""
    start, end = cal.day_range(date(2026, 12, 1), date(2026, 12, 13), STOCKHOLM)
    cal.book(db, ana, things["theatre"], start, end, channel="matrix", now=NOW)
    db.commit()
    start2, end2 = cal.day_range(date(2026, 12, 13), date(2026, 12, 14), STOCKHOLM)
    with pytest.raises(cal.Conflict):
        cal.book(db, bob, things["theatre"], start2, end2, channel="web", now=NOW)
    db.rollback()
    start3, end3 = cal.day_range(date(2026, 12, 14), date(2026, 12, 14), STOCKHOLM)
    cal.book(db, bob, things["theatre"], start3, end3, channel="web", now=NOW)
    db.commit()


def test_concurrent_overlapping_bookings_one_wins(app, things, ana, bob):
    """Two sessions book at the same moment; the database lets exactly one through."""
    flushed = threading.Event()
    results: dict[str, object] = {}
    theatre_id = things["theatre"].id

    def first():
        with app.state.sessionmaker() as s:
            cal.book(
                s, s.get(Member, ana.id), s.get(Bookable, theatre_id), NOW + 24 * H,
                NOW + 48 * H, channel="web", now=NOW,
            )  # fmt: skip
            flushed.set()  # row written, not committed: the second insert must wait
            threading.Event().wait(0.5)
            s.commit()
            results["first"] = "booked"

    def second():
        flushed.wait(5)
        with app.state.sessionmaker() as s:
            try:
                cal.book(
                    s, s.get(Member, bob.id), s.get(Bookable, theatre_id), NOW + 30 * H,
                    NOW + 40 * H, channel="matrix", now=NOW,
                )  # fmt: skip
                s.commit()
                results["second"] = "booked"
            except cal.Conflict as e:
                results["second"] = ("conflict", [b.member_id for b in e.blocking])

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == {"first": "booked", "second": ("conflict", [ana.id])}


def test_cancelled_booking_frees_the_time(db, things, ana, bob):
    b = book(db, ana, things["theatre"], 24, 48)
    cal.cancel(db, ana, b, now=NOW)
    book(db, bob, things["theatre"], 24, 48)
    db.commit()


def test_failed_booking_keeps_earlier_events_of_the_transaction(db, things, ana, bob, heard):
    book(db, ana, things["theatre"], 24, 48)
    with pytest.raises(cal.Conflict):
        book(db, bob, things["theatre"], 30, 40)
    db.commit()
    assert [e.kind for e in heard] == ["booking.created"]


def test_booking_rolled_back_announces_nothing(db, things, ana, heard):
    book(db, ana, things["theatre"], 24, 48)  # its savepoint is released, not committed
    assert heard == []
    db.rollback()
    db.commit()
    assert heard == []


# --- no end: the open-booking window ---------------------------------------------


def test_no_end_blocks_for_two_hours_then_others_can_book(db, things, ana, bob, heard):
    """Ana takes the saw with no end: refused for 2 h, then Bob can book and Ana hears."""
    saw = things["saw"]
    ana_saw = book(db, ana, saw, 0)
    db.commit()
    assert (ana_saw.ends_at, ana_saw.blocks_until) == (None, NOW + 2 * H)
    with pytest.raises(cal.Conflict, match="Saw is with Ana since") as e:
        book(db, bob, saw, 1, 3)
    assert [b.id for b in e.value.blocking] == [ana_saw.id]
    db.rollback()
    # Three days later Ana still has it; Bob books it from 10 minutes from now.
    later = NOW + 72 * H
    bob_saw = cal.book(db, bob, saw, later + timedelta(minutes=10), channel="matrix", now=later)
    db.commit()
    assert heard[-1].kind == "booking.created"
    assert heard[-1].data["still_with"] == [ana_saw.id]
    # Ana's booking stays open: nothing is ended on her behalf.
    db.refresh(ana_saw)
    assert (ana_saw.ends_at, ana_saw.returned_at) == (None, None)
    assert {b.id for b in cal.who_has(db, saw, later + timedelta(minutes=20))} == {
        ana_saw.id,
        bob_saw.id,
    }


def test_open_booking_window_is_a_community_setting(db, things, ana, bob):
    db.add(
        ConfigKV(
            key=bookables.SETTINGS_KEY, value_json={"open_booking_hours": 4}, updated_at=utcnow()
        )
    )
    db.commit()
    book(db, ana, things["saw"], 0)
    with pytest.raises(cal.Conflict):
        book(db, bob, things["saw"], 3, 5)
    db.rollback()
    book(db, bob, things["saw"], 4, 5)


def test_a_booking_with_an_end_blocks_exactly_until_it(db, things, ana, bob):
    book(db, ana, things["car"], 0, 2, destination="town")
    book(db, bob, things["car"], 2, 4)
    db.commit()


# --- the end is a hint: returns ------------------------------------------------


def test_late_return_is_recorded_not_extended(db, things, ana, bob, heard):
    car = things["car"]
    trip = book(db, ana, car, 0, 2)
    nxt = book(db, bob, car, 2, 4)
    db.commit()
    # Past her end, Ana still has the car; Bob's booking has started too.
    assert {b.id for b in cal.who_has(db, car, NOW + 3 * H)} == {trip.id, nxt.id}
    cal.return_item(db, ana, trip, now=NOW + 3 * H)
    db.commit()
    db.refresh(trip)
    assert (trip.ends_at, trip.returned_at) == (NOW + 2 * H, NOW + 3 * H)
    assert heard[-1].kind == "booking.returned"
    assert heard[-1].data["late"] is True


def test_a_space_is_free_again_at_its_end(db, things, ana):
    b = book(db, ana, things["theatre"], 0, 2)
    assert cal.who_has(db, things["theatre"], NOW + H) == [b]
    assert cal.who_has(db, things["theatre"], NOW + 3 * H) == []


def test_early_return_and_return_of_an_open_booking_free_the_thing(db, things, ana, bob):
    saw = things["saw"]
    b = book(db, ana, saw, 0)
    cal.return_item(db, ana, b, now=NOW + timedelta(minutes=30))
    assert (b.ends_at, b.blocks_until) == (None, NOW + timedelta(minutes=30))
    book(db, bob, saw, 1, 2)
    db.commit()


def test_return_rules(db, things, ana, bob):
    future = book(db, ana, things["saw"], 5, 6)
    with pytest.raises(cal.BookingError, match="hasn't started"):
        cal.return_item(db, ana, future, now=NOW)
    now_b = book(db, ana, things["car"], 0, 2)
    with pytest.raises(cal.NotAllowed):
        cal.return_item(db, bob, now_b, now=NOW + H)
    cal.return_item(db, ana, now_b, now=NOW + H)
    with pytest.raises(cal.BookingError, match="already given back"):
        cal.return_item(db, ana, now_b, now=NOW + H)


# --- what a booking asks ------------------------------------------------------------


def test_only_vehicles_say_where_to(db, things, ana):
    car = book(db, ana, things["car"], 0, 2, destination="  to town  for shopping ")
    assert car.destination == "to town for shopping"
    with pytest.raises(cal.BookingError, match="only a vehicle"):
        book(db, ana, things["saw"], 0, 2, destination="town")


def test_note_is_one_line_and_short(db, things, ana):
    assert book(db, ana, things["saw"], 0, 1, note=" cutting   firewood ").note == (
        "cutting firewood"
    )
    with pytest.raises(cal.BookingError, match="one line"):
        book(db, ana, things["saw"], 2, 3, note="a\x00b")
    with pytest.raises(cal.BookingError, match="at most 200"):
        book(db, ana, things["saw"], 2, 3, note="x" * 201)


@pytest.mark.parametrize(
    ("start_h", "end_h", "message"),
    [
        (2, 1, "after the start"),
        (-3, -1, "start is already over"),
        (-0.1, None, None),  # "now" a few minutes ago is fine
    ],
)
def test_times(db, things, ana, start_h, end_h, message):
    if message is None:
        book(db, ana, things["saw"], start_h, end_h)
        return
    with pytest.raises(cal.BookingError, match=message):
        book(db, ana, things["saw"], start_h, end_h)


def test_too_far_ahead_and_naive_times_rejected(db, things, ana):
    with pytest.raises(cal.BookingError, match="two years"):
        book(db, ana, things["theatre"], 24 * 800, 24 * 801)
    with pytest.raises(cal.BookingError, match="timezone"):
        cal.book(
            db, ana, things["theatre"], datetime(2026, 12, 1, 10),  # noqa: DTZ001
            channel="web", now=NOW,
        )  # fmt: skip


def test_deactivated_member_and_archived_thing(db, things, ana, ada, log_signer):
    bookables.archive(db, things["theatre"])
    with pytest.raises(cal.BookingError, match="can't be booked"):
        book(db, ana, things["theatre"], 1, 2)
    members.deactivate(db, ana, ada.id, signer=log_signer, channel="web")
    with pytest.raises(cal.NotAllowed):
        book(db, ana, things["saw"], 1, 2)


# --- changing a booking ------------------------------------------------------------


def test_member_extends_shortens_and_removes_the_end(db, things, ana, bob, heard):
    saw = things["saw"]
    b = book(db, ana, saw, 0, 2)
    book(db, bob, saw, 4, 6)
    db.commit()
    cal.change(db, ana, b, ends_at=NOW + 4 * H, now=NOW + H)  # extend while free
    assert b.blocks_until == NOW + 4 * H
    with pytest.raises(cal.Conflict, match="already booked"):
        cal.change(db, ana, b, ends_at=NOW + 5 * H, now=NOW + H)
    db.refresh(b)
    assert b.ends_at == NOW + 4 * H  # the refused change left it as it was
    cal.change(db, ana, b, ends_at=NOW + 3 * H, note="almost done", now=NOW + H)
    db.commit()
    assert [e.kind for e in heard][-2:] == ["booking.changed", "booking.changed"]
    assert heard[-1].data["old"]["ends_at"] == (NOW + 4 * H).isoformat()


def test_removing_the_end_blocks_for_the_window_from_the_start(db, things, ana):
    b = book(db, ana, things["saw"], 1, 5)
    cal.change(db, ana, b, ends_at=None, now=NOW)
    assert (b.ends_at, b.blocks_until) == (None, NOW + 3 * H)


def test_a_started_booking_keeps_its_start_and_no_change_is_quiet(db, things, ana, heard):
    b = book(db, ana, things["theatre"], 0, 2)
    db.commit()
    with pytest.raises(cal.BookingError, match="start stays"):
        cal.change(db, ana, b, starts_at=NOW + H, now=NOW + timedelta(minutes=5))
    cal.change(db, ana, b, ends_at=NOW + 2 * H, now=NOW)
    db.commit()
    assert [e.kind for e in heard] == ["booking.created"]


def test_only_the_member_or_an_admin_changes_a_booking(db, things, ana, bob, ada, log_signer):
    b = book(db, ana, things["car"], 1, 2)
    db.commit()
    with pytest.raises(cal.NotAllowed):
        cal.change(db, bob, b, ends_at=NOW + 3 * H, now=NOW)
    with pytest.raises(dl.DecisionLogError):
        cal.change(db, ada, b, ends_at=NOW + 3 * H, now=NOW)
    cal.change(
        db, ada, b, ends_at=NOW + 3 * H, destination="the station", signer=log_signer, now=NOW
    )
    db.commit()
    entry = db.scalars(
        select(DecisionLog).where(DecisionLog.kind == "booking.changed_by_admin")
    ).one()
    assert entry.actor_member_id == ada.id
    assert entry.payload_json["member_id"] == ana.id
    assert entry.payload_json["old"]["ends_at"] == (NOW + 2 * H).isoformat()
    assert entry.payload_json["new"]["destination"] == "the station"
    assert b.updated_by == ada.id
    assert dl.verify_db(db).ok


def test_given_back_or_cancelled_bookings_cant_change(db, things, ana):
    b = book(db, ana, things["saw"], 2, 3)
    cal.cancel(db, ana, b, now=NOW)
    with pytest.raises(cal.BookingError, match="cancelled"):
        cal.change(db, ana, b, note="x", now=NOW)


# --- cancelling ----------------------------------------------------------------------


def test_cancel_rules(db, things, ana, bob):
    b = book(db, ana, things["theatre"], 24, 48)
    with pytest.raises(cal.NotAllowed):
        cal.cancel(db, bob, b, now=NOW)
    started = book(db, ana, things["saw"], 0, 2)
    with pytest.raises(cal.BookingError, match="already started"):
        cal.cancel(db, ana, started, now=NOW + H)


def test_admin_cancel_is_logged_with_reason(db, things, ana, ada, log_signer, heard):
    b = book(db, ana, things["theatre"], 24, 48)
    db.commit()
    cal.admin_cancel(db, ada, b, "  double booked by mistake ", log_signer, now=NOW)
    db.commit()
    entry = db.scalars(
        select(DecisionLog).where(DecisionLog.kind == "booking.cancelled_by_admin")
    ).one()
    assert entry.actor_member_id == ada.id
    assert entry.payload_json["reason"] == "double booked by mistake"
    assert entry.payload_json["bookable"] == "theatre"
    assert heard[-1].data["by_admin"] is True
    assert dl.verify_db(db).ok


def test_admin_cancel_needs_admin_and_reason(db, things, ana, bob, ada, log_signer):
    b = book(db, ana, things["theatre"], 24, 48)
    with pytest.raises(cal.NotAllowed):
        cal.admin_cancel(db, bob, b, "mine now", log_signer, now=NOW)
    with pytest.raises(cal.BookingError, match="reason"):
        cal.admin_cancel(db, ada, b, "   ", log_signer, now=NOW)


# --- database guards -----------------------------------------------------------------


def test_bookings_are_never_deleted_and_who_booked_what_stays(db, things, ana, bob):
    b = book(db, ana, things["theatre"], 24, 48)
    db.commit()
    for sql, match in [
        ("DELETE FROM booking", "never deleted"),
        ("TRUNCATE booking", "never deleted"),
        ("UPDATE booking SET member_id = :bob", "can't change"),
        ("UPDATE booking SET bookable_id = :saw", "can't change"),
    ]:
        with pytest.raises(DBAPIError, match=match):
            db.execute(text(sql), {"bob": bob.id, "saw": things["saw"].id})
        db.rollback()
    later = (
        "UPDATE booking SET ends_at = ends_at + interval '1 hour', "
        "blocks_until = blocks_until + interval '1 hour'"
    )
    db.execute(text(later))  # an end can move later now, while the time is free
    db.commit()
    db.execute(text("UPDATE booking SET cancelled_at = now(), cancelled_by = member_id"))
    db.commit()
    for sql in ("UPDATE booking SET cancel_reason = 'rewritten'", later):
        with pytest.raises(DBAPIError, match="already cancelled"):
            db.execute(text(sql))
        db.rollback()
    assert db.get(Booking, b.id) is not None


def test_a_given_back_booking_keeps_its_times(db, things, ana):
    book(db, ana, things["saw"], 0, 2)
    b = db.scalars(select(Booking)).one()
    cal.return_item(db, ana, b, now=NOW + H)
    db.commit()
    with pytest.raises(DBAPIError, match="already given back"):
        db.execute(text("UPDATE booking SET returned_at = NULL"))
    db.rollback()


def test_database_checks_the_blocking_end(db, things, ana):
    insert = text(
        "INSERT INTO booking (bookable_id, member_id, starts_at, ends_at, blocks_until, channel, "
        "created_at) VALUES (:b, :m, :s, :e, :u, 'web', now())"
    )
    for ends, until in [(NOW + H, NOW + 2 * H), (None, NOW), (NOW - H, NOW - H)]:
        with pytest.raises(IntegrityError):
            db.execute(
                insert, {"b": things["saw"].id, "m": ana.id, "s": NOW, "e": ends, "u": until}
            )
        db.rollback()


# --- reading -----------------------------------------------------------------------


def test_availability(db, things, ana):
    saw = things["saw"]
    b = book(db, ana, saw, 2, 4)
    assert not cal.availability(db, saw, NOW + 3 * H, NOW + 5 * H).free
    free = cal.availability(db, saw, NOW + 4 * H, NOW + 6 * H)
    assert (free.free, free.bookings) == (True, [])
    assert not cal.availability(db, saw, NOW + 3 * H).free  # no end: the 2-hour window
    assert cal.next_booking(db, saw, NOW).id == b.id


def test_day_range_follows_the_local_calendar_across_dst():
    start, end = cal.day_range(date(2026, 10, 25), date(2026, 10, 25), STOCKHOLM)
    assert (start, end - start) == (datetime(2026, 10, 24, 22, 0, tzinfo=UTC), timedelta(hours=25))
    with pytest.raises(cal.BookingError):
        cal.day_range(date(2026, 12, 2), date(2026, 12, 1), STOCKHOLM)


# --- the COMMONS room ------------------------------------------------------------------


def feed_lines(db) -> list[str]:
    rows = db.scalars(select(EventFeed.summary).order_by(EventFeed.id))
    return [line.replace(DASH, "-") for line in rows]


def test_every_booking_and_change_gets_a_room_line(db, things, ana, bob, ada, log_signer):
    db.add(ConfigKV(key="community.timezone", value_json="Europe/Stockholm", updated_at=utcnow()))
    db.commit()
    trip = book(db, ana, things["car"], 1, 3, destination="town for shopping", note="bags")
    db.commit()
    cal.change(db, ana, trip, ends_at=NOW + 4 * H, now=NOW)
    db.commit()
    cal.change(db, ada, trip, ends_at=NOW + 5 * H, signer=log_signer, now=NOW)
    db.commit()
    saw = book(db, bob, things["saw"], 0)
    db.commit()
    later = NOW + 5 * H
    cal.book(db, ana, things["saw"], later, later + H, channel="web", now=later)
    db.commit()
    cal.return_item(db, bob, saw, now=later + 2 * H)
    db.commit()
    lines = feed_lines(db)
    assert lines[0] == (
        "Ana booked Car: Tue 10 Nov 2026, 14:00-16:00, going to town for shopping (bags)"
    )
    assert lines[1].startswith("Ana changed the booking of Car: now Tue 10 Nov 2026, 14:00-17:00")
    assert lines[2].startswith("Ada changed Ana's booking of Car: now")
    assert lines[3] == "Bob booked Saw: from Tue 10 Nov 2026, 13:00"
    assert lines[4].endswith("; Bob still has it")
    assert lines[5] == "Bob gave back Saw"


def test_web_time_names_nearby_days_and_keeps_the_year_when_it_differs():
    from app.textfmt import web_time

    tz = ZoneInfo("Europe/Stockholm")
    now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)  # Sat 14 Nov, 11:00 in Stockholm
    at = lambda *a: datetime(*a, tzinfo=tz)  # noqa: E731
    assert web_time(at(2026, 11, 14, 8, 27), tz, now) == "Today 08:27"
    assert web_time(at(2026, 11, 13, 19, 27), tz, now) == "Yesterday 19:27"
    assert web_time(at(2026, 11, 15, 10, 0), tz, now) == "Tomorrow 10:00"
    assert web_time(at(2026, 11, 10, 14, 0), tz, now) == "Tue 10 Nov 14:00"
    assert web_time(at(2025, 12, 31, 23, 0), tz, now) == "Wed 31 Dec 2025 23:00"
    # UTC in, local out: 23:30 UTC on the 13th is already the 14th in Stockholm.
    assert web_time(datetime(2026, 11, 13, 23, 30, tzinfo=UTC), tz, now) == "Today 00:30"
