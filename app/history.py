"""Usage history for admins: past entries, levels over time, CSV.

Read only. Everything is derived from the ledger the same way the live level
is (`ledger.counted_entries` + `ledger.stock_steps`), so the history, the chat
and the COMMONS room always tell the same numbers. The derivation always runs
over the whole history and is then cut to the period, so the level on the
first day includes everything logged before it.

Days are calendar days in the community's time zone. A correction made after
the period still changes what an entry inside it counts for: the history shows
what the entries count for today, with the correction rows next to them.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ledger
from app.calendar import member_name
from app.i18n import en
from app.models import ResourceCategory, ResourceEntry

DEFAULT_DAYS = 90
MAX_DAYS = 366
# Longer periods are drawn per week (Monday to Sunday), so the chart stays readable.
WEEKLY_AFTER_DAYS = 92


class PeriodError(ValueError):
    """The chosen period can't be shown; the message is for the admin."""


@dataclass(frozen=True)
class Period:
    start: date  # first day, inclusive
    end: date  # last day, inclusive
    tz: tzinfo

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def starts_at(self) -> datetime:
        return datetime.combine(self.start, time(), self.tz)

    @property
    def ends_at(self) -> datetime:
        """Midnight after the last day (exclusive)."""
        return datetime.combine(self.end + timedelta(days=1), time(), self.tz)

    def __contains__(self, t: datetime) -> bool:
        return self.starts_at <= t < self.ends_at


