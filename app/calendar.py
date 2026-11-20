"""Bookings of bookable things (theatre, car, saw).

The web calendar and chat call these functions, with the same checks, as the
member who books. Callers commit; events go out after the commit
(`app.events`), and the COMMONS room gets a line for each (`app.feed`).

Think of a library. A member books a thing now or for later; the end is
optional and only a hint, because COMMONS can't make anyone give a thing back,
it reminds them instead. A booking without an end blocks the thing for the
community's open-booking window (2 hours by default) from its start; after
that the booking stays open, but others can book the thing, and the event
says who still has it so COMMONS can tell them it is needed.

Overlaps are rejected by the database (`ex_booking_overlap` on
[starts_at, blocks_until)), also when two members book at the same moment: one
gets the booking, the other a `Conflict` naming who has it.

COMMONS informs, people decide: a late return is never extended or decided
automatically. A vehicle or tool whose end has passed counts as still with the
member (`who_has`) until they give it back; a space is free again at its end.

Members change, extend, shorten, give back and cancel their own bookings;
admins can change and cancel anyone's, which goes into the decision log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import bookables, community, decision_log
from app.events import Event, publish
from app.i18n import en
from app.models import BOOKING_CHANNELS, OVERLAP_CONSTRAINT, Bookable, Booking, Member, utcnow
from app.textfmt import when_range

MAX_AHEAD = timedelta(days=2 * 366)
# "Now" in a chat message is a little in the past by the time it is booked.
START_GRACE = timedelta(minutes=15)
NOTE_MAX = 200
DESTINATION_MAX = 100
REASON_MAX = 200
_UNSET: object = object()


class BookingError(ValueError):
    """A rule was broken; the message can be shown to the member."""


class NotAllowed(BookingError):
    pass


class Conflict(BookingError):
    """The thing is already booked then; `blocking` are the bookings in the way."""

    def __init__(self, message: str, blocking: list[Booking]) -> None:
        super().__init__(message)
        self.blocking = blocking


@dataclass
class Availability:
    """The bookings that block a time window, and who has the thing when it starts."""

    bookings: list[Booking] = field(default_factory=list)
    holders: list[Booking] = field(default_factory=list)

    @property
    def free(self) -> bool:
        return not self.bookings


# --- checks --------------------------------------------------------------------


def _check_member(member: Member) -> None:
    if not member.is_active:
        raise NotAllowed(en.BOOKING_DEACTIVATED)


def _check_channel(channel: str) -> None:
    if channel not in BOOKING_CHANNELS:
        raise BookingError(en.BOOKING_UNKNOWN_CHANNEL)


def _check_thing(thing: Bookable) -> None:
    if not thing.is_active:
        raise BookingError(en.BOOKING_ARCHIVED.format(name=thing.name))


def _aware(*times: datetime | None) -> None:
    for t in times:
        if t is not None and t.tzinfo is None:
            raise BookingError(en.BOOKING_NO_TIMEZONE)


def _text(value: str | None, limit: int, what: str) -> str | None:
    if value is None:
        return None
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
        raise BookingError(en.BOOKING_ONE_LINE.format(what=what))
    value = " ".join(value.split())
    if len(value) > limit:
        raise BookingError(en.BOOKING_TEXT_TOO_LONG.format(what=what, max=limit))
    return value or None


def _destination(thing: Bookable, value: str | None) -> str | None:
    value = _text(value, DESTINATION_MAX, "destination")
    if value is not None and thing.type != "vehicle":
        raise BookingError(en.BOOKING_DESTINATION_VEHICLES)
    return value


def _check_times(
    starts_at: datetime, ends_at: datetime | None, now: datetime, *, check_start: bool = True
) -> None:
    _aware(starts_at, ends_at)
    if check_start and starts_at < now - START_GRACE:
        raise BookingError(en.BOOKING_IN_THE_PAST)
    if starts_at > now + MAX_AHEAD:
        raise BookingError(en.BOOKING_TOO_FAR)
    if ends_at is not None:
        if ends_at <= starts_at:
            raise BookingError(en.BOOKING_END_BEFORE_START)
        if ends_at <= now:
            raise BookingError(en.BOOKING_END_IN_THE_PAST)


def _blocks_until(db: Session, starts_at: datetime, ends_at: datetime | None) -> datetime:
    if ends_at is not None:
        return ends_at
    return starts_at + timedelta(hours=bookables.settings(db).open_booking_hours)


def _still_open(booking: Booking) -> None:
    if booking.cancelled_at is not None:
        raise BookingError(en.BOOKING_CANCELLED)
    if booking.returned_at is not None:
        raise BookingError(en.BOOKING_RETURNED)


def _is_admin(member: Member) -> bool:
    return "admin" in member.role_names


def _own(booking: Booking, member: Member) -> None:
    _check_member(member)
    if booking.member_id != member.id:
        raise NotAllowed(en.BOOKING_ONLY_YOURS)


def member_name(member: Member) -> str:
    """How a member is named on the calendar and in chat: display name, or "ana" from @ana:…"""
    return member.display_name or member.matrix_user_id.removeprefix("@").split(":", 1)[0]


_name = member_name


def timezone(db: Session) -> ZoneInfo:
    """The community's time zone: days and times are shown and typed in it."""
    return ZoneInfo(community.get(db, community.TIMEZONE_KEY) or "UTC")


