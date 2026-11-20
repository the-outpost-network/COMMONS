"""What a member gave: their contributions and participation badges.

Private to the member: every function here takes the member it is about, and
the routes and the chat only ever pass the signed-in sender. Admins get no
per-member view (aggregate participation only, not built yet).

Badges reward taking part, never using more. A badge rule
(`badge_rule.rule_json`, seeded from community.yaml) is
`{"count": <counter>, "min": <n>}`; the counters are in `COUNTERS` and the same
names are the only ones community.yaml accepts (`community.BadgeCounter`):

- `ledger_entries`: readings, uses and adds the member logged by hand (sensor
  entries and entries they undid don't count; a "use" counts as much as a
  reading: logging is the contribution, not the amount).
- `ledger_days_in_a_row`: the longest run of days in a row with at least one
  such entry, in the community's time zone.
- `bookings_honoured`: bookings kept as agreed: a space whose time is over, or
  a vehicle or tool given back by its end (or with no end). Cancelling or
  bringing something back late doesn't count; it doesn't take anything away either.
- `votes`: not counted yet. A rule
  using it shows as "not counted yet" and is never awarded.

A counter returns the moments its value went up, so the badge for "min: n"
was earned at the n-th moment. `sync` writes earned badges into `member_badge`
with that moment, whenever it runs (page view, chat question): the date is
when it was really earned, not when somebody looked. A badge once earned stays,
even if an entry is undone later or the rule is archived.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import calendar as cal
from app.models import (
    BadgeRule,
    Booking,
    Member,
    MemberBadge,
    ResourceCategory,
    ResourceEntry,
    utcnow,
)

RECENT = 10
WINDOW_DAYS = 30


# --- counting ------------------------------------------------------------------


def counted_entries(db: Session, member: Member) -> list[ResourceEntry]:
    """The member's own hand-logged entries that still count (not undone), oldest first."""
    originals = list(
        db.scalars(
            select(ResourceEntry)
            .where(
                ResourceEntry.member_id == member.id,
                ResourceEntry.entry_type != "correction",
                ResourceEntry.channel != "sensor",
            )
            .order_by(ResourceEntry.created_at, ResourceEntry.id)
        )
    )
    if not originals:
        return []
    # The latest correction of each entry decides; a void one means it was undone.
    undone: dict[int, bool] = {}
    fixes = db.execute(
        select(ResourceEntry.corrects_id, ResourceEntry.void)
        .where(ResourceEntry.corrects_id.in_([e.id for e in originals]))
        .order_by(ResourceEntry.created_at, ResourceEntry.id)
    )
    for corrects_id, void in fixes:
        undone[corrects_id] = void
    return [e for e in originals if not undone.get(e.id, False)]


def _entry_moments(db: Session, member: Member, tz: ZoneInfo, now: datetime) -> list[datetime]:
    return [e.created_at for e in counted_entries(db, member)]


def _streak_moments(db: Session, member: Member, tz: ZoneInfo, now: datetime) -> list[datetime]:
    """When the longest run of days in a row first reached 1, 2, 3, … days."""
    first_of_day: dict[date, datetime] = {}
    for e in counted_entries(db, member):
        first_of_day.setdefault(e.created_at.astimezone(tz).date(), e.created_at)
    moments: list[datetime] = []
    run, previous = 0, None
    for day in sorted(first_of_day):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        previous = day
        if run > len(moments):
            moments.append(first_of_day[day])
    return moments


def honoured(booking: Booking, now: datetime) -> datetime | None:
    """When the booking was kept as agreed, or None (not yet, cancelled, or back late)."""
    if booking.cancelled_at is not None:
        return None
    if booking.bookable.type == "space":
        return booking.blocks_until if booking.blocks_until <= now else None
    if booking.returned_at is None:
        return None
    if booking.ends_at is not None and booking.returned_at > booking.ends_at:
        return None
    return booking.returned_at