def _parse_day(raw: str, what: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise PeriodError(en.HISTORY_BAD_DATE.format(what=what)) from None


def period(raw_from: str | None, raw_to: str | None, tz: tzinfo, today: date) -> Period:
    """The period from the form fields (YYYY-MM-DD); by default the last 90 days."""
    end = _parse_day(raw_to, "to") if raw_to else today
    start = _parse_day(raw_from, "from") if raw_from else end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise PeriodError(en.HISTORY_FROM_AFTER_TO)
    if (end - start).days + 1 > MAX_DAYS:
        raise PeriodError(en.HISTORY_TOO_LONG.format(n=MAX_DAYS))
    return Period(start, end, tz)


@dataclass(frozen=True)
class Row:
    """One line of the table and the CSV: an original entry or a correction."""

    entry: ResourceEntry
    # Originals: what it counts for today (0 once undone). Corrections: None.
    counted: float | None
    # "counted", "corrected", "undone" (originals) or "correction".
    status: str
    # Stock originals that still count: the level right after this entry.
    level_after: float | None = None

    @property
    def is_correction(self) -> bool:
        return self.status == "correction"


@dataclass(frozen=True)
class Bucket:
    """One day (or week) of the chart."""

    start: date
    level: float | None  # stock: level at the end; None before anything was logged
    used: float
    added: float


@dataclass
class History:
    category: ResourceCategory
    period: Period
    rows: list[Row]
    buckets: list[Bucket]
    weekly: bool
    level_at_start: float | None
    level_at_end: float | None
    used: float = 0.0
    added: float = 0.0
    entries: int = 0  # originals in the period
    corrections: int = 0
    chart: dict = field(default_factory=dict)


def _corrections(db: Session, category: ResourceCategory) -> dict[int, list[ResourceEntry]]:
    rows = db.scalars(
        select(ResourceEntry)
        .where(ResourceEntry.category_id == category.id, ResourceEntry.corrects_id.is_not(None))
        .order_by(ResourceEntry.created_at, ResourceEntry.id)
    )
    by_original: dict[int, list[ResourceEntry]] = {}
    for r in rows:
        by_original.setdefault(r.corrects_id, []).append(r)
    return by_original


def _level_before(steps: list[tuple[ledger.Counted, float]], t: datetime) -> float | None:
    """The stock level just before `t`; None if nothing counted yet."""
    value = None
    for c, after in steps:
        if c.entry.occurred_at >= t:
            break
        value = after
    return value


def history(db: Session, category: ResourceCategory, p: Period) -> History:
    counted = ledger.counted_entries(db, category)
    fixes = _corrections(db, category)
    stock = category.kind == "stock"
    steps = ledger.stock_steps(counted) if stock else []
    level_after = {c.entry.id: v for c, v in steps}

    rows: list[Row] = []
    used = added = 0.0
    entries = corrections = 0
    for c in counted:
        if c.entry.occurred_at not in p:
            continue
        entries += 1
        status = "undone" if c.void else ("corrected" if c.corrected else "counted")
        rows.append(Row(c.entry, c.amount, status, level_after.get(c.entry.id)))
        for fix in fixes.get(c.entry.id, []):
            corrections += 1
            rows.append(Row(fix, None, "correction"))
        if c.entry.entry_type == "use":
            used += c.amount
        elif c.entry.entry_type == "add":
            added += c.amount

    weekly = p.days > WEEKLY_AFTER_DAYS
    buckets = _buckets(counted, steps, p, weekly, stock)
    h = History(
        category=category,
        period=p,
        rows=rows,
        buckets=buckets,
        weekly=weekly,
        level_at_start=_level_before(steps, p.starts_at) if stock else None,
        level_at_end=_level_before(steps, p.ends_at) if stock else None,
        used=round(used, 6),
        added=round(added, 6),
        entries=entries,
        corrections=corrections,
    )
    h.chart = chart(h)
    return h


def _buckets(counted, steps, p: Period, weekly: bool, stock: bool) -> list[Bucket]:
    starts: list[date] = []
    d = p.start
    while d <= p.end:
        starts.append(d)
        d = (d + timedelta(days=7 - d.weekday())) if weekly else d + timedelta(days=1)
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else p.end + timedelta(days=1)
        lo, hi = datetime.combine(s, time(), p.tz), datetime.combine(e, time(), p.tz)
        inside = [c for c in counted if not c.void and lo <= c.entry.occurred_at < hi]
        out.append(
            Bucket(
                start=s,
                level=_level_before(steps, hi) if stock else None,
                used=round(sum(c.amount for c in inside if c.entry.entry_type == "use"), 6),
                added=round(sum(c.amount for c in inside if c.entry.entry_type == "add"), 6),
            )
        )
    return out


# --- chart (server-side SVG; the template only places these numbers) ---------

WIDTH, HEIGHT = 720, 240
LEFT, RIGHT, TOP, BOTTOM = 56, 12, 12, 28


def _nice_max(v: float) -> tuple[float, float]:
    """A round top for the y axis and the step between its 4 ticks."""
    if v <= 0:
        return 1.0, 0.25
    raw = v / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return step * 4, step


def chart(h: History) -> dict:
    """Coordinates for the chart: a level line (stock) or use bars (flow)."""
    stock = h.category.kind == "stock"
    n = len(h.buckets)
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    values = [b.level for b in h.buckets] if stock else [b.used for b in h.buckets]
    known = [v for v in values if v is not None]
    marks = [*known]
    if h.category.shortage_threshold is not None:
        marks.append(h.category.shortage_threshold)
    # A level can go below zero when uses outrun the last reading: show that too.
    hi, lo = max([0.0, *marks]), min([0.0, *marks])
    span, step = _nice_max(hi - lo)
    low = -step * math.ceil(-lo / step) if lo < 0 else 0.0
    top = low + span
    while top < hi:
        top += step

    def y(v: float) -> float:
        span = top - low if top > low else 1.0
        return round(TOP + plot_h - (v - low) / span * plot_h, 1)

    slot = plot_w / max(n, 1)

    def x(i: int) -> float:
        return round(LEFT + slot * (i + 0.5), 1)

    ticks = []
    v = low
    while v <= top + 1e-9:
        ticks.append({"y": y(v), "label": round(v, 6)})
        v += step
    every = max(1, math.ceil(n / 6))
    labels = [{"x": x(i), "label": b.start} for i, b in enumerate(h.buckets) if i % every == 0]
    out = {
        "width": WIDTH,
        "height": HEIGHT,
        "left": LEFT,
        "right": WIDTH - RIGHT,
        "top": TOP,
        "bottom": TOP + plot_h,
        "ticks": ticks,
        "labels": labels,
        "empty": not known or (not stock and not any(known)),
        "threshold": None,
    }
    if h.category.shortage_threshold is not None:
        out["threshold"] = y(h.category.shortage_threshold)
    if stock:
        out["points"] = [
            {"x": x(i), "y": y(b.level), "bucket": b}
            for i, b in enumerate(h.buckets)
            if b.level is not None
        ]
        out["line"] = " ".join(f"{p['x']},{p['y']}" for p in out["points"])
    else:
        bar_w = round(max(1.0, slot * 0.7), 1)
        out["bars"] = [
            {
                "x": round(x(i) - bar_w / 2, 1),
                "y": y(b.used),
                "w": bar_w,
                "h": round(y(0.0) - y(b.used), 1),
                "bucket": b,
            }
            for i, b in enumerate(h.buckets)
            if b.used > 0
        ]
    return out


# --- CSV -----------------------------------------------------------------------

CSV_COLUMNS = (
    "entry_id",
    "occurred_at",
    "logged_at",
    "type",
    "amount",
    "unit",
    "counted_amount",
    "status",
    "corrects_id",
    "level_after",
    "member",
    "channel",
    "note",
)
# A spreadsheet runs a cell starting with these as a formula (CSV injection).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str | None) -> str:
    """Text a member typed, made inert for spreadsheets: a leading ' stops formulas."""
    if not value:
        return ""
    return "'" + value if value.startswith(FORMULA_START) else value


def _number(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def to_csv(h: History) -> str:
    tz = h.period.tz
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in h.rows:
        e = r.entry
        w.writerow(
            [
                e.id,
                e.occurred_at.astimezone(tz).isoformat(timespec="seconds"),
                e.created_at.astimezone(tz).isoformat(timespec="seconds"),
                e.entry_type,
                "" if e.void else _number(e.amount),
                h.category.unit,
                _number(r.counted),
                "undo" if e.void else r.status,
                e.corrects_id or "",
                _number(r.level_after),
                safe_text(member_name(e.member)),
                e.channel,
                safe_text(e.note),
            ]
        )
    return buf.getvalue()


def csv_filename(h: History) -> str:
    """firewood_2026-10-15_2026-11-14.csv (the slug is [a-z0-9-] only)."""
    return f"{h.category.slug}_{h.period.start.isoformat()}_{h.period.end.isoformat()}.csv"
