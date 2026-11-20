"""Bookings from chat: "I want to use the theatre from 01.12.26 to 13.12.26 for a workshop".

Everything the web calendar can do with a member's own bookings works here too,
for spaces, vehicles and tools alike:

- book a thing now or later, with an optional end, a note and, for a vehicle,
  where to ("I'm taking the saw", "car tomorrow 9:00 to 13:00 to town"),
- ask whether a thing is free, who has it and where it is,
- list the things that can be booked and one's own bookings,
- give a thing back, extend or shorten one's booking, cancel it.

As with logging, the rules here catch the common phrasings and the model is
only asked for the rest. Either way the proposal is just words: which action,
which thing (by name), a note and where to. Times are never taken from the
model: code reads them from the member's own message (dates are
day.month.year), so a misread date can't come from the model. Code then

- finds the thing by its name, and asks when none or several match,
- acts as the sender through `app.calendar` (`channel="matrix"`), with the
  same checks as the web; someone else's booking is out of reach,
- repeats what it understood, and on a conflict says who has the thing and
  offers the nearest free time,
- and asks "yes or no?" before writing anything the model had to guess: a
  thing the member didn't name in this message ("I returned it"), or a time
  from an earlier message. Named things are booked, given back or cancelled
  at once, and the reply says what was done.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, object_session

from app import bookables, chat_ledger
from app import calendar as cal
from app.i18n import en
from app.models import Bookable, Booking, Member
from app.textfmt import when_range

ACTIONS = ("book", "availability", "mine", "list", "return", "cancel", "change")
# Looking for the nearest free time gives up after this many tries.
FREE_SLOT_TRIES = 50


class ChatTimeError(ValueError):
    """The member's time words don't make a valid time; the message can be shown."""


@dataclass(frozen=True)
class Span:
    """When, as read from the member's own words. `said` is False if they said no time."""

    said: bool = False
    starts_at: datetime | None = None  # UTC
    ends_at: datetime | None = None  # UTC; None: no end
    start_is_now: bool = False


@dataclass(frozen=True)
class BookingRequest:
    action: str  # one of ACTIONS
    thing: str | None = None  # words naming the thing (from the model); None: read the text
    note: str | None = None
    destination: str | None = None
    booking_ref: int | None = None  # "booking #12"
    about_someone_else: bool = False
    from_model: bool = False
    # COMMONS' last message in this chat ("You said you'd give back the Saw …"):
    # "it" means the one thing it names.
    last_reply: str = ""


@dataclass(frozen=True)
class Pending:
    """A write that waits for the member's "yes"."""

    kind: str  # book / cancel / return / change
    thing_id: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    note: str | None = None
    destination: str | None = None
    booking_id: int | None = None


@dataclass
class Outcome:
    reply: str
    pending: Pending | None = None


# --- reading times -------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "tues": 1, "wednesday": 2, "thursday": 3, "thurs": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}  # fmt: skip
_WD = r"(?:monday|tuesday|tues|wednesday|thursday|thurs|friday|saturday|sunday)"
_ORD = r"(?:st|nd|rd|th)?"
_AMPM = r"[ap]\.?m\.?"
_TOKENS = re.compile(
    r"(?P<now>\b(?:right\s+)?now\b|\bright\s+away\b|\bstraight\s+away\b)"
    r"|(?P<dur>\bfor\s+(?:(?P<dn>\d+(?:[.,]5)?)|(?P<dw>an?|one|two|three|four|five|six|seven"
    r"|eight|nine|ten|half\s+an?|a\s+couple\s+of))\s*"
    r"(?P<du>min(?:ute)?s?|h(?:ou)?rs?|hours?|h|days?|nights?|weeks?)\b)"
    r"|(?P<to>(?:\b(?:to|until|till|til|through|thru|up\s+to)\b|[-\u2013\u2014])\s*)"
    r"|(?P<from>\b(?:from|since|starting|between)\b)"
    # dates
    r"|(?:\bon\s+|\bthe\s+)?(?P<date>"
    r"(?P<dmy>\b(?P<d1>\d{1,2})\.(?P<m1>\d{1,2})\.(?P<y1>\d{4}|\d{2})?(?!\d))"
    r"|(?P<iso>\b(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})\b)"
    r"|(?P<rel>\b(?:today|tonight|tomorrow|day\s+after\s+tomorrow)\b)"
    rf"|(?P<wd>\b(?:(?P<next>next|this|coming)\s+)?(?P<wdn>{_WD})\b)"
    rf"|(?P<dm>\b(?P<d3>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<mon3>{_MONTH})(?:\s+(?P<y3>\d{{4}}))?\b)"
    rf"|(?P<md>\b(?P<mon4>{_MONTH})\s+(?P<d4>\d{{1,2}}){_ORD}(?:,?\s+(?P<y4>\d{{4}}))?\b))"
    # times
    r"|(?:\bat\s+|\bby\s+|\baround\s+)?(?P<time>"
    rf"(?P<hm>\b(?P<h1>\d{{1,2}}):(?P<mi1>\d{{2}})(?:\s*(?P<ap1>{_AMPM}))?)"
    rf"|(?P<hap>\b(?P<h2>\d{{1,2}})\s*(?P<ap2>{_AMPM}))"
    r"|(?P<noon>\b(?:noon|midday|midnight)\b))"
    r"|(?P<hat>\bat\s+(?P<h3>\d{1,2})(?:\s*o'?clock)?\b(?![.,:]?\d))",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a couple of": 2,
}  # fmt: skip


@dataclass
class _Part:
    date: date | None = None
    time: time | None = None
    midnight: bool = False  # "until midnight": the end of that day
    date_pos: int = -1
    time_pos: int = -1


