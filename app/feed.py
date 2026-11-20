"""The event feed: one short line per change, for the COMMONS room (D6).

Services only publish events (app/events.py). Just before a session commits,
this module turns each event it has a formatter for into an `event_feed` row,
in the same transaction as the change: the line exists exactly when the change
does. The event that subscribers get after the commit carries the row as
`data["feed"]`, which /internal/events streams to the bot.

Every word is written here by code from the database, never by the model.
For ledger events the feed item also has

- `entry_id` and `line`: the original entry as it counts now, so the bot can
  edit its earlier post when an entry is fixed within minutes,
- `topic`: the current levels, for the room topic.

Bookings: one line when something is booked, changed, given back or
cancelled, with the note and (vehicles) where the member is going: there are
no private bookings, so others can ask for an errand or come along. If someone
still has the thing when a booking starts, the line says so.

Low-level alerts (D7): after a ledger write, each category it touched is
checked against its `shortage_threshold`. Dropping below it writes one
`ledger.level_low` row; nothing more until the level is back at or above the
threshold (`ledger.level_ok`). The newest of those rows is the alert state, so
it survives restarts and is never kept anywhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import community, ledger
from app.events import PENDING, Event
from app.i18n import en
from app.models import (
    Booking,
    DecisionLog,
    EventFeed,
    ForecastRun,
    Member,
    Proposal,
    ResourceCategory,
    ResourceEntry,
    utcnow,
)
from app.textfmt import number, when_range

SUMMARY_MAX = 500
LEVEL_LOW = "ledger.level_low"
LEVEL_OK = "ledger.level_ok"
LEDGER_KINDS = ("ledger.entry_added", "ledger.entry_edited", "ledger.entry_corrected")


def who(member: Member | None) -> str:
    if member is None:
        return en.FEED_SOMEONE
    return member.display_name or member.matrix_user_id.removeprefix("@").split(":", 1)[0]


# --- the levels, for the room topic -------------------------------------------


def _level_text(lv: ledger.Level) -> str:
    c = lv.category
    if c.kind == "flow":
        return en.FEED_TOPIC_FLOW.format(
            name=c.name, used=number(lv.used_recently or 0), unit=c.unit
        )
    if lv.value is None:
        return en.FEED_TOPIC_NONE.format(name=c.name)
    text = en.FEED_TOPIC_ITEM.format(name=c.name, value=number(lv.value), unit=c.unit)
    if lv.from_zero:
        text += " " + en.FEED_TOPIC_FROM_ZERO
    if lv.below_threshold:
        text += " " + en.FEED_TOPIC_LOW
    return text


def topic(db: Session) -> str:
    """ "COMMONS levels: Water 1,850 l · Firewood 1 m³ (from 0, low)"."""
    items = [_level_text(lv) for lv in ledger.levels(db)]
    return en.FEED_TOPIC.format(levels=" · ".join(items) if items else en.FEED_TOPIC_EMPTY)


# --- ledger lines -----------------------------------------------------------------


def _amount(value: float, unit: str) -> str:
    return f"{number(value)} {unit}"


def entry_line(db: Session, entry: ResourceEntry) -> str:
    """An original entry as it counts now: "Nora: Firewood used 1.5 m³ (#42, corrected …)"."""
    c = entry.category
    base = en.FEED_ENTRY.format(
        who=who(entry.member),
        name=c.name,
        what=en.LEDGER_TYPE_PAST[entry.entry_type],
        amount=_amount(entry.amount, c.unit),
        ref=entry.id,
    )
    fix = ledger.latest_correction(db, entry)
    if fix is not None and fix.void:
        return base + " " + en.FEED_LINE_UNDONE
    if fix is not None:
        return base + " " + en.FEED_LINE_CORRECTED.format(amount=_amount(fix.amount, c.unit))
    if entry.edited_at is not None:
        return base + " " + en.FEED_LINE_EDITED
    return base


def _before(db: Session, original: ResourceEntry, correction: ResourceEntry) -> float | None:
    """What `original` counted for just before `correction` (None: it was undone)."""
    earlier = db.scalars(
        select(ResourceEntry)
        .where(ResourceEntry.corrects_id == original.id, ResourceEntry.id != correction.id)
        .where(ResourceEntry.created_at <= correction.created_at)
        .order_by(ResourceEntry.created_at.desc(), ResourceEntry.id.desc())
        .limit(1)
    ).first()
    if earlier is None:
        return original.amount
    return None if earlier.void else earlier.amount


def _ledger_item(db: Session, e: Event) -> dict[str, Any] | None:
    row = db.get(ResourceEntry, e.data["entry_id"])
    if row is None:
        return None
    original = row.corrects or row
    c = original.category
    actor = row.member
    ref = {
        "who": who(actor),
        "ref": original.id,
        "name": c.name,
        "what": en.LEDGER_TYPE_PAST[original.entry_type],
        "author": who(original.member),
    }
    if e.kind == "ledger.entry_added":
        summary = entry_line(db, original)
    elif e.kind == "ledger.entry_edited":
        summary = en.FEED_EDITED.format(
            **ref,
            before=_amount(e.data.get("old_amount", original.amount), c.unit),
            after=_amount(original.amount, c.unit),
        )
    elif row.void:
        summary = en.FEED_UNDONE.format(**ref, amount=_amount(original.amount, c.unit))
    else:
        before = _before(db, original, row)
        summary = en.FEED_CORRECTED.format(
            **ref,
            before=en.FEED_VALUE_UNDONE if before is None else _amount(before, c.unit),
            after=_amount(row.amount, c.unit),
        )
    return {
        "member_id": actor.id if actor else None,
        "summary": summary,
        "entry_id": original.id,
        "line": entry_line(db, original),
        "topic": topic(db),
    }


# --- bookings ---------------------------------------------------------------------


def _tz(db: Session) -> ZoneInfo:
    return ZoneInfo(community.get(db, community.TIMEZONE_KEY) or "UTC")


def _details(b: Booking) -> str:
    """ ", going to town (bring bags)": where to (vehicles) and the note, if given."""
    out = en.FEED_GOING_TO.format(destination=b.destination) if b.destination else ""
    return out + (en.FEED_NOTE.format(note=b.note) if b.note else "")


def _still_with(db: Session, e: Event) -> str:
    holders = [db.get(Booking, i) for i in e.data.get("still_with") or ()]
    names = [who(h.member) for h in holders if h is not None]
    return en.FEED_STILL_WITH.format(who=", ".join(names)) if names else ""


def _booking_item(db: Session, e: Event) -> dict[str, Any] | None:
    b = db.get(Booking, e.data["booking_id"])
    if b is None:
        return None
    tz = _tz(db)
    args = {"who": who(b.member), "name": b.bookable.name}
    when = when_range(b.starts_at, b.ends_at, tz)
    actor = b.member_id
    if e.kind == "booking.created":
        summary = en.FEED_BOOKED.format(**args, when=when, details=_details(b))
        summary += _still_with(db, e)
    elif e.kind == "booking.changed":
        actor = e.data.get("changed_by") or b.member_id
        if e.data.get("by_admin"):
            summary = en.FEED_BOOKING_CHANGED_ADMIN.format(
                **args, admin=who(db.get(Member, actor)), when=when, details=_details(b)
            )
        else:
            summary = en.FEED_BOOKING_CHANGED.format(**args, when=when, details=_details(b))
        summary += _still_with(db, e)
    elif e.kind == "booking.returned":
        if e.data.get("late"):
            due = when_range(b.ends_at, None, tz).removeprefix("from ")
            summary = en.FEED_RETURNED_LATE.format(**args, when=due)
        else:
            summary = en.FEED_RETURNED.format(**args)
    elif e.data.get("by_admin"):
        actor = b.cancelled_by
        summary = en.FEED_BOOKING_CANCELLED_ADMIN.format(
            **args,
            admin=who(db.get(Member, b.cancelled_by) if b.cancelled_by else None),
            when=when,
            reason=b.cancel_reason or "",
        )
    else:
        actor = b.cancelled_by
        summary = en.FEED_BOOKING_CANCELLED.format(**args, when=when)
    return {"member_id": actor, "summary": summary, "booking_id": b.id}


def _overdue_item(db: Session, e: Event) -> dict[str, Any] | None:
    """A reminder to give a thing back (app/reminders.py): a private message, not a room line."""
    b = db.get(Booking, e.data["booking_id"])
    if b is None or b.ends_at is None:
        return None
    tz = _tz(db)
    following = db.scalars(
        select(Booking)
        .where(
            Booking.bookable_id == b.bookable_id,
            Booking.cancelled_at.is_(None),
            Booking.starts_at > b.starts_at,
        )
        .order_by(Booking.starts_at)
        .limit(1)
    ).first()
    nxt = ""
    if following is not None:
        nxt = en.REMINDER_NEXT.format(
            who=who(following.member),
            when=when_range(following.starts_at, None, tz).removeprefix("from "),
        )
    name = b.bookable.name
    return {
        "member_id": None,
        "summary": en.REMINDER_OVERDUE.format(
            name=name,
            due=when_range(b.ends_at, None, tz).removeprefix("from "),
            next=nxt,
            lower=name.lower(),
        ),
        "booking_id": b.id,
        "due_at": e.data["due_at"],
        "to": b.member.matrix_user_id,
    }


def _backup_failed_item(db: Session, e: Event) -> dict[str, Any] | None:
    """A failed nightly backup (app/backup_alert.py): a private message to one admin."""
    m = db.get(Member, e.data["member_id"])
    if m is None:
        return None
    reason = en.BACKUP_FAILED_REASONS.get(e.data["reason"], en.BACKUP_FAILED_REASONS["other"])
    return {
        "member_id": None,
        "summary": en.BACKUP_FAILED.format(reason=reason),
        "to": m.matrix_user_id,
    }


def _forecast_item(db: Session, e: Event) -> dict[str, Any] | None:
    """A new likely shortage: once per crossing (app/forecast.py decides when)."""
    from app import forecast

    r = db.get(ForecastRun, e.data["run_id"])
    c = db.get(ResourceCategory, e.data["category_id"])
    if r is None or c is None or r.shortage_on is None:
        return None
    span = forecast.range_text(r)
    basis = forecast.basis_text(r)
    return {
        "member_id": None,
        "summary": en.FEED_FORECAST.format(
            name=c.name,
            threshold=_amount(c.shortage_threshold, c.unit),
            day=forecast.day_text(r.shortage_on),
            basis=f"{span}; {basis}" if span else basis,
        ),
        "category_id": c.id,
    }


def _vote_item(db: Session, e: Event) -> dict[str, Any] | None:
    """A vote in the COMMONS room: the announcement, then the result as a reply to it.

    Ballots are never taken here: the room post points members to their direct chat.
    """
    from app import vote_chat

    p = db.get(Proposal, e.data.get("proposal_id"))
    if p is None:
        return None
    if e.kind == "proposal.opened":
        text, html = vote_chat.announcement(db, p, _tz(db))
        return {"member_id": None, "summary": text, "text": text, "html": html, "proposal_id": p.id}
    text, html = vote_chat.room_result(db, p)
    shown = vote_chat.announced(db, p.id)
    return {
        "member_id": None,
        "summary": text,
        "text": text,
        "html": html,
        "proposal_id": p.id,
        "reply_room": shown.room_id if shown else None,
        "reply_to": shown.event_id if shown else None,
    }


def _anchor_item(db: Session, e: Event) -> dict[str, Any] | None:
    """The decision log's head, for members to keep (app/anchors.py)."""
    return {
        "member_id": None,
        "summary": en.FEED_ANCHOR.format(seq=e.data["seq"], hash=e.data["hash"]),
    }