_tz = timezone


# --- reading -------------------------------------------------------------------


def _active(thing: Bookable):
    return select(Booking).where(Booking.bookable_id == thing.id, Booking.cancelled_at.is_(None))


def overlapping(
    db: Session, thing: Bookable, starts_at: datetime, ends_at: datetime
) -> list[Booking]:
    """Active bookings of `thing` that block some of [starts_at, ends_at)."""
    q = _active(thing).where(Booking.blocks_until > starts_at, Booking.starts_at < ends_at)
    return list(db.scalars(q.order_by(Booking.starts_at, Booking.id)))


def who_has(db: Session, thing: Bookable, at: datetime | None = None) -> list[Booking]:
    """Who has `thing` at `at`: started, not given back, and not over.

    A booking without an end lasts until it is given back. A vehicle or tool
    past its end is still with the member until they give it back; a space is
    free again at its end. Usually one booking or none; two when someone still
    has it and the next booking has started, and both should hear about it.
    """
    at = at or utcnow()
    q = _active(thing).where(Booking.starts_at <= at, Booking.returned_at.is_(None))
    if thing.type == "space":
        q = q.where(or_(Booking.ends_at.is_(None), Booking.ends_at > at))
    return list(db.scalars(q.order_by(Booking.starts_at, Booking.id)))


def availability(
    db: Session, thing: Bookable, starts_at: datetime, ends_at: datetime | None = None
) -> Availability:
    """Can `thing` be booked from `starts_at` (until `ends_at`, or with no end)?"""
    _aware(starts_at, ends_at)
    if ends_at is not None and ends_at <= starts_at:
        raise BookingError(en.BOOKING_END_BEFORE_START)
    blocks = _blocks_until(db, starts_at, ends_at)
    return Availability(
        bookings=overlapping(db, thing, starts_at, blocks),
        holders=who_has(db, thing, starts_at),
    )


def next_booking(db: Session, thing: Bookable, after: datetime) -> Booking | None:
    """The first active booking of `thing` that starts after `after`."""
    return db.scalars(
        _active(thing).where(Booking.starts_at > after).order_by(Booking.starts_at).limit(1)
    ).first()


def get(db: Session, booking_id: int) -> Booking | None:
    return db.get(Booking, booking_id)


