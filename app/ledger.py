"""The resource ledger: the one code path that writes resource data.

Chat (D3), the web board and later sensors all call these functions, with the
same checks, as the member who logged. Callers commit; events go out after the
commit (`app.events`).

Rules:
- amounts are numbers >= 0 (a reading may be 0, an empty tank; use and add > 0)
  and at most a sane bound: twice the capacity, or 1,000,000 without one;
- an entry may be up to 30 days in the past, never in the future (5 minutes
  of leeway for clocks that run a little fast);
- the entry type must fit the resource: a "flow" resource has no readings;
- deactivated members and archived resources are refused;
- the author may edit an entry for 15 minutes; afterwards the author or an
  admin adds a `correction` row that points at the original. Nothing is ever
  deleted (the database refuses it too);
- "undo" is a `void` correction: the entry stays in the history but no longer
  counts. `fix_entry` picks edit or correction, so chat and web share the rule.

Levels are derived, never stored: for a "stock" resource the latest reading
plus the adds minus the uses after it, each entry counted with the amount of
its latest correction (a void one drops the entry). Until the first reading
the level counts from zero (adds minus uses), and says so (`from_zero`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import categories
from app.events import Event, publish
from app.models import (
    EDIT_WINDOW_MINUTES,
    ENTRY_CHANNELS,
    Member,
    ResourceCategory,
    ResourceEntry,
    utcnow,
)

EDIT_WINDOW = timedelta(minutes=EDIT_WINDOW_MINUTES)
MAX_PAST = timedelta(days=30)
CLOCK_LEEWAY = timedelta(minutes=5)
DEFAULT_MAX_AMOUNT = 1_000_000.0
NOTE_MAX = 140
FLOW_WINDOW = timedelta(days=7)

TYPES_FOR_KIND = {"stock": ("reading", "use", "add"), "flow": ("use", "add")}


class LedgerError(ValueError):
    """A rule was broken; the message can be shown to the member."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class NotAllowed(LedgerError):
    pass


def parse_amount(raw: str) -> float:
    """A typed number: "800", "2.5", "2,5" (decimal comma), "1,500" (thousands)."""
    text = raw.strip().replace(" ", "").replace("\u202f", "")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d+,\d+", text):
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError as e:
        raise LedgerError("the amount must be a number", field="amount") from e


def max_amount(category: ResourceCategory) -> float:
    return category.capacity * 2 if category.capacity else DEFAULT_MAX_AMOUNT


def _check_amount(
    category: ResourceCategory, entry_type: str, amount: float, *, correction: bool = False
) -> float:
    try:
        amount = float(amount)
    except (TypeError, ValueError) as e:
        raise LedgerError("the amount must be a number", field="amount") from e
    if not math.isfinite(amount) or amount < 0:
        raise LedgerError("the amount must be a positive number", field="amount")
    # A correction to 0 says "this use or refill didn't happen".
    if amount == 0 and entry_type in ("use", "add") and not correction:
        raise LedgerError("the amount must be more than 0", field="amount")
    if amount > max_amount(category):
        raise LedgerError(
            f"{amount:g} {category.unit} is more than {category.name} can hold; please check",
            field="amount",
        )
    return amount


def _check_time(occurred_at: datetime | None, now: datetime) -> datetime:
    if occurred_at is None:
        return now
    if occurred_at.tzinfo is None:
        raise LedgerError("time without a timezone", field="when")
    if occurred_at > now + CLOCK_LEEWAY:
        raise LedgerError("the time can't be in the future", field="when")
    if occurred_at < now - MAX_PAST:
        raise LedgerError("entries can go back at most 30 days", field="when")
    return occurred_at


def _check_note(note: str | None) -> str | None:
    if note is None:
        return None
    note = " ".join(note.split())
    if len(note) > NOTE_MAX:
        raise LedgerError(f"the note can be at most {NOTE_MAX} characters", field="note")
    return note or None


