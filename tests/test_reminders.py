"""Reminders to give a vehicle or tool back: when, how often, to whom, and delivery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import pytest
from sqlalchemy import select
from tests.conftest import SERVER

from app import bookables, events, members, reminders
from app import calendar as cal
from app.models import ConfigKV, EventFeed
from bot.dm import DirectMessages

T = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)  # the tests seed no time zone: UTC
H = timedelta(hours=1)
MIN = timedelta(minutes=1)


@pytest.fixture
def things(db):
    for name, type_ in (("Theatre", "space"), ("Car", "vehicle"), ("Saw", "tool")):
        bookables.create(db, bookables.ThingSpec(name=name, type=type_))
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


def take(db, member, thing, hours: float = 1, **kw):
    b = cal.book(db, member, thing, T, T + hours * H, channel="matrix", now=T, **kw)
    db.commit()
    return b


def sent(db) -> list[EventFeed]:
    db.expire_all()
    return list(
        db.scalars(
            select(EventFeed).where(EventFeed.kind == reminders.OVERDUE).order_by(EventFeed.id)
        )
    )


# --- when ----------------------------------------------------------------------


def test_one_reminder_two_hours_after_the_end(db, things, ana):
    b = take(db, ana, things["saw"])  # due 10:00
    assert reminders.send_due(db, T + 3 * H - MIN) == 0
    assert reminders.send_due(db, T + 3 * H) == 1
    assert reminders.send_due(db, T + 5 * H) == 0  # once per end
    [row] = sent(db)
    assert row.data["to"] == ana.matrix_user_id
    assert row.data["booking_id"] == b.id
    assert row.summary.startswith("You said you'd give back the Saw by Tue 10 Nov 2026, 10:00.")
    assert '"brought the saw back"' in row.summary
    assert row.member_id is None  # not an action of anyone's


def test_a_longer_booking_gets_a_new_reminder_for_its_new_end(db, things, ana):
    b = take(db, ana, things["car"])
    reminders.send_due(db, T + 3 * H)
    cal.change(db, ana, b, ends_at=T + 5 * H, now=T + 3 * H)
    db.commit()
    assert reminders.send_due(db, T + 6 * H) == 0
    assert reminders.send_due(db, T + 7 * H) == 1
    assert [r.data["due_at"] for r in sent(db)] == [
        (T + H).isoformat(),
        (T + 5 * H).isoformat(),
    ]


def test_given_back_or_on_time_means_no_reminder(db, things, ana, bob):
    b = take(db, ana, things["saw"])
    cal.return_item(db, ana, b, now=T + 2 * H)
    db.commit()
    take(db, bob, things["car"], hours=10)
    assert reminders.send_due(db, T + 4 * H) == 0
    assert sent(db) == []


def test_spaces_get_no_reminder(db, things, ana):
    take(db, ana, things["theatre"])
    assert reminders.send_due(db, T + 3 * H) == 0


def test_no_reminder_without_an_end(db, things, ana):
    cal.book(db, ana, things["saw"], T, None, channel="matrix", now=T)
    db.commit()
    assert reminders.send_due(db, T + 5 * H) == 0


def test_long_overdue_bookings_are_left_alone(db, things, ana):
    take(db, ana, things["saw"])
    assert reminders.send_due(db, T + H + 24 * H) == 0


def test_deactivated_member_gets_none(db, things, ana, log_signer):
    take(db, ana, things["saw"])
    admin = members.add_member(db, f"@ada:{SERVER}", "Ada", ("admin",), signer=log_signer)
    db.commit()
    members.deactivate(db, ana, admin.id, signer=log_signer, channel="web")
    db.commit()
    assert reminders.send_due(db, T + 3 * H) == 0


# --- quiet hours -----------------------------------------------------------------


def test_nothing_at_night_and_sent_when_quiet_hours_end(db, things, ana):
    late = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
    cal.book(db, ana, things["saw"], late, late + H, channel="matrix", now=late)
    db.commit()  # due 21:00, reminder at 23:00: quiet (22:00 to 08:00)
    assert reminders.send_due(db, late + 3 * H) == 0
    assert reminders.send_due(db, late + 11 * H - MIN) == 0  # 07:59
    assert reminders.send_due(db, late + 12 * H) == 1  # 08:00


def test_the_times_are_community_settings(db, things, ana):
    db.add(
        ConfigKV(
            key=bookables.SETTINGS_KEY,
            value_json={"reminder_every_hours": 1, "quiet_hours_start": "08:00",
                        "quiet_hours_end": "08:00"},
        )
    )  # fmt: skip
    db.commit()
    take(db, ana, things["saw"])
    assert reminders.send_due(db, T + 2 * H) == 1


@pytest.mark.parametrize(
    ("hhmm", "quiet"),
    [("21:59", False), ("22:00", True), ("03:00", True), ("07:59", True), ("08:00", False)],
)
def test_quiet_hours_across_midnight(hhmm, quiet):
    h, m = map(int, hhmm.split(":"))
    now = datetime(2026, 11, 10, h, m, tzinfo=UTC)
    assert reminders.is_quiet(now, bookables.BookingSettings(), UTC) is quiet


# --- what it says --------------------------------------------------------------------


def test_says_who_booked_it_next(db, things, ana, bob):
    take(db, ana, things["saw"])
    cal.book(db, bob, things["saw"], T + 4 * H, T + 6 * H, channel="web", now=T)
    db.commit()
    reminders.send_due(db, T + 3 * H)
    assert " Bob has booked it from Tue 10 Nov 2026, 13:00." in sent(db)[0].summary


def test_the_reminder_goes_onto_the_event_stream(db, things, ana):
    got: list[events.Event] = []
    stop = events.subscribe(got.append)
    try:
        take(db, ana, things["saw"])
        reminders.send_due(db, T + 3 * H)
    finally:
        stop()
    [e] = [e for e in got if e.kind == reminders.OVERDUE]
    assert e.data["feed"]["to"] == ana.matrix_user_id
    assert e.data["feed"]["kind"] == reminders.OVERDUE


# --- the bot delivers it privately ------------------------------------------------------

BOT = f"@commons:{SERVER}"
NORA = f"@nora:{SERVER}"


class FakeMatrix:
    bot_user_id = BOT

    def __init__(self, rooms: dict[str, set[str]] | None = None, fail: bool = False) -> None:
        self.rooms = rooms or {}
        self.sent: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.fail = fail

    async def joined_rooms(self) -> list[str]:
        return list(self.rooms)

    async def members(self, room_id: str) -> set[str]:
        return self.rooms[room_id]

    async def create_direct(self, user_id: str) -> str:
        room = f"!new{len(self.created)}:{SERVER}"
        self.created.append(user_id)
        self.rooms[room] = {BOT, user_id}
        return room

    async def send_text(self, room_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("matrix down")
        self.sent.append((room_id, text))


def item(**kw):
    return {"id": 7, "kind": reminders.OVERDUE, "summary": "give it back", "to": NORA, **kw}


def run(dm: DirectMessages, it: dict) -> bool:
    return anyio.run(dm.on_feed, it)


def test_sent_to_the_members_own_room_with_the_bot():
    m = FakeMatrix({"!village:x": {BOT, NORA, f"@ana:{SERVER}"}, "!dm:x": {BOT, NORA}})
    assert run(DirectMessages(m, server_name=SERVER), item()) is True
    assert m.sent == [("!dm:x", "give it back")]
    assert m.created == []


def test_never_in_the_community_room_even_with_two_members():
    m = FakeMatrix({"!village:x": {BOT, NORA}})
    dm = DirectMessages(m, server_name=SERVER, community_room=lambda: "!village:x")
    run(dm, item())
    assert m.created == [NORA]
    assert m.sent[0][0] != "!village:x"


def test_room_lines_are_left_to_the_room_feed():
    m = FakeMatrix()
    assert run(DirectMessages(m, server_name=SERVER), item(kind="booking.created")) is False
    assert m.sent == []


@pytest.mark.parametrize("to", ["@eve:evil.example", None, 42])
def test_only_to_members_of_this_server(to):
    m = FakeMatrix({"!dm:x": {BOT, NORA}})
    assert run(DirectMessages(m, server_name=SERVER), item(to=to)) is True
    assert m.sent == []


def test_old_replays_are_skipped():
    m = FakeMatrix({"!dm:x": {BOT, NORA}})
    dm = DirectMessages(m, server_name=SERVER, clock=lambda: T.timestamp() + 2 * 86400)
    run(dm, item(at=T.isoformat()))
    assert m.sent == []


def test_a_matrix_error_does_not_stop_the_stream():
    m = FakeMatrix({"!dm:x": {BOT, NORA}}, fail=True)
    assert run(DirectMessages(m, server_name=SERVER), item()) is True