def day_range(first: date, last: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Whole days, both included, in the community's time zone, as UTC.

    01.12.26 to 13.12.26 = from midnight on 1 Dec to midnight at the end of 13 Dec.
    """
    if last < first:
        raise BookingError(en.BOOKING_LAST_BEFORE_FIRST)
    start = datetime.combine(first, time(0), tz)
    end = datetime.combine(last + timedelta(days=1), time(0), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


# --- writing -------------------------------------------------------------------


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).isoformat()


def _event(kind: str, booking: Booking, **extra) -> Event:
    return Event(
        kind,
        {
            "booking_id": booking.id,
            "bookable_id": booking.bookable_id,
            "member_id": booking.member_id,
            "starts_at": _iso(booking.starts_at),
            "ends_at": _iso(booking.ends_at),
            "channel": booking.channel,
            **extra,
        },
    )


def _conflict(db: Session, thing: Bookable, starts_at: datetime, blocks_until: datetime, own_id):
    with db.no_autoflush:  # the refused change must not be written again by the query
        blocking = [b for b in overlapping(db, thing, starts_at, blocks_until) if b.id != own_id]
    first = blocking[0] if blocking else None
    if first is not None and first.ends_at is None:
        since = when_range(first.starts_at, None, _tz(db)).removeprefix("from ")
        message = en.BOOKING_WITH.format(name=thing.name, who=_name(first.member), since=since)
    else:
        message = en.BOOKING_CONFLICT.format(name=thing.name)
    return Conflict(message, blocking)


def _write(db: Session, booking: Booking, apply, starts_at, blocks_until) -> None:
    """Apply and write a booking in a savepoint; the exclusion constraint decides overlaps.

    `apply` makes the change inside the savepoint: `begin_nested` writes pending
    changes before it starts, so a change made earlier would not be undone.
    """
    try:
        with db.begin_nested():
            apply()
            db.flush()
    except IntegrityError as e:
        if getattr(getattr(e.orig, "diag", None), "constraint_name", None) != OVERLAP_CONSTRAINT:
            raise
        raise _conflict(db, booking.bookable, starts_at, blocks_until, booking.id) from None


def _still_with(db: Session, booking: Booking, now: datetime) -> list[int]:
    """Who has the thing now with no end, or past their end, so COMMONS can tell them."""
    return [
        b.id
        for b in who_has(db, booking.bookable, now)
        if b.id != booking.id and (b.ends_at is None or b.ends_at <= now)
    ]


def book(
    db: Session,
    member: Member,
    thing: Bookable,
    starts_at: datetime,
    ends_at: datetime | None = None,
    *,
    channel: str,
    note: str | None = None,
    destination: str | None = None,
    now: datetime | None = None,
) -> Booking:
    """Book `thing` as `member` from `starts_at`, until `ends_at` (a hint) or with no end."""
    now = now or utcnow()
    _check_member(member)
    _check_channel(channel)
    _check_thing(thing)
    _check_times(starts_at, ends_at, now)
    booking = Booking(
        bookable=thing,
        member_id=member.id,
        starts_at=starts_at,
        ends_at=ends_at,
        blocks_until=_blocks_until(db, starts_at, ends_at),
        note=_text(note, NOTE_MAX, "note"),
        destination=_destination(thing, destination),
        channel=channel,
        created_at=now,
    )
    _write(db, booking, lambda: db.add(booking), booking.starts_at, booking.blocks_until)
    following = next_booking(db, thing, booking.starts_at)
    publish(
        db,
        _event(
            "booking.created",
            booking,
            still_with=_still_with(db, booking, now),
            next_booking_id=following.id if following else None,
        ),
    )
    return booking


def _details(booking: Booking) -> dict[str, str | None]:
    return {
        "starts_at": _iso(booking.starts_at),
        "ends_at": _iso(booking.ends_at),
        "note": booking.note,
        "destination": booking.destination,
    }


def change(
    db: Session,
    actor: Member,
    booking: Booking,
    *,
    starts_at: datetime | object = _UNSET,
    ends_at: datetime | object | None = _UNSET,
    note: str | object | None = _UNSET,
    destination: str | object | None = _UNSET,
    signer: decision_log.Signer | None = None,
    now: datetime | None = None,
) -> Booking:
    """Change a booking: the member's own, or anyone's by an admin (logged, with `signer`).

    Only what is passed changes. An end can move later too, while nobody else
    has booked that time; `ends_at=None` removes it. A started booking keeps its start.
    """
    now = now or utcnow()
    _check_member(actor)
    by_admin = booking.member_id != actor.id
    if by_admin and not _is_admin(actor):
        raise NotAllowed(en.BOOKING_ONLY_YOURS)
    if by_admin and signer is None:
        raise decision_log.DecisionLogError("an admin change needs the decision-log signer")
    _still_open(booking)
    thing = booking.bookable
    old = _details(booking)

    new_start = booking.starts_at if starts_at is _UNSET else starts_at
    new_end = booking.ends_at if ends_at is _UNSET else ends_at
    started = booking.starts_at <= now
    if started and new_start != booking.starts_at:
        raise BookingError(en.BOOKING_START_FIXED)
    _check_times(new_start, new_end, now, check_start=not started)
    new_note = booking.note if note is _UNSET else _text(note, NOTE_MAX, "note")
    new_destination = (
        booking.destination if destination is _UNSET else _destination(thing, destination)
    )
    if (new_start, new_end, new_note, new_destination) == (
        booking.starts_at,
        booking.ends_at,
        booking.note,
        booking.destination,
    ):
        return booking
    blocks = booking.blocks_until
    if (new_start, new_end) != (booking.starts_at, booking.ends_at):
        blocks = _blocks_until(db, new_start, new_end)

    def apply() -> None:
        booking.starts_at, booking.ends_at, booking.blocks_until = new_start, new_end, blocks
        booking.note, booking.destination = new_note, new_destination
        booking.updated_at, booking.updated_by = now, actor.id

    _write(db, booking, apply, new_start, blocks)
    new = _details(booking)
    if by_admin:
        decision_log.append(
            db,
            signer,
            "booking.changed_by_admin",
            {
                "booking_id": booking.id,
                "bookable": thing.slug,
                "member_id": booking.member_id,
                "old": old,
                "new": new,
            },
            actor=actor.id,
        )
    publish(
        db,
        _event(
            "booking.changed",
            booking,
            by_admin=by_admin,
            changed_by=actor.id,
            old=old,
            still_with=_still_with(db, booking, now),
        ),
    )
    return booking


def return_item(
    db: Session, member: Member, booking: Booking, *, now: datetime | None = None
) -> Booking:
    """Give back `member`'s own started booking.

    `returned_at` says when it came back; the end the member gave stays as it
    was (a hint), and the thing is free for others from now on.
    """
    now = now or utcnow()
    _own(booking, member)
    _still_open(booking)
    if booking.starts_at > now:
        raise BookingError(en.BOOKING_NOT_STARTED)
    late = booking.ends_at is not None and booking.ends_at < now
    booking.returned_at = now
    # The end stays what the member said; the thing is free again from now.
    booking.blocks_until = min(
        booking.blocks_until, max(now, booking.starts_at + timedelta(seconds=1))
    )
    db.flush()
    following = next_booking(db, booking.bookable, now)
    publish(
        db,
        _event(
            "booking.returned",
            booking,
            late=late,
            returned_at=_iso(now),
            next_booking_id=following.id if following else None,
        ),
    )
    return booking


def cancel(db: Session, member: Member, booking: Booking, *, now: datetime | None = None) -> None:
    """Cancel `member`'s own booking before it starts (after that: give it back)."""
    now = now or utcnow()
    _own(booking, member)
    _still_open(booking)
    if booking.starts_at <= now:
        raise BookingError(en.BOOKING_STARTED)
    booking.cancelled_at = now
    booking.cancelled_by = member.id
    db.flush()
    publish(db, _event("booking.cancelled", booking, by_admin=False))


def admin_cancel(
    db: Session,
    admin: Member,
    booking: Booking,
    reason: str,
    signer: decision_log.Signer,
    *,
    now: datetime | None = None,
) -> None:
    """An admin cancels any booking, with a reason; recorded in the decision log."""
    now = now or utcnow()
    _check_member(admin)
    if not _is_admin(admin):
        raise NotAllowed(en.BOOKING_ADMINS_ONLY)
    reason = _text(reason, REASON_MAX, "reason") or ""
    if not reason:
        raise BookingError(en.BOOKING_NEEDS_REASON)
    _still_open(booking)
    booking.cancelled_at = now
    booking.cancelled_by = admin.id
    booking.cancel_reason = reason
    db.flush()
    decision_log.append(
        db,
        signer,
        "booking.cancelled_by_admin",
        {
            "booking_id": booking.id,
            "bookable": booking.bookable.slug,
            "member_id": booking.member_id,
            "starts_at": _iso(booking.starts_at),
            "ends_at": _iso(booking.ends_at),
            "reason": reason,
        },
        actor=admin.id,
    )
    publish(db, _event("booking.cancelled", booking, by_admin=True, reason=reason))