def _booking_moments(db: Session, member: Member, tz: ZoneInfo, now: datetime) -> list[datetime]:
    bookings = db.scalars(select(Booking).where(Booking.member_id == member.id))
    return sorted(t for b in bookings if (t := honoured(b, now)) is not None)


Counter = Callable[[Session, Member, ZoneInfo, datetime], list[datetime]]

# None: a participation counter whose feature isn't built yet; never awarded.
COUNTERS: dict[str, Counter | None] = {
    "ledger_entries": _entry_moments,
    "ledger_days_in_a_row": _streak_moments,
    "bookings_honoured": _booking_moments,
    "votes": None,
}


# --- badges --------------------------------------------------------------------


@dataclass(frozen=True)
class BadgeView:
    rule: BadgeRule
    counter: str
    target: int
    value: int  # progress, capped at target
    counted: bool  # False: the counter isn't available yet
    earned_at: datetime | None

    @property
    def earned(self) -> bool:
        return self.earned_at is not None


def _rule(rule: BadgeRule) -> tuple[str, int] | None:
    data = rule.rule_json if isinstance(rule.rule_json, dict) else {}
    counter, target = data.get("count"), data.get("min")
    if counter not in COUNTERS or isinstance(target, bool) or not isinstance(target, int):
        return None
    return counter, max(1, target)


def sync(db: Session, member: Member, now: datetime | None = None) -> list[BadgeView]:
    """Award the badges `member` has reached and return all of theirs, in order.

    Active rules plus archived ones the member already earned. Idempotent and
    safe if two requests run at once (the insert ignores a row that exists).
    The caller commits.
    """
    now = now or utcnow()
    tz = cal.timezone(db)
    earned = {
        mb.badge_rule_id: mb.awarded_at
        for mb in db.scalars(select(MemberBadge).where(MemberBadge.member_id == member.id))
    }
    rules = db.scalars(select(BadgeRule).order_by(BadgeRule.position, BadgeRule.id)).all()
    cache: dict[str, list[datetime]] = {}
    views: list[BadgeView] = []
    for rule in rules:
        if rule.archived_at is not None and rule.id not in earned:
            continue
        parsed = _rule(rule)
        if parsed is None:
            continue
        counter, target = parsed
        count = COUNTERS[counter]
        if count is None:
            views.append(BadgeView(rule, counter, target, 0, False, earned.get(rule.id)))
            continue
        if counter not in cache:
            cache[counter] = count(db, member, tz, now)
        moments = cache[counter]
        earned_at = earned.get(rule.id)
        if earned_at is None and rule.archived_at is None and len(moments) >= target:
            earned_at = moments[target - 1]
            db.execute(
                insert(MemberBadge)
                .values(member_id=member.id, badge_rule_id=rule.id, awarded_at=earned_at)
                .on_conflict_do_nothing()
            )
        views.append(BadgeView(rule, counter, target, min(len(moments), target), True, earned_at))
    db.flush()
    return views


# --- the summary ---------------------------------------------------------------


@dataclass(frozen=True)
class CategoryShare:
    category: ResourceCategory
    mine: int  # all time
    mine_recent: int  # last WINDOW_DAYS days
    all_recent: int  # everybody's, last WINDOW_DAYS days


@dataclass(frozen=True)
class Summary:
    entries: int
    shares: list[CategoryShare]
    bookings: int  # not cancelled
    bookings_honoured: int
    recent_entries: list[ResourceEntry]
    undone_ids: set[int]
    recent_bookings: list[Booking]
    badges: list[BadgeView]