def _year(y: str | None, month: int, day_: int, today: date) -> int:
    if y:
        return 2000 + int(y) if len(y) == 2 else int(y)
    # No year: this year, or next year if that day is over.
    try:
        this = date(today.year, month, day_)
    except ValueError:
        return today.year
    return today.year if this >= today else today.year + 1


def _make_date(year: int, month: int, day_: int) -> date:
    try:
        return date(year, month, day_)
    except ValueError:
        raise ChatTimeError(en.CHAT_BOOK_BAD_DATE) from None


def _date_of(m: re.Match[str], today: date) -> date:
    g = m.groupdict()
    if g["dmy"]:
        d, mo = int(g["d1"]), int(g["m1"])
        return _make_date(_year(g["y1"], mo, d, today), mo, d)
    if g["iso"]:
        return _make_date(int(g["y2"]), int(g["m2"]), int(g["d2"]))
    if g["rel"]:
        word = " ".join(g["rel"].lower().split())
        return today + timedelta(days={"tomorrow": 1, "day after tomorrow": 2}.get(word, 0))
    if g["wd"]:
        target = _WEEKDAYS[g["wdn"].lower()]
        delta = (target - today.weekday()) % 7
        if delta == 0 and (g["next"] or "").lower() == "next":
            delta = 7
        return today + timedelta(days=delta)
    if g["dm"] or g["md"]:
        d = int(g["d3"] or g["d4"])
        mo = _MONTHS[(g["mon3"] or g["mon4"])[:3].lower()]
        return _make_date(_year(g["y3"] or g["y4"], mo, d, today), mo, d)
    raise ChatTimeError(en.CHAT_BOOK_BAD_DATE)  # pragma: no cover - the regex has no other dates


def _time_of(m: re.Match[str]) -> tuple[time, bool]:
    """(the time, whether it is midnight at the end of a day)."""
    g = m.groupdict()
    if g["noon"]:
        word = g["noon"].lower()
        return (time(0), True) if word == "midnight" else (time(12), False)
    if g["hm"]:
        h, mi, ap = int(g["h1"]), int(g["mi1"]), g["ap1"]
    elif g["hap"]:
        h, mi, ap = int(g["h2"]), 0, g["ap2"]
    else:
        h, mi, ap = int(g["h3"]), 0, None
    if ap:
        if not 1 <= h <= 12:
            raise ChatTimeError(en.CHAT_BOOK_BAD_TIME)
        h = h % 12 + (12 if ap.lower().startswith("p") else 0)
    if h == 24 and mi == 0:
        return time(0), True
    if h > 23 or mi > 59:
        raise ChatTimeError(en.CHAT_BOOK_BAD_TIME)
    return time(h, mi), False


def _duration(m: re.Match[str]) -> timedelta:
    g = m.groupdict()
    if g["dn"]:
        n = float(g["dn"].replace(",", "."))
    else:
        word = " ".join(g["dw"].lower().split())
        n = 0.5 if word.startswith("half") else _NUMBER_WORDS[word]
    unit = g["du"].lower()
    if unit.startswith("m"):
        return timedelta(minutes=n)
    if unit.startswith("h"):
        return timedelta(hours=n)
    if unit.startswith(("d", "n")):
        return timedelta(days=n)
    return timedelta(weeks=n)


def time_spans(text: str) -> list[tuple[int, int]]:
    """Where the time words are in `text` (to read the rest without them)."""
    return [(m.start(), m.end()) for m in _real_tokens(text)]


def _real_tokens(text: str) -> list[re.Match[str]]:
    """The time tokens, without a "to"/"from" that isn't next to a time ("to town")."""
    found = list(_TOKENS.finditer(text))
    keep = []
    for i, m in enumerate(found):
        if m.group("to") or m.group("from"):
            nxt = found[i + 1] if i + 1 < len(found) else None
            if nxt is None or not (nxt.group("date") or nxt.group("time") or nxt.group("hat")):
                continue
            if not re.fullmatch(r"\s*", text[m.end() : nxt.start()]):
                continue
            if m.group("to") and m.group("to").strip() in "-\u2013\u2014":
                # "10:00-12:00": a dash only joins two times or dates.
                prev = found[i - 1] if i else None
                if prev is None or not (prev.group("date") or prev.group("time")):
                    continue
        keep.append(m)
    return keep


def parse_span(text: str, now: datetime, tz: ZoneInfo) -> Span:
    """Read when from the member's words: "from 01.12.26 to 13.12.26", "now", "till 17:00".

    Days are whole days in the community's time zone: "on 05.12.26" is that
    whole day, "01.12.26 to 13.12.26" includes the 13th. A time without a day is
    today, or tomorrow if it is already over. Raises ChatTimeError for a day or
    time that doesn't exist.
    """
    local_now = now.astimezone(tz).replace(second=0, microsecond=0)
    today = local_now.date()
    start, end = _Part(), _Part()
    part = start
    said_now = False
    duration: timedelta | None = None
    tokens = _real_tokens(text)
    for m in tokens:
        if m.group("now"):
            said_now = True
        elif m.group("dur"):
            duration = _duration(m)
        elif m.group("to"):
            part = end
        elif m.group("from"):
            part = start
        elif m.group("date"):
            if part.date is None:
                part.date, part.date_pos = _date_of(m, today), m.start()
        elif part.time is None:
            part.time, part.midnight = _time_of(m)
            part.time_pos = m.start()
    if not (said_now or duration or start.date or start.time or end.date or end.time):
        return Span()

    # "10:00 to 12:00 on Saturday": a day after both times belongs to both.
    if (
        start.date is None
        and start.time is not None
        and end.date is not None
        and end.date_pos > end.time_pos >= 0
    ):
        start.date = end.date

    start_is_now = False
    whole_day_start = False
    if start.date and start.time:
        s = datetime.combine(start.date + timedelta(days=start.midnight), start.time, tz)
    elif start.date:
        whole_day_start = True
        s = datetime.combine(start.date, time(0), tz)
        if start.date == today:
            s, start_is_now = local_now, True
    elif start.time:
        s = datetime.combine(today, start.time, tz)
        if s < local_now - cal.START_GRACE:
            s += timedelta(days=1)
    else:
        s, start_is_now = local_now, True

    e: datetime | None
    if end.date and end.time:
        e = datetime.combine(end.date + timedelta(days=end.midnight), end.time, tz)
    elif end.date:
        e = datetime.combine(end.date + timedelta(days=1), time(0), tz)
    elif end.time:
        e = datetime.combine(s.date() + timedelta(days=end.midnight), end.time, tz)
        if e <= s:
            e += timedelta(days=1)
    elif duration is not None:
        e = s + duration
    elif whole_day_start:
        e = datetime.combine(start.date + timedelta(days=1), time(0), tz)
    else:
        e = None
    return Span(True, s.astimezone(UTC), None if e is None else e.astimezone(UTC), start_is_now)


