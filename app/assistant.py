"""Chat assistant: plain-language messages from Matrix → one intent → normal code.

The model proposes, code decides:

1. Simple rules catch the common phrasings without calling the model at all
   (fast, and still works when the model is down).
2. Otherwise the local model maps the message to ONE allow-listed intent, as JSON
   that llama.cpp forces into INTENT_SCHEMA.
3. Code validates the proposal, acts as the sending member with that member's
   rights, calls the same service functions the web uses, and writes the reply.
   The model's own text is only used for small talk ("chat").

Member messages go only into the user turn, never into the system prompt.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import (
    chat_booking,
    chat_fix,
    chat_ledger,
    chat_levels,
    community,
    community_room,
    decision_log,
    impact,
    members,
    vote_chat,
)
from app.chat_booking import BookingRequest, Pending
from app.chat_fix import FixRequest
from app.chat_ledger import LogRequest
from app.chat_levels import Question
from app.config import Settings
from app.i18n import en
from app.llm import LLMError
from app.matrix_admin import MatrixAdminError
from app.models import AuthEvent, Member, utcnow
from app.vote_chat import BallotRequest, CancelRequest, VoteRequest
from app.weather import Forecast, describe

INTENTS = (
    "set_display_name",
    "show_profile",
    "my_contributions",
    "weather",
    "log_entry",
    "correct_entry",
    "show_levels",
    "show_history",
    "show_votes",
    # A ballot in the member's own words; code finds vote and option and asks first.
    "vote",
    "book",
    "check_availability",
    "my_bookings",
    "list_bookables",
    "return_item",
    "cancel_booking",
    "change_booking",
    "help",
    "chat",
)

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        # Whose data the request is about. The tools only ever act on the sender;
        # this lets the model say "someone else" so code can refuse instead of guessing.
        "about": {"type": "string", "enum": ["me", "someone_else"]},
        "display_name": {"type": ["string", "null"], "maxLength": 100},
        # log_entry: only words; code finds the resource and checks everything.
        "resource": {"type": ["string", "null"], "maxLength": 60},
        "entry_type": {"type": ["string", "null"], "enum": ["reading", "use", "add", None]},
        "amount": {"type": ["number", "null"]},
        "unit": {"type": ["string", "null"], "maxLength": 20},
        "day": {"type": ["string", "null"], "enum": ["today", "yesterday", None]},
        "time": {"type": ["string", "null"], "pattern": "^[0-9]{1,2}:[0-9]{2}$"},
        "note": {"type": ["string", "null"], "maxLength": 140},
        # correct_entry: the entry number the member wrote ("#42"), if any.
        "entry_ref": {"type": ["integer", "null"], "minimum": 1},
        # show_history: how many entries ("last 5 firewood entries").
        "count": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
        # bookings: only words; code finds the thing and reads the times itself.
        "bookable": {"type": ["string", "null"], "maxLength": 60},
        "destination": {"type": ["string", "null"], "maxLength": 100},
        "booking_ref": {"type": ["integer", "null"], "minimum": 1},
        # vote: only the member's words; code finds the vote and the option.
        "vote_ref": {"type": ["integer", "null"], "minimum": 1},
        "vote_topic": {"type": ["string", "null"], "maxLength": 60},
        "vote_choice": {"type": ["string", "null"], "maxLength": 60},
        "reply": {"type": ["string", "null"], "maxLength": 1500},
    },
    "required": [
        "intent",
        "about",
        "display_name",
        "resource",
        "entry_type",
        "amount",
        "unit",
        "day",
        "time",
        "note",
        "entry_ref",
        "count",
        "bookable",
        "destination",
        "booking_ref",
        "vote_ref",
        "vote_topic",
        "vote_choice",
        "reply",
    ],
    "additionalProperties": False,
}

# The chat so far, as the bot reads it from the Matrix room (reminders included).
HISTORY_MESSAGES = 10
HISTORY_CHARS = 6000
MAX_ROOMS = 500
MAX_REPLY_CHARS = 1500
# An offer ("Shall I book it? (yes/no)") waits this long for the answer.
PENDING_SECONDS = 600

# Model intent -> chat_booking action.
BOOKING_INTENTS = {
    "book": "book",
    "check_availability": "availability",
    "my_bookings": "mine",
    "list_bookables": "list",
    "return_item": "return",
    "cancel_booking": "cancel",
    "change_booking": "change",
}
_YES = re.compile(
    r"^(?:y|yes|yeah|yep|yup|yea|ya|jep|jup|ja|jo|yo|sure|ok|okay|k|please|yes,?\s+please"
    r"|do\s+it|book\s+it|go\s+ahead|confirm|correct|right|that'?s\s+right|\U0001f44d|\u2705)"
    r"\W*$",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^(?:n|no|nope|nah|nein|nee|n\u00f6|no,?\s+thanks?|don'?t|cancel|never\s?mind|stop"
    r"|forget\s+it|leave\s+it|\U0001f44e|\u274c)\W*$",
    re.IGNORECASE,
)
_HELP = {"help", "!help", "?"}
# Only unambiguous phrasings ("call me later" is not a name change; the model decides that).
_SET_NAME = re.compile(
    r"^(?:please\s+)?(?:change|set|update)\s+my\s+(?:display\s+)?name\s+to\s+(?P<name>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_SHOW_PROFILE = re.compile(
    r"^(?:who\s+am\s+i|what(?:'s|\s+is)\s+my\s+name|my\s+profile|whoami)\s*[?.!]*$",
    re.IGNORECASE,
)
# "my badges", "what have I contributed?": the member's own contribution summary.
_MY_CONTRIBUTIONS = re.compile(
    r"^(?:(?:show\s+(?:me\s+)?)?my\s+(?:badges|contributions|impact)"
    r"|what\s+(?:have\s+i|did\s+i)\s+(?:contribute|contributed|do\s+for\s+the\s+community)"
    r"|what\s+badges\s+do\s+i\s+have)\s*[?.!]*$",
    re.IGNORECASE,
)
# Short weather questions ("weather?", "what's the weather like tomorrow?"). Anything
# longer or less clear ("will it rain later?") goes to the model.
_WEATHER = re.compile(
    r"^(?:(?:how(?:'s|\s+is)|what(?:'s|\s+is))\s+the\s+)?(?:weather|forecast)"
    r"(?:\s+(?:like|today|tomorrow|now|forecast|at\s+the\s+outpost))*\s*[?.!]*$",
    re.IGNORECASE,
)


class JSONCompleter(Protocol):
    async def complete_json(
        self, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class DisplayNames(Protocol):
    async def get_display_name(self, user_id: str) -> str | None: ...
    async def set_display_name(self, user_id: str, name: str) -> None: ...


class Weather(Protocol):
    enabled: bool
    forecast: Forecast | None


@dataclass(frozen=True)
class Intent:
    name: str
    about_someone_else: bool = False
    display_name: str | None = None
    reply: str | None = None
    log: LogRequest | None = None
    fix: FixRequest | None = None
    question: Question | None = None
    vote: VoteRequest | None = None
    cancel: CancelRequest | None = None
    ballot: BallotRequest | None = None
    booking: BookingRequest | None = None
    source: str = "rules"  # or "model"


def parse_rules(text: str) -> Intent | None:
    """The fast path: exact phrasings that need no model."""
    if m := _SET_NAME.match(text):
        return Intent("set_display_name", display_name=m.group("name"))
    if _SHOW_PROFILE.match(text):
        return Intent("show_profile")
    if _MY_CONTRIBUTIONS.match(text):
        return Intent("my_contributions")
    if _WEATHER.match(text):
        return Intent("weather")
    return vote_rule(text)


def vote_rule(text: str) -> Intent | None:
    """Votes in chat: strict phrasings, handled without the model."""
    cancel = vote_chat.parse_cancel(text)
    if cancel is not None:
        return Intent("cancel_vote", cancel=cancel)
    vote = vote_chat.parse(text)
    if vote is not None:
        return Intent("cast_vote", vote=vote) if vote.rest else Intent("show_votes")
    return None


# Safety net for a small model: "Adelia's name", "his name", "their name"
# mean the request is about someone else, whatever the model said.
_OTHER_PERSON = re.compile(
    r"\b(?:(?!my\b|let\b|it\b|that\b|what\b|who\b|there\b|here\b)\w+['\u2019]s"
    r"|his|her|their)\s+(?:display\s+)?name\b",
    re.IGNORECASE,
)


def mentions_someone_else(text: str) -> bool:
    return bool(_OTHER_PERSON.search(text))


# "log it for @adelia", "as Nora", "on behalf of Frida": logging is always as the
# sender, so a request naming someone else is refused instead of half-obeyed.
_FOR_SOMEONE_ELSE = re.compile(
    r"\b[Oo]n\s+behalf\s+of\b|\b(?:[Aa]s|[Ff]or)\s+(?:@\w|[A-Z][a-z]+\b(?!\s+(?:tank|wood)))"
)


def logs_for_someone_else(text: str) -> bool:
    return bool(_FOR_SOMEONE_ELSE.search(text))


def _str(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] or None if isinstance(value, str) else None


def log_request_from_model(data: dict[str, Any], text: str) -> LogRequest:
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int | float):
        amount = None
    entry_type = data.get("entry_type")
    day = data.get("day")
    proposal = LogRequest(
        resource=_str(data.get("resource"), 60),
        entry_type=entry_type if entry_type in ("reading", "use", "add") else None,
        amount=float(amount) if amount is not None else None,
        unit=_str(data.get("unit"), 20),
        day=day if day in ("today", "yesterday") else None,
        time=_str(data.get("time"), 5),
        note=_str(data.get("note"), 140),
        about_someone_else=data.get("about") == "someone_else" or logs_for_someone_else(text),
    )
    return chat_ledger.grounded(proposal, text)


# "undo Frida's entry": without an entry number, undo only ever reaches the
# sender's own entries, so a request naming someone is answered, not half-obeyed.
_SOMEONES = re.compile(r"\b(?!It\b|That\b|What\b|There\b)[A-Z][a-z]+['\u2019]s\b|@\w")


def names_someone(text: str) -> bool:
    return bool(_SOMEONES.search(text))


# Words that say something logged was wrong, for a correction without a number.
_FIX_CUE = re.compile(
    r"\b(?:wrong|mistake|undo|correct(?:ion)?|fix|should\s+(?:be|have\s+been)|meant|oops"
    r"|take\s+(?:it|that)\s+back|didn'?t\s+happen|not\s+right)\b",
    re.IGNORECASE,
)


def fix_request_from_model(data: dict[str, Any], text: str) -> FixRequest:
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int | float):
        amount = None
    ref = data.get("entry_ref")
    if isinstance(ref, bool) or not isinstance(ref, int):
        ref = None
    proposal = FixRequest(
        entry_ref=ref,
        amount=float(amount) if amount is not None else None,
        resource=_str(data.get("resource"), 60),
        unit=_str(data.get("unit"), 20),
        about_someone_else=data.get("about") == "someone_else" or names_someone(text),
    )
    return chat_fix.grounded(proposal, text)


def intent_from_model(data: dict[str, Any], text: str = "") -> Intent | None:
    """Validate the model's proposal. Anything off the allow-list is ignored."""
    name = data.get("intent")
    if name not in INTENTS:
        return None
    about = data.get("about")
    if about not in ("me", "someone_else"):
        return None
    about_someone_else = about == "someone_else" or (
        name in ("set_display_name", "show_profile") and mentions_someone_else(text)
    )
    display_name = data.get("display_name")
    reply = data.get("reply")
    display_name = display_name if isinstance(display_name, str) else None
    reply = reply.strip() if isinstance(reply, str) else None
    if name == "set_display_name" and not display_name:
        return None
    if name == "chat" and not reply:
        return None
    fix = fix_request_from_model(data, text) if name == "correct_entry" else None
    vague = fix is not None and not (fix.amount is not None or fix.entry_ref or fix.undo)
    if vague and not _FIX_CUE.search(text):
        return None  # "Jep", "what do you mean?": nothing here says an entry was wrong
    return Intent(
        name,
        about_someone_else=about_someone_else,
        display_name=display_name,
        reply=reply,
        log=log_request_from_model(data, text) if name == "log_entry" else None,
        fix=fix,
        question=question_from_model(data) if name in ("show_levels", "show_history") else None,
        booking=booking_request_from_model(data, about_someone_else)
        if name in BOOKING_INTENTS
        else None,
        ballot=ballot_request_from_model(data, text) if name == "vote" else None,
        source="model",
    )


