"""Undo and corrections from chat (D5): "undo", "that was wrong, it was 1.5", "#42 should be 2".

As with logging, rules catch the common phrasings and the model is only asked
for the rest. Either way the proposal is just words: which entry (a number the
member wrote, or "my last one"), and what it should have been (or nothing:
undo). Code then

- finds the entry: the number from the history, else the sender's own newest
  entry (of the resource they name), and never an id the member didn't write,
- checks the unit and the rules through `ledger.fix_entry`, which decides
  between editing (author, inside 15 minutes) and a correction row (later, or
  an admin fixing someone else's entry); an undo is always a void correction,
- and repeats the entry with its value before and after, so a wrong guess is
  seen at once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import categories, chat_ledger, ledger, units
from app.i18n import en
from app.models import Member, ResourceEntry
from app.textfmt import number

# Without a number, "undo" only reaches back this far: a stray "undo" shouldn't
# quietly drop something logged last week. Older entries are fixed by number.
LAST_ENTRY_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class FixRequest:
    entry_ref: int | None = None  # "#42"; None = the sender's last entry
    amount: float | None = None  # what it should have been; None = undo
    resource: str | None = None  # "the firewood was 1.5": the last firewood entry
    unit: str | None = None
    undo: bool = False  # the member asked to undo (amount must then be None)
    about_someone_else: bool = False


# --- rules ---------------------------------------------------------------------

_NUM = r"(?P<amt>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
_UNIT = r"(?:\s*(?P<unit>%|[^\W\d_]+\d?))?"
_RES = r"(?P<res>[^\W\d_](?:[^\W\d_]|[ '\u2019-])*?)"
_REF = r"#\s?(?P<ref>\d{1,9})"
_END = r"\s*[.!]*$"
_CUE = (
    r"(?:(?:no|nope|oops|sorry|wrong|correction|mistake|my\s+bad|that\s+was\s+wrong"
    r"|that'?s\s+wrong|that\s+was\s+a\s+mistake)\s*[,.!:;-]*\s*)"
)
_SHOULD = (
    r"(?:it\s+was|it\s+should\s+be|it\s+should\s+have\s+been|should\s+be|should\s+have\s+been"
    r"|make\s+(?:that|it)|i\s+meant|actually|it\s+was\s+actually)"
)

_UNDO = re.compile(
    r"^(?:please\s+)?(?:undo|take\s+(?:that|it)\s+back|scratch\s+that|cancel\s+that)"
    rf"(?:\s+(?:that|it|this|(?:my|the)\s+last(?:\s+one|\s+entry)?|{_REF}"
    rf"|(?:my|the)\s+last\s+{_RES}(?:\s+entry)?))?{_END}",
    re.IGNORECASE,
)
_FIX_REF_TO = re.compile(
    rf"^(?:{_CUE})?(?:correct|fix|change|set)\s+{_REF}\s+to\s+{_NUM}{_UNIT}{_END}",
    re.IGNORECASE,
)
_FIX_REF_IS = re.compile(
    rf"^(?:{_CUE})?{_REF}\s+(?:should\s+be|should\s+have\s+been|was|was\s+actually|is)"
    rf"\s+{_NUM}{_UNIT}{_END}",
    re.IGNORECASE,
)
_FIX_LAST = re.compile(rf"^(?:{_CUE})?{_SHOULD}\s+{_NUM}{_UNIT}{_END}", re.IGNORECASE)
# "the firewood should be 1.5", "oops, the water was 800": the last entry of that
# resource. Plain "the water was 800" (no cue, "was") is left alone: it could be news.
_FIX_RES = re.compile(
    rf"^(?:{_CUE}(?:the\s+)?{_RES}\s+(?:was|should\s+be|should\s+have\s+been)"
    rf"|(?:the\s+)?{_RES.replace('res', 'res2')}\s+(?:should\s+be|should\s+have\s+been))"
    rf"\s+(?:actually\s+)?{_NUM}{_UNIT}{_END}",
    re.IGNORECASE,
)


def parse_fix_rule(text: str) -> FixRequest | None:
    """A FixRequest for the common phrasings, or None (then other rules or the model)."""
    text = chat_ledger.without_greeting(text)
    if m := _UNDO.match(text):
        g = m.groupdict()
        return FixRequest(
            entry_ref=int(g["ref"]) if g.get("ref") else None,
            resource=(g.get("res") or "").strip() or None,
            undo=True,
        )
    for pattern in (_FIX_REF_TO, _FIX_REF_IS, _FIX_LAST, _FIX_RES):
        if m := pattern.match(text):
            g = m.groupdict()
            resource = (g.get("res") or g.get("res2") or "").strip() or None
            return FixRequest(
                entry_ref=int(g["ref"]) if g.get("ref") else None,
                amount=ledger.parse_amount(g["amt"]),
                resource=resource,
                unit=g.get("unit"),
            )
    return None


def rule_applies(db: Session, req: FixRequest) -> bool:
    """Trust a rule only if the word after the number is a unit ("it was 2 yesterday" isn't)."""
    if req.unit is None or units.known(req.unit):
        return True
    return any(units.same(req.unit, c.unit) for c in categories.list_active(db))


# --- keeping the model honest ---------------------------------------------------

_REF_IN_TEXT = re.compile(r"#\s?(\d{1,9})\b|\b(?:entry|number|no\.?)\s+(\d{1,9})\b", re.IGNORECASE)
_UNDO_WORDS = re.compile(
    r"\b(?:undo|take\s+(?:that|it)\s+back|scratch\s+that|cancel|didn'?t\s+happen"
    r"|never\s+happened|remove|delete)\b",
    re.IGNORECASE,
)


def refs_in(text: str) -> set[int]:
    return {int(a or b) for a, b in _REF_IN_TEXT.findall(text)}


def grounded(req: FixRequest, text: str) -> FixRequest:
    """Keep only what the member's message says: their entry number and their amount.

    The number after "#" names an entry, so it never counts as an amount (the
    model might swap them). Without a stated amount, it's an undo only if the
    member said so; otherwise code asks what the value should be.
    """
    refs = refs_in(text)
    entry_ref = req.entry_ref if req.entry_ref in refs else None
    without_refs = _REF_IN_TEXT.sub(" ", text)
    amount = req.amount
    if amount is not None and amount not in chat_ledger.numbers_in(without_refs):
        amount = None
    return FixRequest(
        entry_ref=entry_ref,
        amount=amount,
        resource=req.resource,
        unit=req.unit,
        undo=amount is None and bool(_UNDO_WORDS.search(text)),
        about_someone_else=req.about_someone_else,
    )


# --- acting ---------------------------------------------------------------------


def _who(member: Member) -> str:
    return member.display_name or member.matrix_user_id.removeprefix("@").split(":", 1)[0]


def _value(amount: float | None, unit: str) -> str:
    return en.FIX_VALUE_UNDONE if amount is None else f"{number(amount)} {unit}"


def _find(db: Session, member: Member, req: FixRequest, now: datetime) -> ResourceEntry | str:
    """The entry the member means, or a reply asking for more."""
    if req.entry_ref is not None:
        entry = ledger.get(db, req.entry_ref)
        if entry is None:
            return en.FIX_NOT_FOUND.format(ref=req.entry_ref)
        return entry.corrects or entry
    if req.about_someone_else:
        return en.FIX_SOMEONE_ELSE
    category = None
    if req.resource:
        category = chat_ledger.resolve(db, req.resource)
        if category is None:
            return en.FIX_WHICH_RESOURCE.format(resource=req.resource)
    entry = ledger.last_entry(db, member, category)
    if entry is None:
        return en.FIX_NOTHING_YET
    if now - entry.created_at > LAST_ENTRY_MAX_AGE:
        return en.FIX_LAST_TOO_OLD.format(name=entry.category.name.lower(), ref=entry.id)
    return entry


def fix(db: Session, member: Member, req: FixRequest, *, now: datetime, tz: ZoneInfo) -> str:
    """Undo or correct an entry as `member` and say what changed (or ask)."""
    if req.amount is None and not req.undo:
        return en.FIX_NEED_AMOUNT
    found = _find(db, member, req, now)
    if isinstance(found, str):
        return found
    entry = found
    category = entry.category
    if req.unit and not units.same(req.unit, category.unit):
        return en.LOG_WRONG_UNIT.format(name=category.name, unit=category.unit, given=req.unit)
    try:
        done = ledger.fix_entry(db, member, entry, req.amount, channel="matrix", now=now)
    except ledger.NotAllowed:
        db.rollback()
        return en.FIX_NOT_YOURS.format(ref=entry.id)
    except ledger.LedgerError as e:
        db.rollback()
        return en.FIX_REFUSED.format(reason=str(e))
    db.commit()
    what = en.FIX_WHAT.format(
        ref=entry.id,
        name=category.name,
        what=en.LEDGER_TYPE_PAST[entry.entry_type],
        who=_who(entry.member),
        when=chat_ledger.when_text(entry.occurred_at, now, tz),
    )
    if done.mode == "undone":
        reply = en.FIX_UNDONE.format(entry=what, before=_value(done.before, category.unit))
    else:
        reply = (en.FIX_EDITED if done.mode == "edited" else en.FIX_CORRECTED).format(
            entry=what,
            before=_value(done.before, category.unit),
            after=_value(done.after, category.unit),
        )
    level = chat_ledger.level_text(db, category, now)
    return f"{reply} {level}" if level else reply