# --- reading the rest -------------------------------------------------------------


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


_WORD = re.compile(r"[^\W\d_][\w'\u2019-]*")


def _norm(word: str) -> str:
    """ "Theater's" -> "theater", "theatre" -> "theater", "saws" -> "saw"."""
    w = word.casefold().replace("\u2019", "'")
    w = re.sub(r"'s?$", "", w)
    if w.endswith("tre"):
        w = w[:-3] + "ter"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _one_edit(a: str, b: str) -> bool:
    """At most one letter added, dropped, changed or two swapped ("theatr", "workhsop")."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [k for k in range(len(a)) if a[k] != b[k]]
        if len(diff) == 1:
            return True
        x, y = diff[0], diff[-1]
        return len(diff) == 2 and y == x + 1 and a[x] == b[y] and a[y] == b[x]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:k] + long_[k + 1 :] == short for k in range(len(long_)))


def same_word(said: str, name: str) -> bool:
    """Whether a word the member wrote means a word of a thing's name.

    Spelling variants and plurals always count; one typo only in longer words,
    so "car" never matches "cart".
    """
    a, b = _norm(said), _norm(name)
    return a == b or (min(len(a), len(b)) >= 5 and _one_edit(a, b))


def _matches(text: str, name: str) -> list[tuple[int, int]]:
    """Where `name` (word by word, loosely) stands in `text`."""
    words = list(_WORD.finditer(text))
    want = [w.group() for w in _WORD.finditer(name)]
    if not want:
        return []
    out = []
    for k in range(len(words) - len(want) + 1):
        if all(same_word(words[k + n].group(), want[n]) for n in range(len(want))):
            out.append((words[k].start(), words[k + len(want) - 1].end()))
    return out


@dataclass(frozen=True)
class Found:
    things: list[Bookable]  # distinct, in the order named
    end: int = -1  # where the (first) thing's name ends in the text


def find_things(db: Session, text: str) -> Found:
    """The bookable things the text names, active ones first for a shared name.

    In "the theatre from … for a workshop" the Workshop is the purpose, not a
    second thing: a name after "for", following another name, doesn't count.
    """
    all_ = sorted(bookables.list_all(db), key=lambda t: (-len(t.name), not t.is_active))
    hits: list[tuple[int, int, Bookable]] = []
    taken: list[tuple[int, int]] = []
    seen_names: set[str] = set()
    for thing in all_:
        key = thing.name.casefold()
        if key in seen_names:  # an archived thing with an active namesake
            continue
        seen_names.add(key)
        for start, end in _matches(text, thing.name):
            if any(a < end and start < b for a, b in taken):
                continue
            taken.append((start, end))
            hits.append((start, end, thing))
    hits.sort(key=lambda h: h[0])
    if len({h[2].id for h in hits}) > 1:
        first_end = hits[0][1]
        cut = re.search(r"\bfor\b", text[first_end:], re.IGNORECASE)
        if cut is not None:
            limit = first_end + cut.start()
            hits = [h for h in hits if h[0] < limit]
    things: list[Bookable] = []
    for _, _, t in hits:
        if t not in things:
            things.append(t)
    return Found(things, hits[0][1] if hits else -1)


_ARTICLE = r"(?:(?:a|an|the|our|my|some)\s+)?"
_NOTE = re.compile(rf"\bfor\s+{_ARTICLE}(?P<note>[^,.;!?\n]+)", re.IGNORECASE)
_DEST = re.compile(
    rf"\bto\s+(?:(?:go|drive|get|run|head)\s+(?:to\s+)?)?{_ARTICLE}(?P<dest>[^,.;!?\n]+?)"
    r"(?=\s+for\b|\s*[,.;!?\n]|\s*$)",
    re.IGNORECASE,
)


def note_and_destination(text: str, thing: Bookable | None, after: int) -> tuple[str | None, ...]:
    """The note ("for a workshop") and, for a vehicle, where to ("to town"), after the name."""
    rest = _blank(text, time_spans(text))[max(after, 0) :]
    rest = re.sub(r"\b(?:on|at|from|and|please)\s*$", " ", rest.rstrip(), flags=re.IGNORECASE)
    destination = None
    if thing is not None and thing.type == "vehicle" and (d := _DEST.search(rest)):
        destination = " ".join(d.group("dest").split()) or None
        rest = rest[: d.start()] + " " * (d.end() - d.start()) + rest[d.end() :]
    note = None
    if n := _NOTE.search(rest):
        note = " ".join(n.group("note").split())
        note = re.sub(r"\s+(?:on|at|from|please)$", "", note, flags=re.IGNORECASE) or None
    return (note[: cal.NOTE_MAX] if note else None), (
        destination[: cal.DESTINATION_MAX] if destination else None
    )


def _other_member_named(db: Session, member: Member, text: str, action: str = "book") -> bool:
    """ "book the car for Frida", "as @nora", "on behalf of Ana": booking is only for the sender.

    "for him" / "for her" is someone else too. For cancelling, changing and giving
    back, "Ana's" anywhere means Ana's booking ("for Ana's birthday" stays a note
    when booking).
    """
    if re.search(r"\bfor\s+(?:him|her)\b", text, re.IGNORECASE):
        return True
    names: set[str] = set()
    for other in db.scalars(select(Member).where(Member.id != member.id)):
        names.add(other.matrix_user_id.removeprefix("@").split(":", 1)[0].casefold())
        if other.display_name:
            names.add(other.display_name.split()[0].casefold())
    own = {member.matrix_user_id.removeprefix("@").split(":", 1)[0].casefold()}
    if member.display_name:
        own.add(member.display_name.split()[0].casefold())
    for m in re.finditer(
        r"\b(?:for|on\s+behalf\s+of|as)\s+(?:@(?P<mx>[\w.=-]+)|(?P<w>[^\W\d_][\w-]*)(?P<poss>['\u2019]s)?)",
        text,
        re.IGNORECASE,
    ):
        if m.group("mx"):
            if m.group("mx").casefold() not in own:
                return True
        elif not m.group("poss") and m.group("w").casefold() in names - own:
            return True
    if action != "book":
        for m in re.finditer(r"(?:@(?P<mx>[\w.=-]+)|\b(?P<w>[^\W\d_][\w-]*))'s\b", text):
            word = (m.group("mx") or m.group("w")).casefold()
            if word in names - own:
                return True
        # "Ana is done with the saw", "Frida brought the car back": another member
        # is the one giving back, cancelling or changing, so it isn't the sender's
        # booking to touch. ("Ana, ..." addresses her and doesn't count.)
        for m in _SUBJECT.finditer(text):
            word = (m.group("mx") or m.group("w")).casefold()
            if word in names - own:
                return True
    return False


# A name or Matrix id opening the message or a sentence, not followed by a comma.
_SUBJECT = re.compile(
    r"(?:^|[.!?;]\s+)(?:@(?P<mx>[\w.=-]+)|(?P<w>[^\W\d_][\w-]*))(?:\s*:[^\s]+)?\b(?!\s*[,:])"
)


# --- rules -------------------------------------------------------------------------

_LIST = re.compile(
    r"^(?:what|which)\s+(?:things\s+)?(?:can|could)\s+(?:i|we|be)\s+(?:book|borrow|use|reserve"
    r"|booked|borrowed|reserved)\b"
    r"|^(?:list|show)\s+(?:me\s+)?(?:the\s+|all\s+)?(?:bookables|bookable\s+things|things"
    r"(?:\s+to\s+book)?)\b|^what\s+is\s+there\s+to\s+(?:book|borrow)\b",
    re.IGNORECASE,
)
_MINE = re.compile(
    r"^(?:(?:show|list|what\s+are)\s+(?:me\s+)?)?my\s+bookings\b|^what\s+(?:have|did)\s+i\s+book"
    r"|^what\s+do\s+i\s+have\s+(?:booked|borrowed)\b|^bookings\s*\??$",
    re.IGNORECASE,
)
_RETURN = re.compile(
    r"\b(?:brought|bring(?:ing)?|gave|give|giving|put|putting|took|taken|returned|have)\b"
    r"(?:\s+\S+){0,4}?\s+back\b|\breturn(?:ed|ing)?\b|\b(?:done|finished)\s+with\b"
    r"|^(?:i'?m\s+|i\s+am\s+)?back\s+with\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^(?:please\s+)?cancel\s+(?!that\b|it\b|this\b)"
    r"|\b(?:don'?t|do\s+not|won'?t|will\s+not|no\s+longer)\s+need\b",
    re.IGNORECASE,
)
# "I don't want the car", "not taking the saw": never a booking by the rules.
_NEGATED = re.compile(r"\b(?:don'?t|do\s+not|won'?t|will\s+not|not|no\s+longer|never)\b", re.I)
_CHANGE = re.compile(
    r"^(?:please\s+)?(?:(?:can|could)\s+i\s+)?(?:extend|keep|prolong|shorten|change|move)\b"
    r".*\b(?:until|till|til|to)\b",
    re.IGNORECASE,
)
_AVAIL = re.compile(
    r"^(?:is|are)\s+.+?\b(?:free|available|booked|taken|in\s+use|busy)\b"
    r"|^who\s+(?:has|have|took|is\s+using|booked|got)\b|^where\s+(?:is|are)\b"
    r"|^when\s+is\s+.+?\b(?:free|available)\b|^is\s+(?:anyone|anybody|someone)\s+using\b"
    r"|^(?:can|could|may)\s+(?:i|we)\s+(?:book|use|borrow|have|take|reserve)\b",
    re.IGNORECASE,
)
_BOOK = re.compile(
    r"\b(?:book|reserve|use|using|take|taking|borrow|borrowing|grab|grabbing|need|want|have"
    r"|get|getting)\b",
    re.IGNORECASE,
)
_REF = re.compile(r"\bbooking\s*#\s?(?P<ref>\d{1,9})\b|#\s?(?P<ref2>\d{1,9})\b", re.IGNORECASE)
_TAKE_NOW = re.compile(
    r"\b(?:take|taking|took|borrow|borrowing|grab|grabbing|getting)\b", re.IGNORECASE
)


# Characters that hide inside words ("F\u200belix"): zero-width spaces and joiners,
# word joiner, soft hyphen, byte-order mark.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\u00ad\ufeff"))


def _clean(text: str) -> str:
    # NFKC turns full-width and other look-alike letters into plain ones, so a
    # name can't slip past the checks below in disguise.
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    return chat_ledger.without_greeting(text.replace("\u2019", "'")).strip()


def parse_rule(db: Session, text: str) -> BookingRequest | None:
    """A BookingRequest for the common phrasings, or None (then the model decides)."""
    t = _clean(text)
    if _LIST.search(t):
        return BookingRequest("list")
    if _MINE.search(t):
        return BookingRequest("mine")
    named = bool(find_things(db, t).things)
    ref = _ref(t)
    if _RETURN.search(t) and (named or ref or re.search(r"\b(?:it|that)\b", t, re.IGNORECASE)):
        return BookingRequest("return", booking_ref=ref)
    if _CANCEL.search(t) and (named or ref or re.search(r"\bbooking\b", t, re.IGNORECASE)):
        return BookingRequest("cancel", booking_ref=ref)
    if not named and not ref:
        return None
    if _CHANGE.search(t):
        return BookingRequest("change", booking_ref=ref)
    if not named:
        return None
    if _AVAIL.search(t) or t.endswith("?"):
        return BookingRequest("availability")
    if _BOOK.search(t) and not _NEGATED.search(t):
        return BookingRequest("book")
    return None


def _ref(text: str) -> int | None:
    m = _REF.search(text)
    return int(m.group("ref") or m.group("ref2")) if m else None


# --- acting ------------------------------------------------------------------------


def _who(b: Booking) -> str:
    return cal.member_name(b.member)


def _when(b: Booking, tz: ZoneInfo) -> str:
    return when_range(b.starts_at, b.ends_at, tz)


def _details(note: str | None, destination: str | None) -> str:
    out = ""
    if destination:
        out += en.CHAT_BOOK_DEST.format(destination=destination)
    if note:
        out += en.CHAT_BOOK_NOTE.format(note=note)
    return out


def bookables_for_prompt(db: Session) -> str:
    """The bookable things for the model: names and types only."""
    lines = [f"- {t.name} ({t.type})" for t in bookables.list_active(db)]
    return "\n".join(lines) or "- (none configured yet)"


def _list(db: Session) -> str:
    things = bookables.list_active(db)
    if not things:
        return en.CHAT_BOOK_NOTHING_CONFIGURED
    lines = []
    for t in things:
        where = en.CHAT_BOOK_WHERE.format(location=t.location) if t.location else ""
        lines.append(f"- {t.name} ({en.CHAT_BOOK_TYPES[t.type]}{where})")
    return en.CHAT_BOOK_LIST.format(lines="\n".join(lines))


def _which(db: Session, found: list[Bookable], *, verb: str) -> str:
    if found:
        return en.CHAT_BOOK_WHICH.format(names=" or ".join(t.name for t in found))
    things = bookables.list_active(db)
    if not things:
        return en.CHAT_BOOK_NOTHING_CONFIGURED
    return en.CHAT_BOOK_WHAT.format(verb=verb, names=", ".join(t.name for t in things))


def _resolve(db: Session, req: BookingRequest, text: str) -> Found:
    if req.thing:
        found = find_things(db, req.thing)
        if found.things:
            # Where the name stands in the message itself, to read the note after it.
            spots = _matches(text, found.things[0].name)
            return Found(found.things, spots[0][1] if spots else -1)
    return find_things(db, text)


def open_bookings(db: Session, member: Member, now: datetime) -> list[Booking]:
    """The sender's bookings that aren't over: upcoming, running, or a thing still with them."""
    q = (
        select(Booking)
        .join(Bookable)
        .where(
            Booking.member_id == member.id,
            Booking.cancelled_at.is_(None),
            Booking.returned_at.is_(None),
            or_(Booking.ends_at.is_(None), Booking.ends_at > now, Bookable.type != "space"),
        )
        .order_by(Booking.starts_at, Booking.id)
    )
    return list(db.scalars(q))


def _mine(db: Session, member: Member, now: datetime, tz: ZoneInfo) -> str:
    found = open_bookings(db, member, now)
    if not found:
        return en.CHAT_BOOK_MINE_NONE
    return en.CHAT_BOOK_MINE.format(lines="\n".join(_line(b, tz) for b in found))


def _line(b: Booking, tz: ZoneInfo) -> str:
    return f"- #{b.id} {b.bookable.name}, {_when(b, tz)}{_details(b.note, b.destination)}"


def next_free(
    db: Session, thing: Bookable, starts_at: datetime, ends_at: datetime | None, now: datetime
) -> tuple[datetime, datetime | None] | None:
    """The nearest time from `starts_at` on when `thing` is free for as long as asked."""
    tz = cal.timezone(db)
    blocks = cal._blocks_until(db, starts_at, ends_at)
    length = blocks - starts_at
    whole_days = (
        ends_at is not None
        and starts_at.astimezone(tz).time() == time(0)
        and ends_at.astimezone(tz).time() == time(0)
    )
    t = starts_at
    for _ in range(FREE_SLOT_TRIES):
        in_way = cal.overlapping(db, thing, t, t + length)
        if not in_way:
            return t, None if ends_at is None else t + (ends_at - starts_at)
        t = max(b.blocks_until for b in in_way)
        if whole_days:
            local = t.astimezone(tz)
            if local.time() != time(0):
                t = datetime.combine(local.date() + timedelta(days=1), time(0), tz).astimezone(UTC)
        if t > now + cal.MAX_AHEAD:
            return None
    return None


def _busy_line(db: Session, thing: Bookable, blocking: list[Booking], tz: ZoneInfo) -> str:
    first = blocking[0] if blocking else None
    if first is None:
        return en.BOOKING_CONFLICT.format(name=thing.name)
    if first.ends_at is None:
        since = when_range(first.starts_at, None, tz).removeprefix("from ")
        return en.BOOKING_WITH.format(name=thing.name, who=_who(first), since=since)
    return en.CHAT_BOOK_BUSY.format(name=thing.name, who=_who(first), when=_when(first, tz))


def _offer(
    db: Session,
    member: Member,
    thing: Bookable,
    starts_at: datetime,
    ends_at: datetime | None,
    blocking: list[Booking],
    note: str | None,
    destination: str | None,
    now: datetime,
) -> Outcome:
    tz = cal.timezone(db)
    if blocking and blocking[0].member_id == member.id:
        return Outcome(
            en.CHAT_BOOK_YOURS_ALREADY.format(
                name=thing.name, when=_when(blocking[0], tz), lower=thing.name.lower()
            )
        )
    busy = _busy_line(db, thing, blocking, tz)
    slot = next_free(db, thing, starts_at, ends_at, now)
    if slot is None:
        return Outcome(en.CHAT_BOOK_NO_SLOT.format(busy=busy))
    s, e = slot
    return Outcome(
        en.CHAT_BOOK_OFFER.format(busy=busy, when=when_range(s, e, tz)),
        Pending("book", thing.id, s, e, note, destination),
    )


def _book_now(
    db: Session,
    member: Member,
    thing: Bookable,
    starts_at: datetime,
    ends_at: datetime | None,
    note: str | None,
    destination: str | None,
    now: datetime,
) -> Outcome:
    tz = cal.timezone(db)
    try:
        booking = cal.book(
            db,
            member,
            thing,
            starts_at,
            ends_at,
            channel="matrix",
            note=note,
            destination=destination,
            now=now,
        )
    except cal.Conflict as e:
        db.rollback()
        return _offer(db, member, thing, starts_at, ends_at, e.blocking, note, destination, now)
    except cal.BookingError as e:
        db.rollback()
        return Outcome(en.CHAT_BOOK_REFUSED.format(reason=str(e)))
    db.commit()
    hint = en.CHAT_BOOK_UNDO_HINT
    if booking.ends_at is None or booking.starts_at <= now:
        hint = en.CHAT_BOOK_RETURN_HINT.format(name=thing.name.lower())
    reply = en.CHAT_BOOK_DONE.format(
        name=thing.name,
        when=_when(booking, tz),
        details=_details(booking.note, booking.destination),
    )
    return Outcome(f"{reply} {hint}")


def _context(db: Session, req: BookingRequest, text: str) -> tuple[Found, Bookable | None]:
    found = _resolve(db, req, text)
    return found, found.things[0] if len(found.things) == 1 else None


def _span(
    db: Session, text: str, earlier: list[str], thing: Bookable, now: datetime, tz: ZoneInfo
) -> tuple[Span, str]:
    """The time said in this message, else in the member's last message that says one.

    "is the theatre free on monday at 12?" … "book it": the model knows "it" is
    the theatre from the chat; the time comes from the earlier message. Only one
    that names this thing or no thing at all counts.
    """
    span = parse_span(text, now, tz)
    if span.said:
        return span, text
    for before in earlier:
        try:
            found = parse_span(before, now, tz)
        except ChatTimeError:
            continue
        if not found.said:
            continue
        named = [t for t in bookables.list_all(db) if _matches(before, t.name)]
        if not named or thing in named:
            return found, before
        break
    return span, text


def _note(text: str, thing: Bookable, found: Found, said_in: str) -> tuple[str | None, ...]:
    note, destination = note_and_destination(text, thing, found.end)
    if note is None and destination is None and said_in != text:
        spots = _matches(said_in, thing.name)
        note, destination = note_and_destination(said_in, thing, spots[0][1] if spots else -1)
    return note, destination


def _book(
    db: Session,
    member: Member,
    req: BookingRequest,
    text: str,
    now: datetime,
    earlier: tuple[str, ...] | list[str] = (),
) -> Outcome:
    tz = cal.timezone(db)
    found, thing = _context(db, req, text)
    if thing is None:
        return Outcome(_which(db, found.things, verb="book"))
    if not thing.is_active:
        return Outcome(en.BOOKING_ARCHIVED.format(name=thing.name).capitalize() + ".")
    try:
        span, said_in = _span(db, text, list(earlier), thing, now, tz)
    except ChatTimeError as e:
        return Outcome(str(e))
    if not span.said:
        if _TAKE_NOW.search(text):
            span = Span(True, now.replace(second=0, microsecond=0), None, True)
        else:
            return Outcome(en.CHAT_BOOK_WHEN.format(name=thing.name))
    note, destination = _note(text, thing, found, said_in)
    if req.from_model:
        # The model's own note or destination only if the member wrote it.
        note = note or _grounded(req.note, text, "for")
        if thing.type == "vehicle":
            destination = destination or _grounded(req.destination, text, "to")
    if thing.type != "vehicle":
        destination = None
    starts_at = span.starts_at or now
    ends_at = span.ends_at
    if req.from_model and (not _meant(thing, text, req) or said_in != text):
        # The model had to guess the thing or the time from the chat: echo and ask.
        avail = cal.availability(db, thing, starts_at, ends_at)
        if not avail.free:
            out = _offer(
                db, member, thing, starts_at, ends_at, avail.bookings, note, destination, now
            )
        else:
            out = Outcome(
                en.CHAT_BOOK_CONFIRM.format(
                    name=thing.name,
                    when=when_range(starts_at, ends_at, tz),
                    details=_details(note, destination),
                ),
                Pending("book", thing.id, starts_at, ends_at, note, destination),
            )
    else:
        out = _book_now(db, member, thing, starts_at, ends_at, note, destination, now)
    return out


def named_here(thing: Bookable, text: str) -> bool:
    """Whether the member named `thing` in this message ("the theater" counts)."""
    return bool(_matches(text, thing.name))


def _meant(thing: Bookable, text: str, req: BookingRequest) -> bool:
    """Named here, or the one thing COMMONS' last message was about ("I returned it").

    After a list ("#1 Theatre … #3 Saw") nothing counts as meant: that's a guess.
    """
    if named_here(thing, text):
        return True
    if not req.last_reply:
        return False
    db = object_session(thing)
    return db is not None and find_things(db, req.last_reply).things == [thing]


def _picked_here(booking: Booking, text: str, req: BookingRequest) -> bool:
    """Whether the member clearly meant this booking: its thing, or its number ("#12")."""
    return _meant(booking.bookable, text, req) or _ref(text) == booking.id


def _grounded(value: str | None, text: str, cue: str) -> str | None:
    """The model's note or destination, only where the member wrote "for …" / "to …" it.

    The live model fills a note with the whole message ("taking the saw") or
    with a word from the prompt's examples ("workshop"); neither is kept.
    """
    if not value:
        return None
    value = " ".join(value.split()).removeprefix(f"{cue} ").strip()
    pattern = rf"\b{cue}\s+{_ARTICLE}{re.escape(value)}"
    return value if value and re.search(pattern, " ".join(text.split()), re.IGNORECASE) else None


def _availability(
    db: Session,
    member: Member,
    req: BookingRequest,
    text: str,
    now: datetime,
    earlier: tuple[str, ...] | list[str] = (),
) -> Outcome:
    found, thing = _context(db, req, text)
    if thing is None:
        return Outcome(_which(db, found.things, verb="ask about"))
    return _availability_of(db, member, thing, found, text, now, earlier)


def _availability_of(
    db: Session,
    member: Member,
    thing: Bookable,
    found: Found,
    text: str,
    now: datetime,
    earlier: tuple[str, ...] | list[str],
) -> Outcome:
    tz = cal.timezone(db)
    if not thing.is_active:
        return Outcome(en.BOOKING_ARCHIVED.format(name=thing.name).capitalize() + ".")
    try:
        span, said_in = _span(db, text, list(earlier), thing, now, tz)
    except ChatTimeError as e:
        return Outcome(str(e))
    where = en.CHAT_BOOK_WHERE_IS.format(location=thing.location) if thing.location else ""
    if not span.said or (span.start_is_now and span.ends_at is None):
        holders = cal.who_has(db, thing, now)
        if holders:
            lines = [
                en.CHAT_BOOK_WITH_NOW.format(name=thing.name, who=_who(b), when=_when(b, tz))
                for b in holders
            ]
            reply = " ".join(lines)
        else:
            reply = en.CHAT_BOOK_FREE_NOW.format(name=thing.name, where=where)
        following = cal.next_booking(db, thing, now)
        if following is not None:
            reply += " " + en.CHAT_BOOK_NEXT.format(who=_who(following), when=_when(following, tz))
        return Outcome(reply)
    if span.starts_at is None:  # pragma: no cover - a said span always has a start
        return Outcome(en.ASSISTANT_NOT_UNDERSTOOD)
    avail = cal.availability(db, thing, span.starts_at, span.ends_at)
    note, destination = _note(text, thing, found, said_in)
    if thing.type != "vehicle":
        destination = None
    if not avail.free:
        out = _offer(
            db, member, thing, span.starts_at, span.ends_at, avail.bookings, note, destination, now
        )
    else:
        out = Outcome(
            en.CHAT_BOOK_FREE_THEN.format(
                name=thing.name, when=when_range(span.starts_at, span.ends_at, tz)
            ),
            Pending("book", thing.id, span.starts_at, span.ends_at, note, destination),
        )
    return out


def _pick(
    db: Session,
    member: Member,
    req: BookingRequest,
    text: str,
    candidates: list[Booking],
    *,
    verb: str,
    none_msg: str,
) -> Booking | str:
    """The one booking the member means, or a question back."""
    tz = cal.timezone(db)
    if req.booking_ref is not None:
        b = cal.get(db, req.booking_ref)
        if b is None or b.member_id != member.id:
            return en.CHAT_BOOK_NOT_YOURS.format(ref=req.booking_ref)
        return b
    found = _resolve(db, req, text)
    if len(found.things) > 1:
        return _which(db, found.things, verb=verb)
    if found.things:
        thing = found.things[0]
        candidates = [b for b in candidates if b.bookable_id == thing.id]
        if not candidates:
            return none_msg.format(name=thing.name)
    elif not candidates:
        return none_msg.format(name=en.CHAT_BOOK_ANYTHING)
    if len(candidates) > 1:
        return en.CHAT_BOOK_SEVERAL.format(
            lines="\n".join(_line(b, tz) for b in candidates), verb=verb, ref=candidates[0].id
        )
    return candidates[0]


def _return(db: Session, member: Member, req: BookingRequest, text: str, now: datetime) -> Outcome:
    started = [b for b in open_bookings(db, member, now) if b.starts_at <= now]
    picked = _pick(
        db, member, req, text, started, verb="give back", none_msg=en.CHAT_BOOK_NOT_WITH_YOU
    )
    if isinstance(picked, str):
        return Outcome(picked)
    if req.from_model and not _picked_here(picked, text, req):
        return Outcome(
            en.CHAT_BOOK_CONFIRM_RETURN.format(name=picked.bookable.name),
            Pending("return", booking_id=picked.id),
        )
    return give_back(db, member, picked, now)


def give_back(db: Session, member: Member, booking: Booking, now: datetime) -> Outcome:
    try:
        cal.return_item(db, member, booking, now=now)
    except cal.BookingError as e:
        db.rollback()
        return Outcome(en.CHAT_BOOK_REFUSED.format(reason=str(e)))
    db.commit()
    return Outcome(
        en.CHAT_BOOK_RETURNED.format(
            name=booking.bookable.name, took=_took(now - booking.starts_at)
        )
    )


def _took(d: timedelta) -> str:
    minutes = max(1, int(d.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} days"


def _cancel(db: Session, member: Member, req: BookingRequest, text: str, now: datetime) -> Outcome:
    upcoming = [b for b in open_bookings(db, member, now) if b.starts_at > now]
    picked = _pick(
        db, member, req, text, upcoming, verb="cancel", none_msg=en.CHAT_BOOK_NO_UPCOMING
    )
    if isinstance(picked, str):
        return Outcome(picked)
    tz = cal.timezone(db)
    if req.from_model and not _picked_here(picked, text, req):
        return Outcome(
            en.CHAT_BOOK_CONFIRM_CANCEL.format(name=picked.bookable.name, when=_when(picked, tz)),
            Pending("cancel", booking_id=picked.id),
        )
    return cancel_booking(db, member, picked, now)


def cancel_booking(db: Session, member: Member, booking: Booking, now: datetime) -> Outcome:
    tz = cal.timezone(db)
    if booking.starts_at <= now and booking.member_id == member.id:
        return Outcome(en.CHAT_BOOK_ALREADY_STARTED.format(name=booking.bookable.name.lower()))
    try:
        cal.cancel(db, member, booking, now=now)
    except cal.BookingError as e:
        db.rollback()
        return Outcome(en.CHAT_BOOK_REFUSED.format(reason=str(e)))
    db.commit()
    return Outcome(
        en.CHAT_BOOK_CANCELLED.format(name=booking.bookable.name, when=_when(booking, tz))
    )


def _change(db: Session, member: Member, req: BookingRequest, text: str, now: datetime) -> Outcome:
    tz = cal.timezone(db)
    picked = _pick(
        db,
        member,
        req,
        text,
        open_bookings(db, member, now),
        verb="change",
        none_msg=en.CHAT_BOOK_NO_BOOKING,
    )
    if isinstance(picked, str):
        return Outcome(picked)
    try:
        # "keep the saw until 14:00": a time without a day counts from the booking's start.
        span = parse_span(_until_only(text), max(now, picked.starts_at), tz)
    except ChatTimeError as e:
        return Outcome(str(e))
    if not span.said or span.ends_at is None:
        return Outcome(en.CHAT_BOOK_CHANGE_WHEN.format(name=picked.bookable.name))
    new_end = span.ends_at
    if new_end <= picked.starts_at:
        return Outcome(en.CHAT_BOOK_REFUSED.format(reason=en.BOOKING_END_BEFORE_START))
    if req.from_model and not _picked_here(picked, text, req):
        return Outcome(
            en.CHAT_BOOK_CONFIRM_CHANGE.format(
                name=picked.bookable.name, when=when_range(picked.starts_at, new_end, tz)
            ),
            Pending("change", booking_id=picked.id, ends_at=new_end),
        )
    return change_end(db, member, picked, new_end, now)


def _until_only(text: str) -> str:
    """ "extend the car booking to Sunday": every time word here is the new end."""
    for m in _real_tokens(text):
        if m.group("to"):
            return text[m.start() :]
    return "until " + text


def change_end(
    db: Session, member: Member, booking: Booking, ends_at: datetime, now: datetime
) -> Outcome:
    tz = cal.timezone(db)
    try:
        cal.change(db, member, booking, ends_at=ends_at, now=now)
    except cal.Conflict as e:
        db.rollback()
        return Outcome(
            en.CHAT_BOOK_CHANGE_BUSY.format(busy=_busy_line(db, booking.bookable, e.blocking, tz))
        )
    except cal.BookingError as e:
        db.rollback()
        return Outcome(en.CHAT_BOOK_REFUSED.format(reason=str(e)))
    db.commit()
    return Outcome(en.CHAT_BOOK_CHANGED.format(name=booking.bookable.name, when=_when(booking, tz)))


def handle(
    db: Session,
    member: Member,
    req: BookingRequest,
    text: str,
    now: datetime,
    earlier: tuple[str, ...] | list[str] = (),
) -> Outcome:
    """Answer one booking request from `member`, acting only as them.

    `earlier` are the member's previous messages in this chat, newest first: a
    follow-up that leaves out the time ("book it") takes it from there.
    """
    text = _clean(text)
    if req.about_someone_else or (
        req.action in ("book", "cancel", "change", "return")
        and _other_member_named(db, member, text, req.action)
    ):
        return Outcome(en.CHAT_BOOK_ONLY_YOU)
    tz = cal.timezone(db)
    if req.action == "list":
        return Outcome(_list(db))
    if req.action == "mine":
        return Outcome(_mine(db, member, now, tz))
    if req.action == "availability":
        return _availability(db, member, req, text, now, earlier)
    if req.action == "return":
        return _return(db, member, req, text, now)
    if req.action == "cancel":
        return _cancel(db, member, req, text, now)
    if req.action == "change":
        return _change(db, member, req, text, now)
    return _book(db, member, req, text, now, earlier)


def confirm(db: Session, member: Member, pending: Pending, now: datetime) -> Outcome:
    """The member said yes: do what was offered, with every check again."""
    if pending.kind == "book":
        thing = bookables.get(db, pending.thing_id) if pending.thing_id else None
        if thing is None:
            return Outcome(en.ASSISTANT_NOT_UNDERSTOOD)
        if not thing.is_active:
            return Outcome(en.BOOKING_ARCHIVED.format(name=thing.name).capitalize() + ".")
        starts_at = pending.starts_at or now
        if starts_at < now and pending.ends_at is None:
            starts_at = now.replace(second=0, microsecond=0)
        return _book_now(
            db, member, thing, starts_at, pending.ends_at, pending.note, pending.destination, now
        )
    booking = cal.get(db, pending.booking_id) if pending.booking_id else None
    if booking is None or booking.member_id != member.id:
        return Outcome(en.ASSISTANT_NOT_UNDERSTOOD)
    if pending.kind == "return":
        return give_back(db, member, booking, now)
    if pending.kind == "cancel":
        return cancel_booking(db, member, booking, now)
    if pending.ends_at is None:
        return Outcome(en.ASSISTANT_NOT_UNDERSTOOD)
    return change_end(db, member, booking, pending.ends_at, now)
