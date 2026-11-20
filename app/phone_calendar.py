"""Phone calendar: a member's secret ICS link and the file behind it.

Each member can have one link at a time, e.g.
`/calendar/ics/<token>.ics` on the box. The token is the only
permission (calendar apps can't sign in), so it is long and random, only its
SHA-256 is stored, the app's access log never shows it (`redact_access_log`),
and the link stops working the moment it is revoked, replaced or the member is
deactivated. The box is only reachable on the LAN/VPN, so the link only works
for phones that are.

The feed holds only the member's own bookings for now; choosing what it shows
is a later setting. It is written by hand (RFC 5545) instead of with a library:
a few lines of escaping and folding, tested, and no new dependency.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import hash_token
from app.i18n import en
from app.models import Booking, CalendarToken, Member, utcnow

# How far back the feed goes; everything ahead is included.
PAST = timedelta(days=30)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")


# --- the link --------------------------------------------------------------------


def active(db: Session, member: Member) -> CalendarToken | None:
    return db.scalar(
        select(CalendarToken).where(
            CalendarToken.member_id == member.id, CalendarToken.revoked_at.is_(None)
        )
    )


def revoke(db: Session, member: Member, *, now: datetime | None = None) -> bool:
    """Stop the member's link working. True if there was one."""
    token = active(db, member)
    if token is None:
        return False
    token.revoked_at = now or utcnow()
    db.flush()
    return True


def create(db: Session, member: Member, *, now: datetime | None = None) -> str:
    """A new link for `member`; the old one stops working. Returns the raw token (shown once)."""
    if not member.is_active:
        raise ValueError("deactivated members get no calendar link")
    now = now or utcnow()
    revoke(db, member, now=now)
    raw = secrets.token_urlsafe(32)
    db.add(CalendarToken(member_id=member.id, token_hash=hash_token(raw), created_at=now))
    db.flush()
    return raw


def member_for(db: Session, raw: str) -> Member | None:
    """The active member whose active link this is, or None."""
    if not TOKEN_RE.match(raw):
        return None
    token = db.scalar(
        select(CalendarToken).where(
            CalendarToken.token_hash == hash_token(raw), CalendarToken.revoked_at.is_(None)
        )
    )
    if token is None or not token.member.is_active:
        return None
    return token.member


# --- the file --------------------------------------------------------------------


def _escape(value: str) -> str:
    """TEXT escaping (RFC 5545 3.3.11)."""
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space (3.1)."""
    out, current = [], b""
    for ch in line:
        b = ch.encode()
        if len(current) + len(b) > (75 if not out else 74):
            out.append(current.decode())
            current = b""
        current += b
    out.append(current.decode())
    return "\r\n ".join(out)


def _utc(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _shown_end(b: Booking, now: datetime) -> datetime:
    """The end a phone shows: given back, else the end, else open: until now at the earliest."""
    if b.returned_at is not None:
        return b.returned_at
    if b.ends_at is not None:
        return b.ends_at
    return max(b.blocks_until, now)


def bookings_for(db: Session, member: Member, now: datetime) -> list[Booking]:
    """The member's bookings in the feed: not cancelled, from 30 days ago on, or still out."""
    q = select(Booking).where(
        Booking.member_id == member.id,
        Booking.cancelled_at.is_(None),
        or_(Booking.blocks_until >= now - PAST, Booking.returned_at.is_(None)),
    )
    return list(db.scalars(q.order_by(Booking.starts_at, Booking.id)))


def ics(db: Session, member: Member, host: str, *, now: datetime | None = None) -> str:
    """The member's phone calendar as an iCalendar file."""
    now = now or utcnow()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//COMMONS//Bookings//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(en.ICS_CALENDAR_NAME)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]
    for b in bookings_for(db, member, now):
        thing = b.bookable
        summary = thing.name
        if b.destination:
            summary += en.ICS_GOING_TO.format(destination=b.destination)
        if b.ends_at is None and b.returned_at is None:
            summary += en.ICS_OPEN
        description = [b.note] if b.note else []
        description.append(en.ICS_BOOKED_IN)
        changed = b.updated_at or b.created_at
        event = [
            "BEGIN:VEVENT",
            f"UID:booking-{b.id}@{host}",
            f"DTSTAMP:{_utc(now)}",
            f"LAST-MODIFIED:{_utc(changed)}",
            f"DTSTART:{_utc(b.starts_at)}",
            f"DTEND:{_utc(_shown_end(b, now))}",
            f"SUMMARY:{_escape(summary)}",
            f"DESCRIPTION:{_escape(chr(10).join(description))}",
        ]
        if thing.location:
            event.append(f"LOCATION:{_escape(thing.location)}")
        event += ["TRANSP:OPAQUE", "END:VEVENT"]
        lines += event
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


# --- logs ------------------------------------------------------------------------

_IN_PATH = re.compile(r"(/calendar/ics/)[^/?\s]+")


class _RedactToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _IN_PATH.sub(r"\1[token].ics", a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = _IN_PATH.sub(r"\1[token].ics", record.msg)
        return True


def redact_access_log() -> None:
    """Keep calendar tokens out of uvicorn's access log (secrets are never logged)."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactToken) for f in logger.filters):
        logger.addFilter(_RedactToken())
