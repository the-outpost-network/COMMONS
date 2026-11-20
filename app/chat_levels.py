"""Answering "how much water do we have?" and "last 5 firewood entries" in chat.

Every number in these replies is written by code from the ledger (`ledger.level`,
`ledger.counted_entries`). The model at most picks the intent and the resource
word; it never sees or writes a level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import categories, chat_ledger, forecast, ledger
from app.i18n import en
from app.models import Member, ResourceCategory, ResourceEntry
from app.textfmt import ago, number

HISTORY_DEFAULT = 5
HISTORY_MAX = 20
# A reading older than this is pointed out ("the last reading is 3 days old").
STALE_AFTER = timedelta(hours=48)


@dataclass(frozen=True)
class Question:
    kind: str  # "levels" or "history"
    resource: str | None = None  # None = all resources (levels only)
    count: int = HISTORY_DEFAULT


_END = r"\s*[?.!]*$"
_RES = r"(?P<res>[^\W\d_](?:[^\W\d_]|[ '\u2019-])*?)"
_ALL = re.compile(
    r"^(?:(?:show\s+(?:me\s+)?)?(?:the\s+)?(?:levels?|status|stock|resources?)"
    r"|what(?:'s|\s+is)\s+left|how\s+are\s+we\s+doing)" + _END,
    re.IGNORECASE,
)
_HOW_MUCH = re.compile(
    rf"^(?:and\s+)?how\s+(?:much|many)\s+(?:of\s+)?(?:the\s+)?{_RES}"
    r"(?:\s+(?:do\s+we\s+have|have\s+we\s+got|is\s+(?:there|left)|are\s+(?:there|left)"
    r"|(?:do\s+we\s+have\s+)?left|remaining|in\s+the\s+tank))*" + _END,
    re.IGNORECASE,
)
_HOW_MUCH_IN = re.compile(
    rf"^(?:and\s+)?how\s+(?:much|many)\s+(?:is|are|do\s+we\s+have)\s+(?:still\s+)?(?:left\s+)?"
    rf"(?:in|of)\s+(?:the\s+)?{_RES}(?:\s+left)?" + _END,
    re.IGNORECASE,
)
_LEVEL_OF = re.compile(
    rf"^(?:what(?:'s|\s+is)\s+the\s+)?{_RES}\s+(?:level|status|stock)" + _END, re.IGNORECASE
)
_HISTORY = re.compile(
    rf"^(?:show\s+(?:me\s+)?)?(?:the\s+)?(?:last|recent|latest)\s+(?P<n>\d{{1,2}}\s+)?{_RES}"
    r"\s+(?:entries|logs|readings)"
    + _END
    + rf"|^{_RES.replace('res', 'res2')}\s+(?:history|entries|log)"
    + _END,
    re.IGNORECASE,
)


def parse_question_rule(text: str) -> Question | None:
    text = chat_ledger.without_greeting(text)
    if _ALL.match(text):
        return Question("levels")
    if m := _HISTORY.match(text):
        n = int(m.group("n")) if m.group("n") else HISTORY_DEFAULT
        return Question("history", (m.group("res") or m.group("res2")).strip(), n)
    for pattern in (_HOW_MUCH_IN, _HOW_MUCH, _LEVEL_OF):
        if m := pattern.match(text):
            return Question("levels", m.group("res").strip())
    return None


def _who(member: Member) -> str:
    return member.display_name or member.matrix_user_id.removeprefix("@").split(":", 1)[0]


def _local(dt: datetime, now: datetime, tz: ZoneInfo) -> str:
    when = dt.astimezone(tz)
    if when.date() == now.astimezone(tz).date():
        return when.strftime("%H:%M")
    return when.strftime("%d.%m. %H:%M")


def level_line(lv: ledger.Level, now: datetime) -> str:
    c = lv.category
    if c.kind == "flow":
        return en.LEVEL_FLOW.format(name=c.name, used=number(lv.used_recently or 0), unit=c.unit)
    if lv.value is None:
        return en.LEVEL_NONE.format(name=c.name)
    if lv.from_zero:
        line = en.LEVEL_FROM_ZERO_LINE.format(name=c.name, value=number(lv.value), unit=c.unit)
        if lv.below_threshold:
            line += " " + en.LEVEL_LOW.format(threshold=number(c.shortage_threshold), unit=c.unit)
        return line
    line = en.LEVEL_LINE.format(
        name=c.name,
        value=number(lv.value),
        unit=c.unit,
        ago=ago(lv.reading.occurred_at, now),
        who=_who(lv.reading.member),
    )
    if lv.below_threshold:
        line += " " + en.LEVEL_LOW.format(threshold=number(c.shortage_threshold), unit=c.unit)
    if now - lv.reading.occurred_at > STALE_AFTER:
        line += " " + en.LEVEL_STALE
    return line


def _with_forecast(db: Session, lv: ledger.Level, now: datetime) -> str:
    line = level_line(lv, now)
    note = forecast.describe(forecast.latest_run(db, lv.category.id), lv.category)
    return f"{line} {note}" if note else line


def levels_reply(db: Session, category: ResourceCategory | None, now: datetime) -> str:
    if category is not None:
        return _with_forecast(db, ledger.level(db, category, now), now)
    lines = [_with_forecast(db, lv, now) for lv in ledger.levels(db, now)]
    return "\n".join(lines) if lines else en.LOG_NO_RESOURCES


def _entry_line(e: ResourceEntry, counted: ledger.Counted | None, now, tz) -> str:
    c = e.category
    what = en.LEDGER_TYPE_PAST[e.entry_type]
    line = en.HISTORY_LINE.format(
        id=e.id,
        when=_local(e.occurred_at, now, tz),
        who=_who(e.member),
        what=what,
        amount=number(e.amount),
        unit=c.unit,
    )
    if counted is not None and counted.void:
        line += " " + en.HISTORY_UNDONE
    elif counted is not None and counted.corrected:
        line += " " + en.HISTORY_CORRECTED.format(amount=number(counted.amount), unit=c.unit)
    elif e.edited_at is not None:
        line += " " + en.HISTORY_EDITED
    if e.note:
        line += f' "{e.note}"'
    return line


def history_reply(
    db: Session, category: ResourceCategory, count: int, now: datetime, tz: ZoneInfo
) -> str:
    count = max(1, min(count, HISTORY_MAX))
    counted = ledger.counted_entries(db, category)
    newest = sorted(counted, key=lambda c: (c.entry.occurred_at, c.entry.id), reverse=True)[:count]
    if not newest:
        return en.HISTORY_NONE.format(name=category.name)
    lines = [_entry_line(c.entry, c, now, tz) for c in newest]
    head = en.HISTORY_HEAD.format(n=len(lines), name=category.name)
    return head + "\n" + "\n".join(lines)


def answer(db: Session, q: Question, now: datetime, tz: ZoneInfo) -> str:
    active = categories.list_active(db)
    if not active:
        return en.LOG_NO_RESOURCES
    category = chat_ledger.resolve(db, q.resource) if q.resource else None
    if q.resource and category is None:
        return en.LEVEL_WHICH.format(names=", ".join(c.name for c in active))
    if q.kind == "history":
        if category is None:
            return en.LEVEL_WHICH.format(names=", ".join(c.name for c in active))
        return history_reply(db, category, q.count, now, tz)
    return levels_reply(db, category, now)