def summary(db: Session, member: Member, now: datetime | None = None) -> Summary:
    """Everything the contribution page and the chat show (also awards badges; caller commits)."""
    now = now or utcnow()
    counted = counted_entries(db, member)
    since = now - timedelta(days=WINDOW_DAYS)

    mine: dict[int, int] = defaultdict(int)
    mine_recent: dict[int, int] = defaultdict(int)
    for e in counted:
        mine[e.category_id] += 1
        if e.created_at >= since:
            mine_recent[e.category_id] += 1
    all_recent: dict[int, int] = (
        dict(
            db.execute(
                select(ResourceEntry.category_id, func.count())
                .where(
                    ResourceEntry.category_id.in_(list(mine)),
                    ResourceEntry.entry_type != "correction",
                    ResourceEntry.created_at >= since,
                )
                .group_by(ResourceEntry.category_id)
            ).all()
        )
        if mine
        else {}
    )
    categories = (
        db.scalars(
            select(ResourceCategory)
            .where(ResourceCategory.id.in_(list(mine)))
            .order_by(ResourceCategory.position, ResourceCategory.id)
        ).all()
        if mine
        else []
    )
    shares = [
        CategoryShare(c, mine[c.id], mine_recent[c.id], all_recent.get(c.id, 0)) for c in categories
    ]

    recent_entries = list(
        db.scalars(
            select(ResourceEntry)
            .where(ResourceEntry.member_id == member.id, ResourceEntry.entry_type != "correction")
            .order_by(ResourceEntry.created_at.desc(), ResourceEntry.id.desc())
            .limit(RECENT)
        )
    )
    counted_ids = {e.id for e in counted}
    undone_ids = {e.id for e in recent_entries if e.channel != "sensor" and e.id not in counted_ids}

    bookings = list(
        db.scalars(
            select(Booking)
            .where(Booking.member_id == member.id)
            .order_by(Booking.starts_at.desc(), Booking.id.desc())
        )
    )
    kept = [b for b in bookings if b.cancelled_at is None]
    return Summary(
        entries=len(counted),
        shares=shares,
        bookings=len(kept),
        bookings_honoured=sum(1 for b in kept if honoured(b, now) is not None),
        recent_entries=recent_entries,
        undone_ids=undone_ids,
        recent_bookings=bookings[:RECENT],
        badges=sync(db, member, now),
    )


# --- export --------------------------------------------------------------------


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat()


def export(db: Session, member: Member) -> dict[str, Any]:
    """All of the member's own data COMMONS holds, as JSON. Nobody else's.

    Who cancelled a booking is left out (it may be an admin); everything else
    on the member's rows is theirs.
    """
    entries = db.scalars(
        select(ResourceEntry)
        .where(ResourceEntry.member_id == member.id)
        .order_by(ResourceEntry.created_at, ResourceEntry.id)
    )
    bookings = db.scalars(
        select(Booking).where(Booking.member_id == member.id).order_by(Booking.starts_at)
    )
    badges = db.execute(
        select(BadgeRule.slug, BadgeRule.name, MemberBadge.awarded_at)
        .join(MemberBadge, MemberBadge.badge_rule_id == BadgeRule.id)
        .where(MemberBadge.member_id == member.id)
        .order_by(MemberBadge.awarded_at)
    )
    return {
        "exported_at": _iso(utcnow()),
        "member": {
            "matrix_user_id": member.matrix_user_id,
            "display_name": member.display_name,
            "roles": sorted(member.role_names),
            "created_at": _iso(member.created_at),
        },
        "resource_entries": [
            {
                "id": e.id,
                "resource": e.category.slug,
                "entry_type": e.entry_type,
                "amount": e.amount,
                "unit": e.category.unit,
                "note": e.note,
                "occurred_at": _iso(e.occurred_at),
                "created_at": _iso(e.created_at),
                "edited_at": _iso(e.edited_at),
                "channel": e.channel,
                "corrects_id": e.corrects_id,
                "void": e.void,
            }
            for e in entries
        ],
        "bookings": [
            {
                "id": b.id,
                "thing": b.bookable.name,
                "starts_at": _iso(b.starts_at),
                "ends_at": _iso(b.ends_at),
                "returned_at": _iso(b.returned_at),
                "cancelled_at": _iso(b.cancelled_at),
                "note": b.note,
                "destination": b.destination,
                "channel": b.channel,
                "created_at": _iso(b.created_at),
            }
            for b in bookings
        ],
        "badges": [
            {"slug": slug, "name": name, "awarded_at": _iso(at)} for slug, name, at in badges
        ],
    }
