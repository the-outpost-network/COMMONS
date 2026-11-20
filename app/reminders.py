"""Reminders for vehicles and tools kept past their end.

COMMONS can't make anyone give a thing back, so it asks instead. When a
vehicle or tool booking with an end is still not given back
`reminder_every_hours` after that end, the member gets one private message:
when it was due, who booked it next if anyone, and how to give it back or keep
it longer. Spaces get none: a space is free again at its end.

- Once per end: extending the booking ("keep the saw until 18:00") gives the
  new end its own reminder; giving it back or cancelling stops it.
- Never at night: during the community's quiet hours nothing is sent; a
  reminder due then goes out when they end.
- Never for something more than `reminders_stop_after_hours` overdue, so an
  old forgotten booking isn't nagged about out of the blue.

The reminder is a row in `event_feed` (kind `booking.overdue`), written in
the same transaction as the check: that row is the record that it was sent,
and the bot gets it over /internal/events like the room lines, also on replay
after a restart. Its `to` names the member; the bot sends it to them
privately, never to the community room.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app import bookables
from app import calendar as cal
from app.events import Event, publish
from app.models import Bookable, Booking, EventFeed, utcnow

log = logging.getLogger("commons.reminders")

OVERDUE = "booking.overdue"


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_quiet(now: datetime, settings: bookables.BookingSettings, tz: ZoneInfo) -> bool:
    """Whether `now` is in the community's quiet hours (e.g. 22:00 to 08:00)."""
    t = now.astimezone(tz).time()
    start, end = _hhmm(settings.quiet_hours_start), _hhmm(settings.quiet_hours_end)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def iso(t: datetime) -> str:
    return t.astimezone(ZoneInfo("UTC")).isoformat()


def already_sent(db: Session, booking: Booking) -> bool:
    """Whether this booking's current end already had its reminder."""
    if booking.ends_at is None:
        return False
    found = db.scalar(
        select(EventFeed.id)
        .where(EventFeed.kind == OVERDUE)
        .where(EventFeed.data["booking_id"].as_integer() == booking.id)
        .where(EventFeed.data["due_at"].as_string() == iso(booking.ends_at))
        .limit(1)
    )
    return found is not None


def due(db: Session, now: datetime) -> list[Booking]:
    """Vehicle and tool bookings that are overdue and haven't had their reminder."""
    s = bookables.settings(db)
    after = timedelta(hours=s.reminder_every_hours)
    stop = timedelta(hours=s.reminders_stop_after_hours)
    q = (
        select(Booking)
        .join(Bookable)
        .where(
            Bookable.type != "space",
            Booking.cancelled_at.is_(None),
            Booking.returned_at.is_(None),
            Booking.ends_at.is_not(None),
            Booking.ends_at <= now - after,
            Booking.ends_at > now - stop,
        )
        .order_by(Booking.ends_at, Booking.id)
    )
    return [
        b
        for b in db.scalars(q)
        if b.member.is_active and b.member.matrix_user_id and not already_sent(db, b)
    ]


def send_due(db: Session, now: datetime | None = None) -> int:
    """Write the reminders that are due now; returns how many. Commits."""
    now = now or utcnow()
    if is_quiet(now, bookables.settings(db), cal.timezone(db)):
        return 0
    found = due(db, now)
    for b in found:
        publish(db, Event(OVERDUE, {"booking_id": b.id, "due_at": iso(b.ends_at)}))
    db.commit()  # app.feed writes one event_feed row per reminder in this commit
    return len(found)


def _check(sm: sessionmaker[Session]) -> int:
    with sm() as db:
        return send_due(db)


async def run_forever(sm: sessionmaker[Session], every_seconds: float) -> None:
    """Background loop started by the app's lifespan; one app worker, so one loop."""
    while True:
        try:
            sent = await run_in_threadpool(_check, sm)
            if sent:
                log.info("sent %d overdue reminder(s)", sent)
        except Exception:
            log.exception("overdue reminders failed")
        await asyncio.sleep(every_seconds)