_VOTE_NUMBER = re.compile(r"(?:\bvote|\bproposal|#)\s*#?(\d+)\b", re.IGNORECASE)


def ballot_request_from_model(data: dict[str, Any], text: str = "") -> BallotRequest:
    """Keep only what the message itself says: a small model copies from its examples."""
    written = _VOTE_NUMBER.search(text)
    ref = int(written.group(1)) if written else None  # the number, as the member wrote it
    topic = _str(data.get("vote_topic"), 60)
    if topic and not set(re.findall(r"[^\W\d_]{3,}", topic.casefold())) & set(
        re.findall(r"[^\W\d_]{3,}", text.casefold())
    ):
        topic = None  # not in the message: invented
    return BallotRequest(ref=ref, topic=topic, choice=_str(data.get("vote_choice"), 60))


def booking_request_from_model(data: dict[str, Any], about_someone_else: bool) -> BookingRequest:
    ref = data.get("booking_ref")
    if isinstance(ref, bool) or not isinstance(ref, int):
        ref = None
    return BookingRequest(
        BOOKING_INTENTS[data["intent"]],
        thing=_str(data.get("bookable"), 60),
        note=_str(data.get("note"), 140),
        destination=_str(data.get("destination"), 100),
        booking_ref=ref,
        about_someone_else=about_someone_else,
        from_model=True,
    )