def _check_member(member: Member) -> None:
    if not member.is_active:
        raise NotAllowed("your membership is deactivated")


def _event(kind: str, entry: ResourceEntry, **extra) -> Event:
    return Event(
        kind,
        {
            "entry_id": entry.id,
            "category_id": entry.category_id,
            "member_id": entry.member_id,
            "entry_type": entry.entry_type,
            "amount": entry.amount,
            "channel": entry.channel,
            # A correction: the entry it fixes, and whether it undoes it.
            "corrects_id": entry.corrects_id,
            "void": entry.void,
            **extra,
        },
    )


# --- writing -----------------------------------------------------------------


def add_entry(
    db: Session,
    member: Member,
    category: ResourceCategory,
    entry_type: str,
    amount: float,
    *,
    channel: str,
    note: str | None = None,
    occurred_at: datetime | None = None,
    now: datetime | None = None,
) -> ResourceEntry:
    """Log a reading, use or add for `category` as `member`."""
    now = now or utcnow()
    _check_member(member)
    if channel not in ENTRY_CHANNELS:
        raise LedgerError("unknown channel")
    if not category.is_active:
        raise LedgerError(f"{category.name} is archived")
    allowed = TYPES_FOR_KIND[category.kind]
    if entry_type not in allowed:
        raise LedgerError(f"{category.name} takes {', '.join(allowed)} entries")
    entry = ResourceEntry(
        category_id=category.id,
        member_id=member.id,
        channel=channel,
        entry_type=entry_type,
        amount=_check_amount(category, entry_type, amount),
        note=_check_note(note),
        occurred_at=_check_time(occurred_at, now),
        created_at=now,
    )
    db.add(entry)
    db.flush()
    publish(db, _event("ledger.entry_added", entry))
    return entry


