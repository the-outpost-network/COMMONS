"""Numbers and times written the same way on the board and in chat."""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo

from app.models import utcnow


def number(value: float | int | None) -> str:
    """1000.0 -> "1,000", 2.5 -> "2.5" (no trailing zeros)."""
    if value is None:
        return ""
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def ago(dt: datetime, now: datetime | None = None) -> str:
    """ "just now", "5 min ago", "3 h ago", "2 days ago"."""
    seconds = ((now or utcnow()) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return "1 day ago" if days == 1 else f"{days} days ago"


DASH = "\u2013"  # en dash, for time ranges


def web_time(dt: datetime, tz: tzinfo, now: datetime | None = None) -> str:
    """For web pages only (chat keeps its own wording): "Today 08:27",
    "Yesterday 19:27", "Tomorrow 10:00", "Sat 14 Nov 08:27", and with the year
    when it isn't this year: "Sat 14 Nov 2025 08:27"."""
    from app.i18n import en

    local = dt.astimezone(tz)
    today = (now or utcnow()).astimezone(tz).date()
    offset = (local.date() - today).days
    if offset in en.RELATIVE_DAYS:
        date_part = en.RELATIVE_DAYS[offset]
    elif local.year == today.year:
        date_part = f"{local:%a} {local.day} {local:%b}"
    else:
        date_part = f"{local:%a} {local.day} {local:%b %Y}"
    return f"{date_part} {local:%H:%M}"


def day(dt: datetime) -> str:
    """ "Tue 1 Dec 2026" (dt already in local time)."""
    return f"{dt:%a} {dt.day} {dt:%b %Y}"


def when_range(start: datetime, end: datetime | None, tz: tzinfo) -> str:
    """A booking's time in local time, as short as it stays clear.

    Whole days: "Tue 1 Dec 2026 - Sun 13 Dec 2026" (or one day); one day with
    times: "Sat 14 Nov 2026, 10:00-16:00"; otherwise both ends with times.
    (The dash is an en dash.)
    No end (an open checkout): "from Sat 14 Nov 2026, 10:00".
    """
    s = start.astimezone(tz)
    if end is None:
        return f"from {day(s)}, {s:%H:%M}"
    e = end.astimezone(tz)
    if s.time() == e.time() == time(0):
        last = e - timedelta(days=1)
        return day(s) if last.date() == s.date() else f"{day(s)} {DASH} {day(last)}"
    if s.date() == e.date():
        return f"{day(s)}, {s:%H:%M}{DASH}{e:%H:%M}"
    return f"{day(s)}, {s:%H:%M} {DASH} {day(e)}, {e:%H:%M}"
