"""What the calendar page shows: days, and the bookings on each day.

Pure reading; the page (app/calendar_page.py) renders it. All days are days in
the community's time zone, so a booking from 23:00 to 01:00 shows on both days,
and the daylight-saving switches don't shift anything.

A booking shows from its start until it was given back, or its end, or (no end
and still out) until now at the earliest; cancelled bookings don't show. There
are no private bookings: everyone sees the thing, the member, the note and, for
vehicles, where to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import bookables
from app import calendar as cal
from app.models import THING_TYPES, Bookable, Booking, utcnow

VIEWS = ("month", "week")
PER_DAY = 3  # month view: more than this become "+n more" (two-line chips)


@dataclass
class Item:
    """One booking on one day."""

    booking: Booking
    starts_today: bool
    ends_today: bool
    start: datetime  # local
    end: datetime  # local, what is shown (see shown_end)
    holding: bool  # started, not given back, no end or past its end: "with Ana since …"
    late: bool  # past its end and not given back (vehicles, tools)


@dataclass
class Day:
    day: date
    in_month: bool
    is_today: bool
    items: list[Item] = field(default_factory=list)

    @property
    def shown(self) -> list[Item]:
        return self.items[:PER_DAY]

    @property
    def more(self) -> int:
        return max(0, len(self.items) - PER_DAY)


@dataclass
class Filter:
    """Which bookings to show: all, one type, or one thing."""

    type: str | None = None
    thing: Bookable | None = None

    def query(self) -> dict[str, str]:
        if self.thing is not None:
            return {"thing": self.thing.slug}
        if self.type is not None:
            return {"type": self.type}
        return {}


def make_filter(db: Session, type_: str | None, thing: str | None) -> Filter:
    """Unknown values are ignored, not errors: an old bookmark still opens the calendar."""
    if thing:
        found = bookables.by_slug(db, thing)
        if found is not None:
            return Filter(thing=found)
    if type_ in THING_TYPES:
        return Filter(type=type_)
    return Filter()


def shown_end(b: Booking, now: datetime) -> datetime:
    """Given back: then. An end: the end, or now if it is past and the thing is still out
    (vehicles, tools). No end: the blocked window, or now if it is still out after that."""
    if b.returned_at is not None:
        return b.returned_at
    out = b.starts_at <= now
    if b.ends_at is not None:
        if out and b.ends_at < now and b.bookable.type != "space":
            return now
        return b.ends_at
    return max(b.blocks_until, now) if out else b.blocks_until


def is_holding(b: Booking, now: datetime) -> bool:
    """Someone has it now with no end, or after their end: "with Ana since 14:00"."""
    if b.returned_at is not None or b.cancelled_at is not None or b.starts_at > now:
        return False
    if b.ends_at is None:
        return True
    return b.ends_at <= now and b.bookable.type != "space"


def is_late(b: Booking, now: datetime) -> bool:
    return is_holding(b, now) and b.ends_at is not None


def local_range(first: date, last: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[first 00:00, last+1 00:00) in local time, as UTC."""
    return cal.day_range(first, last, tz)


def bookings_between(
    db: Session, start: datetime, end: datetime, flt: Filter, now: datetime
) -> list[Booking]:
    """Bookings that show somewhere in [start, end): not cancelled, overlapping."""
    q = (
        select(Booking)
        .join(Booking.bookable)
        .where(Booking.cancelled_at.is_(None), Booking.starts_at < end)
        .where(
            or_(
                Booking.blocks_until > start,
                Booking.ends_at > start,
                # Still out: shows up to now.
                Booking.returned_at.is_(None) & (Booking.starts_at <= now),
            )
        )
    )
    if flt.thing is not None:
        q = q.where(Booking.bookable_id == flt.thing.id)
    elif flt.type is not None:
        q = q.where(Bookable.type == flt.type)
    found = db.scalars(q.order_by(Booking.starts_at, Booking.id)).unique()
    return [b for b in found if shown_end(b, now) > start]


def days(first: date, last: date, month: int | None, today: date) -> list[Day]:
    out, d = [], first
    while d <= last:
        out.append(Day(d, month is None or d.month == month, d == today))
        d += timedelta(days=1)
    return out


def place(days_: list[Day], found: list[Booking], tz: ZoneInfo, now: datetime) -> None:
    """Put each booking on every day it touches."""
    by_day = {d.day: d for d in days_}
    for b in found:
        start = b.starts_at.astimezone(tz)
        end = shown_end(b, now).astimezone(tz)
        last = (end - timedelta(microseconds=1)).date() if end > start else start.date()
        d = start.date()
        while d <= last:
            if d in by_day:
                by_day[d].items.append(
                    Item(
                        booking=b,
                        starts_today=d == start.date(),
                        ends_today=d == last,
                        start=start,
                        end=end,
                        holding=is_holding(b, now),
                        late=is_late(b, now),
                    )
                )
            d += timedelta(days=1)


@dataclass
class View:
    kind: str  # month | week
    anchor: date
    first: date
    last: date
    weeks: list[list[Day]]
    prev: date
    next: date
    flt: Filter
    holders: list[Booking]  # who has what right now

    @property
    def title_month(self) -> str:
        return f"{self.anchor:%B %Y}"


def build(
    db: Session,
    kind: str,
    anchor: date,
    flt: Filter,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> View:
    now = now or utcnow()
    today = now.astimezone(tz).date()
    if kind == "week":
        first = anchor - timedelta(days=anchor.weekday())
        last = first + timedelta(days=6)
        month = None
        prev, nxt = anchor - timedelta(days=7), anchor + timedelta(days=7)
    else:
        start_of_month = anchor.replace(day=1)
        next_month = (start_of_month + timedelta(days=32)).replace(day=1)
        first = start_of_month - timedelta(days=start_of_month.weekday())
        end_of_month = next_month - timedelta(days=1)
        last = end_of_month + timedelta(days=6 - end_of_month.weekday())
        month = anchor.month
        prev = (start_of_month - timedelta(days=1)).replace(day=1)
        nxt = next_month
    all_days = days(first, last, month, today)
    start, end = local_range(first, last, tz)
    place(all_days, bookings_between(db, start, end, flt, now), tz, now)
    weeks = [all_days[i : i + 7] for i in range(0, len(all_days), 7)]
    holders = [
        b
        for t in bookables.list_active(db)
        if (flt.thing is None or t.id == flt.thing.id) and (flt.type in (None, t.type))
        for b in cal.who_has(db, t, now)
        if is_holding(b, now)
    ]
    return View(kind, anchor, first, last, weeks, prev, nxt, flt, holders)


def parse_day(raw: str | None, today: date) -> date:
    """`2026-11-16` or today; nonsense is today, not an error."""
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return today