def can_edit(entry: ResourceEntry, member: Member, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return entry.member_id == member.id and now - entry.created_at < EDIT_WINDOW


def edit_entry(
    db: Session,
    member: Member,
    entry: ResourceEntry,
    *,
    amount: float | None = None,
    note: str | None = None,
    occurred_at: datetime | None = None,
    now: datetime | None = None,
) -> None:
    """Change your own entry within 15 minutes of logging it."""
    now = now or utcnow()
    _check_member(member)
    if entry.member_id != member.id:
        raise NotAllowed("only the person who logged an entry can edit it")
    if now - entry.created_at >= EDIT_WINDOW:
        raise NotAllowed(
            f"entries can be edited for {EDIT_WINDOW_MINUTES} minutes; add a correction instead"
        )
    kind = entry.corrects.entry_type if entry.corrects else entry.entry_type
    old_amount = entry.amount
    if amount is not None:
        entry.amount = _check_amount(entry.category, kind, amount)
    if note is not None:
        entry.note = _check_note(note)
    if occurred_at is not None:
        entry.occurred_at = _check_time(occurred_at, now)
    entry.edited_at = now
    db.flush()
    publish(db, _event("ledger.entry_edited", entry, old_amount=old_amount))


def correct_entry(
    db: Session,
    member: Member,
    entry: ResourceEntry,
    amount: float,
    *,
    channel: str,
    note: str | None = None,
    void: bool = False,
    now: datetime | None = None,
) -> ResourceEntry:
    """Add a correction: from now on `entry` counts with `amount`.

    The author or an admin. A correction of a correction corrects the original.
    A use or add can be corrected to 0: it didn't happen. `void=True` undoes the
    entry whatever its type (a reading of 0 would be an empty tank); `amount`
    is then ignored.
    """
    now = now or utcnow()
    _check_member(member)
    original = entry.corrects or entry
    if original.member_id != member.id and "admin" not in member.role_names:
        raise NotAllowed("only the person who logged an entry or an admin can correct it")
    if channel not in ENTRY_CHANNELS:
        raise LedgerError("unknown channel")
    correction = ResourceEntry(
        category_id=original.category_id,
        member_id=member.id,
        channel=channel,
        entry_type="correction",
        amount=0.0
        if void
        else _check_amount(original.category, original.entry_type, amount, correction=True),
        note=_check_note(note),
        occurred_at=now,
        created_at=now,
        corrects_id=original.id,
        void=void,
    )
    db.add(correction)
    db.flush()
    publish(db, _event("ledger.entry_corrected", correction))
    return correction


def latest_correction(db: Session, entry: ResourceEntry) -> ResourceEntry | None:
    """The correction that decides what `entry` (an original) counts for now."""
    return db.scalars(
        select(ResourceEntry)
        .where(ResourceEntry.corrects_id == entry.id)
        .order_by(ResourceEntry.created_at.desc(), ResourceEntry.id.desc())
        .limit(1)
    ).first()


def effective_amount(db: Session, entry: ResourceEntry) -> float | None:
    """What an original entry counts for now; None once it is undone."""
    fix = latest_correction(db, entry)
    if fix is None:
        return entry.amount
    return None if fix.void else fix.amount


def last_entry(
    db: Session, member: Member, category: ResourceCategory | None = None
) -> ResourceEntry | None:
    """The newest original entry `member` logged (of `category`, if given)."""
    q = select(ResourceEntry).where(
        ResourceEntry.member_id == member.id, ResourceEntry.entry_type != "correction"
    )
    if category is not None:
        q = q.where(ResourceEntry.category_id == category.id)
    return db.scalars(
        q.order_by(ResourceEntry.created_at.desc(), ResourceEntry.id.desc()).limit(1)
    ).first()


@dataclass(frozen=True)
class Fix:
    """What `fix_entry` did, for the reply: the entry and its effect before/after."""

    entry: ResourceEntry  # the original
    before: float | None  # None: it was already undone
    after: float | None  # None: undone now
    mode: str  # "edited" (inside the window) / "corrected" / "undone"
    row: ResourceEntry  # the edited entry or the new correction


def fix_entry(
    db: Session,
    member: Member,
    entry: ResourceEntry,
    amount: float | None,
    *,
    channel: str,
    now: datetime | None = None,
) -> Fix:
    """Fix a wrong entry: `amount` is what it should have been, None = undo.

    The author, within the edit window and while nobody has corrected it yet,
    edits the entry itself. Otherwise (later, or an admin fixing someone else's
    entry) a correction row is added and the original stays as it was. Undo is
    always a void correction, so the entry stays visible in the history.
    """
    now = now or utcnow()
    _check_member(member)
    original = entry.corrects or entry
    if original.member_id != member.id and "admin" not in member.role_names:
        raise NotAllowed("only the person who logged an entry or an admin can correct it")
    fix = latest_correction(db, original)
    before = original.amount if fix is None else (None if fix.void else fix.amount)
    if amount is None:
        if before is None:
            raise LedgerError(f"#{original.id} is already undone")
        row = correct_entry(db, member, original, 0, channel=channel, void=True, now=now)
        return Fix(original, before, None, "undone", row)
    amount = _check_amount(original.category, original.entry_type, amount, correction=True)
    if before is not None and amount == before:
        raise LedgerError(f"#{original.id} already counts as {amount:g} {original.category.unit}")
    zero_use = amount == 0 and original.entry_type in ("use", "add")
    if fix is None and not zero_use and can_edit(original, member, now):
        edit_entry(db, member, original, amount=amount, now=now)
        return Fix(original, before, amount, "edited", original)
    row = correct_entry(db, member, original, amount, channel=channel, now=now)
    return Fix(original, before, amount, "corrected", row)


# --- reading -----------------------------------------------------------------


@dataclass(frozen=True)
class Counted:
    """An original entry with the amount it counts for (after corrections)."""

    entry: ResourceEntry
    amount: float
    correction: ResourceEntry | None

    @property
    def corrected(self) -> bool:
        return self.correction is not None

    @property
    def void(self) -> bool:
        """Undone: still in the history, but it no longer counts."""
        return self.correction is not None and self.correction.void


@dataclass(frozen=True)
class Level:
    category: ResourceCategory
    # stock: the derived level; None while nothing is logged. Without a reading
    # it counts from zero (see from_zero).
    value: float | None
    # The reading the level starts from (None: counted from zero), and the
    # newest entry of any kind.
    reading: ResourceEntry | None
    latest: ResourceEntry | None
    # flow: total used in the last 7 days.
    used_recently: float | None = None

    @property
    def from_zero(self) -> bool:
        """Counted from 0: adds minus uses, because nobody has logged a reading yet."""
        return self.value is not None and self.reading is None

    @property
    def below_threshold(self) -> bool:
        t = self.category.shortage_threshold
        return self.value is not None and t is not None and self.value < t

    @property
    def fill(self) -> float | None:
        """0..1 for the level bar, when the capacity is known."""
        if self.value is None or not self.category.capacity:
            return None
        return max(0.0, min(1.0, self.value / self.category.capacity))


def counted_entries(db: Session, category: ResourceCategory) -> list[Counted]:
    """Original entries of `category` in time order, each with its effective amount."""
    rows = db.scalars(
        select(ResourceEntry)
        .where(ResourceEntry.category_id == category.id)
        .order_by(ResourceEntry.created_at, ResourceEntry.id)
    ).all()
    latest_fix: dict[int, ResourceEntry] = {}
    originals = []
    for r in rows:
        if r.entry_type == "correction":
            latest_fix[r.corrects_id] = r  # later corrections win
        else:
            originals.append(r)
    counted = []
    for e in originals:
        fix = latest_fix.get(e.id)
        amount = e.amount if fix is None else (0.0 if fix.void else fix.amount)
        counted.append(Counted(e, amount, fix))
    counted.sort(key=lambda c: (c.entry.occurred_at, c.entry.id))
    return counted


def stock_steps(counted: list[Counted]) -> list[tuple[Counted, float]]:
    """Each entry that still counts, with the stock level right after it.

    The one place the level is derived: `level()` takes the last step and the
    admin history (app/history.py) draws all of them, so the two can never
    disagree. A reading sets the level; until the first reading it counts from
    zero, so the first refills show up. Then adds go up and uses go down.
    """
    value = 0.0
    steps = []
    for c in counted:
        if c.void:
            continue
        kind = c.entry.entry_type
        if kind == "reading":
            value = c.amount
        elif kind == "add":
            value += c.amount
        elif kind == "use":
            value -= c.amount
        steps.append((c, round(value, 6)))
    return steps


def level(db: Session, category: ResourceCategory, now: datetime | None = None) -> Level:
    now = now or utcnow()
    all_counted = counted_entries(db, category)
    latest = max(all_counted, key=lambda c: c.entry.created_at).entry if all_counted else None
    if category.kind == "flow":
        since = now - FLOW_WINDOW
        used = sum(
            c.amount
            for c in all_counted
            if not c.void and c.entry.entry_type == "use" and c.entry.occurred_at >= since
        )
        return Level(category, None, None, latest, used)
    steps = stock_steps(all_counted)
    if not steps:
        return Level(category, None, None, latest)
    readings = [c.entry for c, _ in steps if c.entry.entry_type == "reading"]
    return Level(category, steps[-1][1], readings[-1] if readings else None, latest)


def levels(db: Session, now: datetime | None = None) -> list[Level]:
    """The current level of every active resource, in display order."""
    return [level(db, c, now) for c in categories.list_active(db)]


def recent(db: Session, limit: int = 10) -> list[ResourceEntry]:
    """The newest entries of all resources (corrections included)."""
    return list(
        db.scalars(
            select(ResourceEntry)
            .order_by(ResourceEntry.created_at.desc(), ResourceEntry.id.desc())
            .limit(limit)
        )
    )


def get(db: Session, entry_id: int) -> ResourceEntry | None:
    return db.get(ResourceEntry, entry_id)