FORMATTERS: dict[str, Callable[[Session, Event], dict[str, Any] | None]] = {
    "ledger.entry_added": _ledger_item,
    "ledger.entry_edited": _ledger_item,
    "ledger.entry_corrected": _ledger_item,
    "booking.created": _booking_item,
    "booking.changed": _booking_item,
    "booking.returned": _booking_item,
    "booking.cancelled": _booking_item,
    "booking.overdue": _overdue_item,
    "backup.failed": _backup_failed_item,
    "forecast.shortage": _forecast_item,
    "proposal.opened": _vote_item,
    "proposal.closed": _vote_item,
    "proposal.cancelled": _vote_item,
    "log.anchored": _anchor_item,
}


def item(row: EventFeed) -> dict[str, Any]:
    """A feed row as the bot gets it (also on replay after a reconnect)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "at": row.created_at.isoformat(),
        "summary": row.summary,
        **row.data,
    }


def since(db: Session, last_id: int, limit: int = 100) -> list[dict[str, Any]]:
    """Feed items after `last_id`, oldest first (what a reconnecting bot missed)."""
    rows = db.scalars(
        select(EventFeed).where(EventFeed.id > last_id).order_by(EventFeed.id).limit(limit)
    )
    return [item(r) for r in rows]


def latest_id(db: Session) -> int:
    return db.scalar(select(EventFeed.id).order_by(EventFeed.id.desc()).limit(1)) or 0


def scope(db: Session) -> str:
    """Which database's feed this is: feed ids mean nothing in another one.

    The start of the decision log's first hash: it differs between databases and
    between two seedings of the demo, and a restored backup keeps it.
    """
    first = db.scalar(select(DecisionLog.entry_hash).order_by(DecisionLog.seq).limit(1))
    return (first or "")[:16]


# --- low-level alerts (D7) ---------------------------------------------------------


def alert_state(db: Session, category_id: int) -> str | None:
    """LEVEL_LOW or LEVEL_OK from the newest alert row of the category (None: never)."""
    return db.scalar(
        select(EventFeed.kind)
        .where(EventFeed.kind.in_((LEVEL_LOW, LEVEL_OK)))
        .where(EventFeed.data["category_id"].as_integer() == category_id)
        .order_by(EventFeed.id.desc())
        .limit(1)
    )


def _alert_item(db: Session, category_id: int) -> tuple[str, dict[str, Any]] | None:
    # Lock the category so two writes committing at once can't both alert.
    db.execute(
        select(ResourceCategory.id).where(ResourceCategory.id == category_id).with_for_update()
    )
    c = db.get(ResourceCategory, category_id)
    if c is None or not c.is_active or c.kind != "stock" or c.shortage_threshold is None:
        return None
    lv = ledger.level(db, c)
    if lv.value is None:
        return None
    was_low = alert_state(db, c.id) == LEVEL_LOW
    if lv.below_threshold == was_low:
        return None
    args = {
        "name": c.name,
        "value": _amount(lv.value, c.unit),
        "threshold": _amount(c.shortage_threshold, c.unit),
    }
    kind, text = (LEVEL_LOW, en.FEED_LOW) if lv.below_threshold else (LEVEL_OK, en.FEED_OK)
    return kind, {
        "member_id": None,
        "summary": text.format(**args),
        "category_id": c.id,
        "topic": topic(db),
    }


# --- writing the rows ---------------------------------------------------------------


def _add_row(session: Session, kind: str, made: dict[str, Any]) -> dict[str, Any]:
    member_id = made.pop("member_id")
    summary = made.pop("summary")[:SUMMARY_MAX]
    row = EventFeed(kind=kind, member_id=member_id, summary=summary, data=made, created_at=utcnow())
    session.add(row)
    session.flush()
    return item(row)


@sa_event.listens_for(Session, "before_commit")
def _write_feed(session: Session) -> None:
    pending: list[Event] = session.info.get(PENDING) or []
    if not pending:
        return
    out = []
    touched: list[int] = []
    for e in pending:
        if e.kind in LEDGER_KINDS and e.data.get("category_id") not in touched:
            touched.append(e.data["category_id"])
        fmt = FORMATTERS.get(e.kind)
        made = fmt(session, e) if fmt else None
        if made is None:
            out.append(e)
            continue
        out.append(Event(e.kind, {**e.data, "feed": _add_row(session, e.kind, made)}))
    for category_id in touched:
        alert = _alert_item(session, category_id)
        if alert is not None:
            kind, made = alert
            out.append(
                Event(kind, {"category_id": category_id, "feed": _add_row(session, kind, made)})
            )
    session.info[PENDING] = out