def question_from_model(data: dict[str, Any]) -> Question:
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        count = chat_levels.HISTORY_DEFAULT
    kind = "history" if data.get("intent") == "show_history" else "levels"
    return Question(kind, _str(data.get("resource"), 60), max(1, min(count, 20)))


class SlidingWindowLimit:
    """At most `limit` events per key within `window` seconds."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._hits.clear()
            hits = self._hits[key] = deque()
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


class Assistant:
    """One per app process (the app runs with one worker).

    Conversation memory is per room, in RAM only, and bounded.
    """

    def __init__(
        self,
        settings: Settings,
        llm: JSONCompleter,
        names: DisplayNames | None,
        weather: Weather | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        signer: decision_log.Signer | None = None,
    ) -> None:
        self._s = settings
        self._signer = signer
        self._llm = llm
        self._names = names
        self._weather = weather
        self._limit = SlidingWindowLimit(settings.assistant_rate_per_minute, clock=clock)
        tz = ZoneInfo(settings.timezone)
        self._now = now or (lambda: datetime.now(tz))
        self._locks: dict[str, asyncio.Lock] = {}
        self._clock = clock
        # An offer ("Shall I …? (yes/no)") waiting for this member's answer, per room.
        self._pending: OrderedDict[tuple[str, str], tuple[Pending, float]] = OrderedDict()
        # A ballot in the member's own words, waiting for their "yes": (proposal id, choice).
        self._ballots: OrderedDict[tuple[str, str], tuple[int, Any, float]] = OrderedDict()

    # --- conversation memory -------------------------------------------------

    def _prompt(
        self,
        hist: list[dict[str, str]],
        text: str,
        resources: str = "",
        things: str = "",
        guide: str = "",
    ) -> list[dict[str, str]]:
        today = self._now().strftime("%A, %d %B %Y")
        content = en.ASSISTANT_SYSTEM_PROMPT.format(
            today=today,
            resources=resources,
            bookables=things or "- (none configured yet)",
            guide=guide or "- (nothing written down yet)",
        )
        system = {"role": "system", "content": content}
        turns = [*hist[-HISTORY_MESSAGES:], {"role": "user", "content": text}]
        while len(turns) > 1 and sum(len(t["content"]) for t in turns) > HISTORY_CHARS:
            turns.pop(0)
        return [system, *turns]

    # --- entry point ---------------------------------------------------------

    async def handle(
        self,
        db: Session,
        sender: str,
        room_id: str,
        text: str,
        history: list[dict[str, str]] | None = None,
        *,
        direct: bool = False,
        reply_to: str | None = None,
    ) -> str:
        """Answer one message from `sender` (a Matrix user id). Always returns a reply.

        `history` is the chat before this message as the bot read it from the
        Matrix room, oldest first, reminders included: {"role": "user" or
        "assistant", "content": text}. `direct`: a 1:1 chat with the bot, the only
        place a ballot is accepted.
        """
        # Every account on this homeserver is a member; new accounts join here.
        member = members.ensure_member(db, sender, self._s.matrix_server_name)
        if member is None or not member.is_active:
            return en.ASSISTANT_NOT_A_MEMBER
        db.commit()  # keep a membership that was just created, whatever happens next

        text = text.strip()
        # Ballots and vote messages never reach the model.
        hist = vote_chat.without_vote_turns(list(history or []))[-HISTORY_MESSAGES:]
        if text.lower() in _HELP:
            return en.ASSISTANT_HELP
        if len(text) > self._s.assistant_max_chars:
            return en.ASSISTANT_TOO_LONG.format(limit=self._s.assistant_max_chars)
        if not self._limit.allow(sender):
            return en.ASSISTANT_RATE_LIMITED
        if community_room.asks_to_set_room(text) and self._signer is not None:
            # Configuration: rules and an admin check, never the model.
            return community_room.set_room(db, self._signer, member, room_id, channel="matrix")

        lock = self._locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            key = (room_id, sender)
            reply = self._answer_ballot(db, member, key, text)
            if reply is not None:
                return reply
            reply = self._answer_pending(db, member, key, text)
            if reply is not None:
                return reply
            if direct and reply_to and self._signer is not None:
                ballot = vote_chat.answer(
                    db,
                    member,
                    reply_to,
                    text,
                    room_id=room_id,
                    signer=self._signer,
                    now=utcnow(),
                    tz=ZoneInfo(self._s.timezone),
                )
                if ballot is not None:
                    return ballot
            vote = vote_rule(text)
            if vote is not None:
                return await self._act(db, member, vote, direct)
            # The model decides, with the chat so far. The rules only answer
            # when the model server is down.
            prompt = self._prompt(
                hist,
                text,
                chat_ledger.resources_for_prompt(db),
                chat_booking.bookables_for_prompt(db),
                community.guide_for_prompt(db),
            )
            try:
                data = await self._llm.complete_json(prompt, INTENT_SCHEMA)
            except LLMError:
                intent = self._rules(db, text)
                if intent is None:
                    return en.ASSISTANT_LLM_UNAVAILABLE
            else:
                intent = intent_from_model(data, text)
            if intent is not None and intent.booking is not None:
                earlier = [t["content"] for t in reversed(hist) if t.get("role") == "user"]
                if intent.booking.from_model and hist and hist[-1].get("role") == "assistant":
                    last = hist[-1]["content"]
                    intent = replace(intent, booking=replace(intent.booking, last_reply=last))
                return self._booking(db, member, key, intent.booking, text, earlier)
            if intent is not None and intent.name == "vote" and intent.ballot is not None:
                return self._propose_ballot(db, member, key, intent, direct)
            return await self._act(db, member, intent, direct)

    # --- ballots in the member's own words ---------------------------------------

    def _propose_ballot(
        self, db: Session, member: Member, key: tuple[str, str], intent: Intent, direct: bool
    ) -> str:
        """Ask before recording: the model may have misread the message."""
        self._ballots.pop(key, None)
        if not direct:
            return en.VOTE_CHAT_DIRECT_ONLY
        if intent.about_someone_else:
            return en.VOTE_NL_ONLY_YOURS
        reply, held = vote_chat.understand(
            db, intent.ballot, now=utcnow(), tz=ZoneInfo(self._s.timezone)
        )
        if held is not None:
            self._ballots[key] = (*held, self._clock() + PENDING_SECONDS)
            while len(self._ballots) > MAX_ROOMS:
                self._ballots.popitem(last=False)
        return reply

    def _answer_ballot(
        self, db: Session, member: Member, key: tuple[str, str], text: str
    ) -> str | None:
        """ "yes" / "no" to "Did you mean ...?"; None if there is nothing to answer."""
        held = self._ballots.pop(key, None)
        if held is None:
            return None
        proposal_id, choice, expires = held
        if self._clock() > expires:
            return None
        if _NO.match(text):
            return en.VOTE_NL_DECLINED
        if not _YES.match(text) or self._signer is None:
            return None  # they moved on; the question lapses
        return vote_chat.confirm_ballot(
            db,
            member,
            proposal_id,
            choice,
            signer=self._signer,
            now=utcnow(),
            tz=ZoneInfo(self._s.timezone),
        )

    def _rules(self, db: Session, text: str) -> Intent | None:
        """Fixed phrasings, for when the model server is down."""
        intent = parse_rules(text)
        if intent is None:
            fix = chat_fix.parse_fix_rule(text)
            if fix is not None and chat_fix.rule_applies(db, fix):
                intent = Intent("correct_entry", fix=fix)
        if intent is None:
            req = chat_ledger.parse_log_rule(text)
            if req is not None and chat_ledger.rule_applies(db, req):
                intent = Intent("log_entry", log=req)
        if intent is None:
            breq = chat_booking.parse_rule(db, text)
            if breq is not None:
                intent = Intent("booking", booking=breq)
        if intent is None:
            q = chat_levels.parse_question_rule(text)
            if q is not None and (q.resource is None or chat_ledger.resolve(db, q.resource)):
                intent = Intent(
                    "show_history" if q.kind == "history" else "show_levels", question=q
                )
        return intent

    # --- bookings ------------------------------------------------------------

    def _utcnow(self) -> datetime:
        return self._now().astimezone(UTC)

    def _booking(
        self,
        db: Session,
        member: Member,
        key: tuple[str, str],
        req: BookingRequest,
        text: str,
        earlier: list[str],
    ) -> str:
        outcome = chat_booking.handle(db, member, req, text, self._utcnow(), earlier)
        self._remember(key, outcome)
        return outcome.reply

    def _remember(self, key: tuple[str, str], outcome: chat_booking.Outcome) -> None:
        self._pending.pop(key, None)
        if outcome.pending is not None:
            self._pending[key] = (outcome.pending, self._clock() + PENDING_SECONDS)
            while len(self._pending) > MAX_ROOMS:
                self._pending.popitem(last=False)

    def _answer_pending(
        self, db: Session, member: Member, key: tuple[str, str], text: str
    ) -> str | None:
        """ "yes" / "no" to an offer made in this room; None if there is nothing to answer."""
        held = self._pending.get(key)
        if held is None:
            return None
        pending, expires = held
        if self._clock() > expires:
            self._pending.pop(key, None)
            return None
        if _NO.match(text):
            self._pending.pop(key, None)
            return en.CHAT_BOOK_DECLINED
        if not _YES.match(text):
            self._pending.pop(key, None)  # they moved on; the offer lapses
            return None
        outcome = chat_booking.confirm(db, member, pending, self._utcnow())
        self._remember(key, outcome)
        return outcome.reply

    # --- intents: code decides ----------------------------------------------

    async def _act(
        self, db: Session, member: Member, intent: Intent | None, direct: bool = False
    ) -> str:
        if intent is None:
            return en.ASSISTANT_NOT_UNDERSTOOD
        if intent.name == "weather":
            return self._weather_reply()
        if intent.name == "help":
            return en.ASSISTANT_HELP
        tz = ZoneInfo(self._s.timezone)
        if intent.name == "show_votes":
            return vote_chat.listing(db, member, direct=direct, now=utcnow(), tz=tz)
        if intent.name == "cancel_vote" and intent.cancel is not None:
            if self._signer is None:
                return en.VOTE_CHAT_UNAVAILABLE
            return vote_chat.cancel(db, member, intent.cancel, signer=self._signer, now=utcnow())
        if intent.name == "cast_vote" and intent.vote is not None:
            if self._signer is None:
                return en.VOTE_CHAT_UNAVAILABLE
            return vote_chat.cast(
                db, member, intent.vote, direct=direct, signer=self._signer, now=utcnow(), tz=tz
            )
        if intent.reply and self._not_a_resource(db, intent):
            # "when is dinner?" proposed as a level of "dinner", with an answer:
            # a question about the place, not about a tracked resource. Answer it
            # like chat. Without an answer, code asks which resource is meant.
            return self._chat_reply(intent)
        if intent.name == "log_entry" and intent.log is not None:
            if intent.log.is_question:
                # "how much water do we have?" that the model took for a log entry.
                return chat_levels.answer(db, Question("levels", None), utcnow(), tz)
            return chat_ledger.log(db, member, intent.log, now=utcnow(), tz=tz)
        if intent.name == "correct_entry" and intent.fix is not None:
            return chat_fix.fix(db, member, intent.fix, now=utcnow(), tz=tz)
        if intent.name in ("show_levels", "show_history") and intent.question is not None:
            return chat_levels.answer(db, intent.question, utcnow(), tz)
        if intent.name in ("show_profile", "set_display_name") and intent.about_someone_else:
            return en.ASSISTANT_ONLY_YOURS
        if intent.name == "show_profile":
            return await self._show_profile(db, member)
        if intent.name == "my_contributions":
            if intent.about_someone_else:
                return en.ASSISTANT_ONLY_YOURS
            return self._my_contributions(db, member)
        if intent.name == "set_display_name":
            return await self._set_display_name(db, member, intent.display_name or "")
        # chat: the only case where the model's own words are sent back.
        return self._chat_reply(intent)

    @staticmethod
    def _chat_reply(intent: Intent) -> str:
        reply = intent.reply or ""
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS].rstrip() + " …"
        return reply

    @staticmethod
    def _not_a_resource(db: Session, intent: Intent) -> bool:
        """A question naming something that isn't a tracked resource ("dinner")."""
        if intent.question is not None:
            words = intent.question.resource
        elif intent.log is not None and intent.log.is_question:
            words = intent.log.resource
        else:
            return False
        return bool(words) and chat_ledger.resolve(db, words) is None

    def _weather_reply(self) -> str:
        """Tool: the forecast for the community's location. Code writes every number."""
        if self._weather is None or not self._weather.enabled:
            return en.WEATHER_NOT_CONFIGURED
        return describe(
            self._weather.forecast,
            self._s.weather_place,
            datetime.now(UTC),
            ZoneInfo(self._s.timezone),
        )

    def _my_contributions(self, db: Session, member: Member) -> str:
        """Tool: the sender's own contributions and badges (never anyone else's)."""
        data = impact.summary(db, member)
        db.commit()
        if data.entries:
            parts = ", ".join(f"{sh.mine} {sh.category.name.lower()}" for sh in data.shares)
            entries = en.CHAT_ME_ENTRIES.format(count=data.entries, parts=parts)
        else:
            entries = en.CHAT_ME_ENTRIES_NONE
        if data.bookings:
            bookings = en.CHAT_ME_BOOKINGS.format(kept=data.bookings_honoured, count=data.bookings)
        else:
            bookings = en.CHAT_ME_BOOKINGS_NONE
        badges = []
        for b in data.badges:
            if b.earned:
                badges.append(en.CHAT_ME_BADGE_EARNED.format(name=b.rule.name))
            elif not b.counted:
                badges.append(en.CHAT_ME_BADGE_NOT_COUNTED.format(name=b.rule.name))
            else:
                line = en.CHAT_ME_BADGE_PROGRESS
                badges.append(line.format(name=b.rule.name, value=b.value, target=b.target))
        return en.CHAT_ME.format(
            entries=entries,
            bookings=bookings,
            badges=", ".join(badges) or en.CHAT_ME_NO_BADGES,
            url=f"{self._s.public_url}/me",
        )

    async def _show_profile(self, db: Session, member: Member) -> str:
        # Matrix is the source of truth for the name; refresh COMMONS' copy.
        if self._names is not None:
            try:
                name = await self._names.get_display_name(member.matrix_user_id)
            except MatrixAdminError:
                pass
            else:
                if members.store_display_name(db, member, name):
                    db.commit()
        return en.ASSISTANT_PROFILE.format(
            name=member.display_name or en.ASSISTANT_NO_NAME,
            # The localpart, not the full id: Element turns "@name:server" into a mention.
            account=member.matrix_user_id.removeprefix("@").split(":", 1)[0],
            roles=", ".join(member.role_names),
        )

    async def _set_display_name(self, db: Session, member: Member, raw: str) -> str:
        """Tool: change the sender's own display name (and only theirs)."""
        try:
            name = members.clean_display_name(raw)
        except members.MemberError:
            return en.ASSISTANT_NAME_INVALID.format(limit=members.DISPLAY_NAME_MAX)
        if name == member.display_name:
            return en.ASSISTANT_NAME_UNCHANGED.format(name=name)
        if self._names is None:
            return en.ASSISTANT_NAME_UNAVAILABLE
        # Matrix first: if that fails, nothing changes anywhere.
        try:
            await self._names.set_display_name(member.matrix_user_id, name)
        except MatrixAdminError:
            return en.ASSISTANT_NAME_UNAVAILABLE
        members.store_display_name(db, member, name)
        db.add(
            AuthEvent(
                kind="display_name_changed", member_id=member.id, channel="matrix", ts=utcnow()
            )
        )
        db.commit()
        return en.ASSISTANT_NAME_CHANGED.format(name=name)
