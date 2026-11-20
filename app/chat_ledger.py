"""Logging resource entries from chat: "we used 2 m³ of firewood".

The rules here catch the common phrasings without the model. Either way (rules
or model) the result is only a proposal of words: which resource, what
happened, how much, which unit, when. Code then

- finds the resource by its name, id or chat words (never an id from the model),
- checks the unit against the resource's own unit,
- asks back when something is missing or unclear, instead of guessing,
- logs through `ledger.add_entry` as the member who wrote (never anyone else),
- and repeats what it logged, with the new level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import categories, ledger, units
from app.i18n import en
from app.models import Member, ResourceCategory
from app.textfmt import number


@dataclass(frozen=True)
class LogRequest:
    resource: str | None
    entry_type: str | None  # reading / use / add, or None if unclear
    amount: float | None
    unit: str | None = None
    day: str | None = None  # "today" / "yesterday"
    time: str | None = None  # "HH:MM", local time
    note: str | None = None
    about_someone_else: bool = False
    is_question: bool = False
    # The member's message, for a proposal from the model: the resource must be
    # named in it (by name, id or chat word). None for the rules, which read the
    # resource from the message themselves.
    text: str | None = None


# --- rules ---------------------------------------------------------------------

_NUM = r"(?P<amt>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
_UNIT = r"(?:\s*(?P<unit>%|[^\W\d_]+\d?))?"
_RES = r"(?P<res>[^\W\d_](?:[^\W\d_]|[ '\u2019-])*?)"
_WHEN = (
    r"(?:\s+(?P<day>today|yesterday))?"
    r"(?:\s+at\s+(?P<hh>\d{1,2})[:.](?P<mm>\d{2}))?"
)
_END = r"\s*[.!]*$"
_USE = r"used|use|burned|burnt|took|consumed|drank"
_ADD = r"added|add|refilled|filled up|filled|brought in|brought|delivered|topped up|stacked"
_WHO = r"(?:(?:we|i)\s+)?(?:just\s+)?"

_VERB_FIRST = re.compile(
    rf"^{_WHO}(?P<verb>{_USE}|{_ADD})\s+{_NUM}{_UNIT}"
    rf"(?:\s+(?:of\s+)?(?:the\s+)?{_RES})?{_WHEN}{_END}",
    re.IGNORECASE,
)
_READING = re.compile(
    rf"^(?:the\s+)?{_RES}(?:\s*:\s*|\s+(?:reading|level|is\s+at|is\s+now|is|now\s+at|now|at|reads)"
    r"\s*:?\s*)"
    rf"{_NUM}{_UNIT}{_WHEN}{_END}",
    re.IGNORECASE,
)
_READING_FIRST = re.compile(
    rf"^(?:reading|level)\s*:?\s+(?:the\s+)?{_RES}\s*:?\s+{_NUM}{_UNIT}{_WHEN}{_END}",
    re.IGNORECASE,
)


_GREETING = re.compile(
    r"^(?:hi|hey|hello|hallo|hola|ola|moin|servus|good\s+(?:morning|evening))"
    r"(?:\s+(?:there|commons|bot))?\s*[,!.:-]*\s+",
    re.IGNORECASE,
)


def without_greeting(text: str) -> str:
    """ "ola, how much water do we have?" -> "how much water do we have?" """
    text = " ".join(text.split())
    return _GREETING.sub("", text, count=1)


def parse_log_rule(text: str) -> LogRequest | None:
    """A LogRequest for the common phrasings, or None (then the model decides)."""
    text = without_greeting(text)
    for pattern in (_VERB_FIRST, _READING_FIRST, _READING):
        m = pattern.match(text)
        if not m:
            continue
        groups = m.groupdict()
        verb = (groups.get("verb") or "").lower()
        if pattern is not _VERB_FIRST:
            entry_type = "reading"
        elif re.fullmatch(_USE, verb):
            entry_type = "use"
        else:
            entry_type = "add"
        resource, unit = groups.get("res"), groups.get("unit")
        # "used 2 firewood": the word after the number is the resource, not a unit.
        if not resource and unit:
            resource, unit = unit, None
        if not resource:
            return None
        time = f"{int(groups['hh']):02d}:{groups['mm']}" if groups.get("hh") else None
        return LogRequest(
            resource=resource.strip(),
            entry_type=entry_type,
            amount=ledger.parse_amount(groups["amt"]),
            unit=unit,
            day=(groups.get("day") or "").lower() or None,
            time=time,
        )
    return None


# --- keeping the model honest ---------------------------------------------------
# A small model fills in what it thinks is likely: a time nobody said, an amount
# for "a bit", a reading for "how much water do we have?". Its proposal is only
# used where the member's own words back it up.

_NUMBER_WORDS = {
    "zero": 0, "half": 0.5, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100,
}  # fmt: skip
_DIGITS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?")
_QUESTION = re.compile(
    r"\?\s*$|^(?:how|what|when|where|which|who|is|are|do|does|did|can|could|will|should)\b",
    re.IGNORECASE,
)
_TIME_IN_TEXT = re.compile(r"\b(\d{1,2})[:.h](\d{2})\b")


def numbers_in(text: str) -> set[float]:
    found = {ledger.parse_amount(n) for n in _DIGITS.findall(text)}
    words = re.findall(r"[a-z]+", text.lower())
    # "a"/"an" only count before a unit-like word ("a cubic metre"), not "a bit".
    for i, w in enumerate(words):
        if w in ("a", "an"):
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt in ("cubic", "litre", "liter", "metre", "meter", "basket", "bottle", "load"):
                found.add(1.0)
        elif w in _NUMBER_WORDS:
            found.add(float(_NUMBER_WORDS[w]))
    return found


def is_question(text: str) -> bool:
    return bool(_QUESTION.search(text.strip()))


# "it's at 2000 l now", "the tank shows 800", "3 m³ left": the member states the
# level. That is a reading even if they also say they filled it.
_LEVEL_WORDS = re.compile(
    r"\b(?:is|it'?s|its|now|currently|shows?|showing|reads?|reading|level|at)\s+"
    r"(?:(?:now|at|about|around|roughly|exactly|~)\s+)*~?\d"
    r"|\d[\d.,]*\s*[^\W\d_]*\d?\s+(?:left|remaining|in\s+the\s+tank|now)\b",
    re.IGNORECASE,
)


_CLOCK = re.compile(r"\bat\s+\d{1,2}(?:[:.h]\d{2}|\s*(?:am|pm|o'?clock)\b)", re.IGNORECASE)


def states_a_level(text: str) -> bool:
    """True if the message says what the level is now (clock times don't count)."""
    text = _CLOCK.sub(" ", text.replace("\u2019", "'"))
    return bool(_LEVEL_WORDS.search(text))


def grounded(req: LogRequest, text: str) -> LogRequest:
    """Keep only what the member's message actually says."""
    amount = req.amount if req.amount is not None and req.amount in numbers_in(text) else None
    times = {f"{int(h):02d}:{m}" for h, m in _TIME_IN_TEXT.findall(text)}
    time = req.time if req.time and req.time.zfill(5) in times else None
    day = req.day if req.day == "yesterday" and "yesterday" in text.lower() else None
    entry_type = req.entry_type
    if entry_type in ("use", "add") and states_a_level(text):
        entry_type = "reading"
    return LogRequest(
        resource=req.resource,
        entry_type=entry_type,
        amount=amount,
        unit=req.unit,
        day=day,
        time=time.zfill(5) if time else None,
        note=_note_in(req.note, text),
        about_someone_else=req.about_someone_else,
        is_question=is_question(text),
        text=text,
    )


def _note_in(note: str | None, text: str) -> str | None:
    """The model's note, only if the member wrote those words (any case, any spacing).

    The model must not put words into a member's entry, such as a note about
    another member that the message never contained.
    """
    if not note:
        return None
    note = " ".join(note.split())
    return note if note.casefold() in " ".join(text.split()).casefold() else None


def named_in(category: ResourceCategory, text: str, active: list[ResourceCategory]) -> bool:
    """Whether the message names this resource.

    By its name, id or one of its chat words, or by an amount in a unit no other
    resource uses ("it's at 2000l now" when only water is in litres).
    """
    flat = " ".join(text.split()).casefold()
    words = {category.slug, categories.normalize_word(category.name), *category.alias_names}
    if any(re.search(rf"(?<!\w){re.escape(w.casefold())}(?!\w)", flat) for w in words if w):
        return True
    if any(units.same(c.unit, category.unit) for c in active if c.id != category.id):
        return False
    return any(
        re.search(rf"\d\s*{re.escape(u)}(?!\w)", flat) for u in units.spellings(category.unit)
    )


# --- resolving -------------------------------------------------------------------


def resolve(db: Session, words: str | None) -> ResourceCategory | None:
    """The resource the member means: the whole phrase, else exactly one of its words."""
    if not words:
        return None
    found = categories.resolve(db, words)
    if found is not None:
        return found
    hits: dict[int, ResourceCategory] = {}
    for word in re.findall(r"[\w'\u2019-]+", words):
        if (c := categories.resolve(db, word)) is not None:
            hits[c.id] = c
    return next(iter(hits.values())) if len(hits) == 1 else None


def rule_applies(db: Session, req: LogRequest) -> bool:
    """Use a rule's reading only if the resource is clear and the unit word is a unit.

    In "the tank will be at 900 later" the word after the number isn't a unit, so
    the rule stands aside and the model decides. A real but different unit ("kg"
    for water) stays, and the member is asked to say it in the right unit.
    """
    category = resolve(db, req.resource)
    if category is None:
        return False
    return req.unit is None or units.known(req.unit) or units.same(req.unit, category.unit)


def occurred_at(req: LogRequest, now: datetime, tz: ZoneInfo) -> datetime | None:
    """When it happened, from "yesterday" / "at 08:30" (local time); None = now."""
    if req.day is None and req.time is None:
        return None
    local = now.astimezone(tz)
    if req.day == "yesterday":
        local -= timedelta(days=1)
    if req.time is not None:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", req.time)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ledger.LedgerError("the time isn't valid")
        local = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return local


def _names(cats: list[ResourceCategory]) -> str:
    return ", ".join(c.name for c in cats)


# --- acting -----------------------------------------------------------------------


def log(db: Session, member: Member, req: LogRequest, *, now: datetime, tz: ZoneInfo) -> str:
    """Log `req` as `member` and say what happened (or ask what's missing)."""
    if req.about_someone_else:
        return en.LOG_ONLY_YOURS
    if req.is_question:
        return en.LOG_NOT_FROM_QUESTION  # the assistant answers it instead
    active = categories.list_active(db)
    if not active:
        return en.LOG_NO_RESOURCES
    category = resolve(db, req.resource)
    if category is None or (req.text is not None and not named_in(category, req.text, active)):
        # A resource the message doesn't name is the model's guess: ask, never log it.
        return en.LOG_WHICH_RESOURCE.format(names=_names(active))
    allowed = ledger.TYPES_FOR_KIND[category.kind]
    if req.amount is None or isinstance(req.amount, bool):
        return en.LOG_NEED_AMOUNT.format(name=category.name, unit=category.unit)
    if req.entry_type not in allowed:
        return en.LOG_NEED_TYPE.format(
            name=category.name,
            options=" / ".join(en.LOG_TYPE_WORDS[t] for t in allowed),
            example=en.LOG_EXAMPLE.format(unit=category.unit, name=category.name.lower()),
        )
    if req.unit and not units.same(req.unit, category.unit):
        return en.LOG_WRONG_UNIT.format(name=category.name, unit=category.unit, given=req.unit)
    try:
        entry = ledger.add_entry(
            db,
            member,
            category,
            req.entry_type,
            req.amount,
            channel="matrix",
            note=req.note,
            occurred_at=occurred_at(req, now, tz),
            now=now,
        )
    except ledger.LedgerError as e:
        db.rollback()
        return en.LOG_REFUSED.format(reason=str(e))
    db.commit()
    reply = en.LOG_DONE.format(
        amount=number(entry.amount),
        unit=category.unit,
        name=category.name,
        what=en.LEDGER_TYPE_PAST[entry.entry_type],
        who=member.display_name or member.matrix_user_id.removeprefix("@").split(":", 1)[0],
        when=when_text(entry.occurred_at, now, tz),
    )
    level = level_text(db, category, now)
    if level:
        reply += " " + level
    return reply + " " + en.LOG_UNDO_HINT


def when_text(dt: datetime, now: datetime, tz: ZoneInfo) -> str:
    """ "14:20" today, else "09.11. 14:20" (local time)."""
    when = dt.astimezone(tz)
    if when.date() == now.astimezone(tz).date():
        return when.strftime("%H:%M")
    return when.strftime("%d.%m. %H:%M")


def level_text(db: Session, category: ResourceCategory, now: datetime) -> str:
    """ "Water is now at 950 l." (+ a warning), or "" for a flow resource."""
    level = ledger.level(db, category, now)
    if level.value is not None:
        text = en.LOG_LEVEL_NOW.format(
            name=category.name, value=number(level.value), unit=category.unit
        )
        if level.from_zero:
            text += " " + en.LOG_FROM_ZERO.format(name=category.name.lower())
        return text + " " + en.LOG_RUNNING_LOW if level.below_threshold else text
    if category.kind == "stock":
        return en.LOG_NO_LEVEL_YET.format(name=category.name)
    return ""


def resources_for_prompt(db: Session) -> str:
    """The configured resources for the model: names, units and chat words only."""
    lines = []
    for c in categories.list_active(db):
        words = f" (also: {', '.join(c.alias_names)})" if c.alias_names else ""
        lines.append(f"- {c.name} in {c.unit}{words}")
    return "\n".join(lines) or "- (none configured yet)"
