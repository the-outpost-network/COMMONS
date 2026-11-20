"""Bookable (theatre, car, saw) and the community's booking settings.

A bookable thing has a name, a type (space, vehicle or tool), where it is, and a
description. The type decides what a booking of it asks for: a vehicle booking
can say where the member is going. The id (slug) is made from the name when the
thing is created and never changes, so the decision log can refer to it.
Active names are unique (any case), so chat can name a thing without
ambiguity. A thing with bookings is archived, never deleted: `delete` only
works while nothing refers to it, and the database enforces that.

The booking settings (`BookingSettings`, config key `bookings`) are community
settings, not constants: how long a booking without an end blocks the thing,
and when COMMONS reminds people.

The community.yaml seed and the admin page (app/admin_bookable.py) go through
`ThingSpec` and the functions here. Callers check permissions, log
`config.changed` (`values()` is what gets logged) and commit.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import THING_TYPES, Bookable, ConfigKV, utcnow

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SETTINGS_KEY = "bookings"
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class BookablesError(ValueError):
    """A rule was broken; the message can be shown to an admin.

    ``field`` names the form field it belongs to, if any.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class HasHistory(BookablesError):
    pass


def _one_line(v: str) -> str:
    v = " ".join(v.split())
    if any(ord(ch) < 32 for ch in v):
        raise ValueError("must be one line of text")
    return v


class ThingSpec(BaseModel):
    """One bookable thing as written in community.yaml or sent by the admin form."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    type: str
    location: str | None = Field(default=None, max_length=100)
    description: str = Field(default="", max_length=500)
    # Only for things carried over from an older community.yaml (`spaces:`).
    slug: str | None = Field(default=None, max_length=32)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _one_line(v)

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in THING_TYPES:
            raise ValueError(f"must be {', '.join(THING_TYPES)}")
        return v

    @field_validator("location")
    @classmethod
    def _location(cls, v: str | None) -> str | None:
        return (_one_line(v) or None) if v is not None else None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str | None) -> str | None:
        if v is not None and not SLUG_RE.match(v):
            raise ValueError(
                "use 2 to 32 lower-case letters, digits or '-', starting with a letter"
            )
        return v


class BookingSettings(BaseModel):
    """How bookings behave; community settings (config key `bookings`)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # A booking without an end blocks the thing this long from its start.
    open_booking_hours: int = Field(default=2, ge=1, le=24)
    # Remind the holder this often: after the start (no end) or after the end.
    reminder_every_hours: int = Field(default=2, ge=1, le=24)
    # Stop reminding after this long and tell the admins instead.
    reminders_stop_after_hours: int = Field(default=24, ge=1, le=24 * 14)
    # No messages from COMMONS between these times (community time zone).
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"

    @field_validator("quiet_hours_start", "quiet_hours_end", mode="before")
    @classmethod
    def _hhmm(cls, v: Any) -> Any:
        if not isinstance(v, str) or not HHMM_RE.match(v.strip()):
            raise ValueError('use a time like "22:00", in quotes')
        return v


def settings(db: Session) -> BookingSettings:
    """The community's booking settings (the defaults until someone sets them)."""
    row = db.get(ConfigKV, SETTINGS_KEY)
    return BookingSettings() if row is None else BookingSettings.model_validate(row.value_json)


# --- writing -------------------------------------------------------------------


def slug_for(db: Session, name: str) -> str:
    """A fixed id made from the name: "Big saw" -> "big-saw", unique ("saw-2")."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:28].strip("-")
    if not base or not base[0].isalpha():
        base = f"thing-{base}".strip("-")[:28]
    taken = set(db.scalars(select(Bookable.slug)))
    slug, n = base, 2
    while slug in taken or not SLUG_RE.match(slug):
        slug, n = f"{base}-{n}", n + 1
    return slug


def _name_taken(db: Session, name: str, exclude_id: int | None) -> None:
    for other in db.scalars(select(Bookable).where(Bookable.archived_at.is_(None))):
        if other.id != exclude_id and other.name.casefold() == name.casefold():
            raise BookablesError(f"another thing is already called {other.name!r}", "name")


def _apply(thing: Bookable, spec: ThingSpec) -> None:
    thing.name = spec.name
    thing.type = spec.type
    thing.location = spec.location
    thing.description = spec.description


def create(db: Session, spec: ThingSpec) -> Bookable:
    _name_taken(db, spec.name, None)
    if spec.slug is not None and db.scalar(select(Bookable.id).where(Bookable.slug == spec.slug)):
        raise BookablesError(f"{spec.slug!r} already exists", "slug")
    position = (db.scalar(select(func.max(Bookable.position))) or 0) + 1
    thing = Bookable(slug=spec.slug or slug_for(db, spec.name), position=position)
    _apply(thing, spec)
    db.add(thing)
    db.flush()
    return thing


def update(db: Session, thing: Bookable, spec: ThingSpec) -> None:
    """Change everything except the id, which stays what it was."""
    _name_taken(db, spec.name, thing.id)
    _apply(thing, spec)
    db.flush()


def archive(db: Session, thing: Bookable) -> None:
    """Stop it being booked; its bookings stay."""
    if thing.archived_at is None:
        thing.archived_at = utcnow()
        db.flush()


def restore(db: Session, thing: Bookable) -> None:
    if thing.archived_at is None:
        return
    # Its name may have been taken in the meantime.
    for other in db.scalars(select(Bookable).where(Bookable.archived_at.is_(None))):
        if other.name.casefold() == thing.name.casefold():
            raise BookablesError(f"another thing is already called {other.name!r}")
    thing.archived_at = None
    db.flush()


def delete(db: Session, thing: Bookable) -> None:
    """Remove it for good, only while nobody has booked it."""
    try:
        with db.begin_nested():
            db.delete(thing)
            db.flush()
    except IntegrityError as e:
        raise HasHistory(f"{thing.name} has bookings; archive it instead") from e


def values(thing: Bookable) -> dict[str, Any]:
    """What the decision log records about a thing (`config.changed` old and new)."""
    return {
        "slug": thing.slug,
        "name": thing.name,
        "type": thing.type,
        "location": thing.location,
        "description": thing.description,
        "archived": thing.archived_at is not None,
    }


# --- reading -------------------------------------------------------------------


def get(db: Session, thing_id: int) -> Bookable | None:
    return db.get(Bookable, thing_id)


def by_slug(db: Session, slug: str) -> Bookable | None:
    return db.scalar(select(Bookable).where(Bookable.slug == slug))


def list_all(db: Session) -> list[Bookable]:
    """Every thing, active first, each group in display order."""
    return list(
        db.scalars(
            select(Bookable).order_by(
                Bookable.archived_at.is_not(None), Bookable.position, Bookable.id
            )
        )
    )


def list_active(db: Session) -> list[Bookable]:
    """Things members can book now, in display order."""
    return [t for t in list_all(db) if t.is_active]
