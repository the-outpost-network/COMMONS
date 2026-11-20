"""Votes in chat: list them, vote in a direct chat, and the bot's vote messages.

Only strict phrasings cast a vote ("vote 12 1", "vote yes on 12"); the model can
at most show the list. A ballot is accepted only in a direct chat with the bot,
because in a group room the message itself would reveal the choice. Voting goes
through `governance.cast_vote`, exactly like the web, with channel `matrix`.

Vote messages come from the `chat_notification` outbox, written by governance in
the same transaction as the change. The bot fetches them over the internal API
and confirms delivery, so a restart of the bot loses none. Code writes every fact
in a message. The local model only adds one checked, neutral opening sentence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import decision_log, governance
from app.i18n import en
from app.models import ChatNotification, DecisionLog, Member, Proposal, ProposalMessage, Vote
from app.proposals import amount_text

log = logging.getLogger("commons.vote_chat")

MAX_LISTED = 10
KEYCAPS = tuple(f"{n}\ufe0f\u20e3" for n in range(1, 7))  # 1️⃣ .. 6️⃣
ABSTAIN_KEY = "\U0001f937"  # 🤷
MAX_PENDING = 20
MAX_AGE = timedelta(days=3)  # older undelivered messages are no longer news

MAX_INTROS = 3  # per request from the bot; CPU inference is slow
INTRO_TIMEOUT = 75.0  # the first answer after a pause took about a minute on the box
INTRO_BUDGET = 45.0  # no further intro is started after this long
INTRO_MIN = 12
INTRO_MAX = 120
INTRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"intro": {"type": "string", "maxLength": INTRO_MAX}},
    "required": ["intro"],
    "additionalProperties": False,
}
REASON_MIN = 6
REASON_MAX = 200
REASON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reason": {"type": "string", "maxLength": REASON_MAX}},
    "required": ["reason"],
    "additionalProperties": False,
}
_REASON_BANNED = re.compile(r"[@#<>\[\]`*_|{}\\]|https?|www\.|://", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:not|no|never|none|cannot)\b|n['\u2019]t\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?]")
_VOTE_MESSAGE_STARTS = tuple(
    head.split("{", 1)[0]
    for head in (
        en.VOTE_MSG_OPENED,
        en.VOTE_MSG_REMINDER,
        en.VOTE_MSG_PASSED,
        en.VOTE_MSG_REJECTED,
        en.VOTE_MSG_CANCELLED,
    )
)
_INTRO_BANNED = re.compile(
    r"[\d@#<>\[\]`*_|{}\\/:]|https?|www\."
    r"|\b(?:passed|rejected|approved|denied|won|lost|tie|yes|no|abstain|should|must|recommend"
    r"|best|better|worse|excit\w*|thrill\w*|support|oppose|against|favou?r|ignore|instructions?)\b",
    re.IGNORECASE,
)

_ADDRESS = re.compile(r"^@?commons(?::\S+)?[\s:,]+", re.IGNORECASE)
_LIST = re.compile(
    r"^(?:vote"
    r"|(?:open\s+|current\s+)?(?:votes|proposals)"
    r"|(?:please\s+)?(?:show|list|give)\s+(?:me\s+)?(?:the\s+|all\s+)?(?:open\s+|current\s+)?"
    r"(?:votes|proposals)"
    r"|(?:what|which)\s+(?:votes|proposals)"
    r"(?:\s+(?:are|do\s+we\s+have))?(?:\s+(?:there|open|running|now))*"
    r"|what\s+can\s+i\s+vote\s+on"
    r"|are\s+there\s+(?:any\s+)?(?:open\s+)?votes)$",
    re.IGNORECASE,
)
_VOTE = re.compile(r"^(?:i\s+)?vote\s+(?:for\s+)?(?P<rest>\S.*)$", re.IGNORECASE | re.DOTALL)
_ASK = re.compile(r"^on\s+(?P<ref>\S.*)$", re.IGNORECASE)
_ID_FIRST = re.compile(r"^#?(?P<id>\d+)\s+(?P<choice>\S.*)$")
_CHOICE_ON = re.compile(r"^(?P<choice>\S.*?)\s+(?:on|in|for)\s+(?P<ref>\S.*)$", re.IGNORECASE)
_CANCEL = re.compile(
    r"^(?:please\s+)?(?:cancel|call\s+off)\s+(?:the\s+)?(?:vote|proposal)\b\s*"
    r"(?:#?(?P<id>\d+)\b)?(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL_REASON = re.compile(
    r"^\s*(?:[:,;\-\u2013\u2014]|because\b|since\b|reason\b\s*:?)\s*(?:because\s+)?(?P<reason>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_REF_WORDS = re.compile(r"^(?:the\s+)?(?:(?:vote|proposal)\s+)?(.*?)(?:\s+(?:vote|proposal))?$")
_OPTION_WORD = re.compile(r"^(?:option\s+|number\s+|#)", re.IGNORECASE)


@dataclass(frozen=True)
class VoteRequest:
    """What the member wrote after "vote"; empty for "which votes are open?"."""

    rest: str = ""


@dataclass(frozen=True)
class BallotRequest:
    """A ballot in the member's own words, as the model read it. Code resolves it."""

    ref: int | None = None
    topic: str | None = None
    choice: str | None = None


@dataclass(frozen=True)
class CancelRequest:
    """ "cancel vote 12 because ...": both parts are needed before anything happens."""

    proposal_id: int | None = None
    reason: str = ""


def parse_cancel(text: str) -> CancelRequest | None:
    m = _CANCEL.match(_ADDRESS.sub("", text.strip(), count=1))
    if m is None:
        return None
    reason = _CANCEL_REASON.match(m.group("rest"))
    return CancelRequest(
        int(m.group("id")) if m.group("id") else None,
        reason.group("reason").strip() if reason else "",
    )


def parse(text: str) -> VoteRequest | None:
    """The strict rules; None means the message isn't about votes."""
    text = _ADDRESS.sub("", text.strip(), count=1).rstrip(" .!?").removeprefix("/")
    if _LIST.match(text):
        return VoteRequest()
    if m := _VOTE.match(text):
        return VoteRequest(m.group("rest").strip())
    return None


def without_vote_turns(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """The chat for the model, minus vote commands, ballots, their answers and vote messages."""
    starts = (en.VOTE_CHAT_RECORDED.split("{", 1)[0], en.VOTE_NL_CONFIRM.split("{", 1)[0])
    hidden = set()
    for i, turn in enumerate(history):
        if turn.get("role") == "assistant" and turn.get("content", "").startswith(starts):
            hidden.add(i)  # a confirmed ballot, and the message that cast it
            if i and history[i - 1].get("role") == "user":
                hidden.add(i - 1)
    history = [t for i, t in enumerate(history) if i not in hidden]
    out: list[dict[str, str]] = []
    skip_answer = False
    for turn in history:
        content = turn.get("content", "")
        if turn.get("role") == "user":
            skip_answer = parse(content) is not None or parse_cancel(content) is not None
            if not skip_answer:
                out.append(turn)
            continue
        if not skip_answer and not content.startswith(_VOTE_MESSAGE_STARTS):
            out.append(turn)
        skip_answer = False
    return out


def open_votes(db: Session, now: datetime) -> list[Proposal]:
    return list(
        db.scalars(
            select(Proposal)
            .where(Proposal.status == "open", Proposal.closes_at > now)
            .order_by(Proposal.closes_at, Proposal.id)
        )
    )


def _when(ts: datetime, tz: ZoneInfo) -> str:
    local = ts.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b} {local:%H:%M}"


def _card(p: Proposal, tz: ZoneInfo) -> list[str]:
    """Question, context, numbered options and closing time. Member text is plain text."""
    lines = [en.VOTE_CHAT_TITLE.format(id=p.id, question=p.question)]
    if p.why:
        lines.append(p.why)
    if (amount := amount_text(p)) is not None:
        lines.append(en.VOTE_CHAT_AMOUNT.format(amount=amount))
    lines += [f"{KEYCAPS[i]} {o.label}" for i, o in enumerate(p.options)]
    lines.append(en.VOTE_CHAT_CLOSES.format(when=_when(p.closes_at, tz)))
    return lines


def _how(p: Proposal) -> str:
    return en.VOTE_CHAT_HOW.format(id=p.id)


def _label(p: Proposal, option_id: int | None) -> str:
    return governance.label_of(p, option_id) or en.VOTES_ABSTAIN


def listing(db: Session, member: Member, *, direct: bool, now: datetime, tz: ZoneInfo) -> str:
    votes = open_votes(db, now)
    if not votes:
        return en.VOTE_CHAT_NONE
    blocks = []
    for p in votes[:MAX_LISTED]:
        lines = _card(p, tz)
        if direct:  # your own choice is shown to you, never in a shared room
            mine = governance.current_vote(db, p, member)
            lines.append(
                en.VOTE_CHAT_NOT_VOTED
                if mine is None
                else en.VOTE_CHAT_YOURS.format(choice=_label(p, mine.option_id))
            )
        blocks.append("\n".join(lines))
    footer = _how(votes[0]) if direct else en.VOTE_CHAT_DIRECT_ONLY
    return "\n\n".join([*blocks, footer])


def _find(votes: list[Proposal], ref: str) -> Proposal | None:
    ref = ref.strip().lstrip("#")
    if ref.isdecimal():
        return next((p for p in votes if p.id == int(ref)), None)
    m = _REF_WORDS.match(ref.casefold())
    words = m.group(1).strip() if m else ""
    found = [p for p in votes if len(words) >= 3 and words in p.question.casefold()]
    return found[0] if len(found) == 1 else None


def _choice(p: Proposal, raw: str) -> governance.Choice | None:
    raw = _OPTION_WORD.sub("", raw.strip().strip("\"'“”")).strip().casefold()
    if raw == governance.ABSTAIN:
        return governance.ABSTAIN
    if raw.isdecimal():
        position = int(raw)
        return p.options[position - 1].id if 1 <= position <= len(p.options) else None
    return next((o.id for o in p.options if o.label.casefold() == raw), None)


def _record(
    db: Session,
    member: Member,
    p: Proposal,
    choice: governance.Choice,
    signer: decision_log.Signer,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    question = p.question
    label = _label(p, None if choice == governance.ABSTAIN else choice)
    closes = _when(p.closes_at, tz)
    try:
        governance.cast_vote(db, member, p.id, choice, signer=signer, channel="matrix", now=now)
        db.commit()
    except governance.GovernanceError as e:
        db.rollback()
        return en.VOTE_CHAT_REFUSED.format(reason=e)
    db.refresh(p)
    if p.status != "open":
        return en.VOTE_CHAT_RECORDED_LAST.format(choice=label, question=question)
    return en.VOTE_CHAT_RECORDED.format(choice=label, question=question, when=closes)


def cast(
    db: Session,
    member: Member,
    request: VoteRequest,
    *,
    direct: bool,
    signer: decision_log.Signer,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """Act on "vote ...", as `member`. Nothing is written unless it is unambiguous."""
    if not direct:
        return en.VOTE_CHAT_DIRECT_ONLY
    votes = open_votes(db, now)
    if not votes:
        return en.VOTE_CHAT_NONE
    rest = request.rest
    if m := _ASK.match(rest):
        p = _find(votes, m.group("ref"))
        if p is None:
            return en.VOTE_CHAT_WHICH
        return "\n\n".join(["\n".join(_card(p, tz)), _how(p)])

    attempts: list[tuple[str, str | None]]
    if m := _ID_FIRST.match(rest):
        attempts = [(m.group("choice"), m.group("id"))]
    else:
        attempts = [(rest, None)]
        if m := _CHOICE_ON.match(rest):
            attempts.append((m.group("choice"), m.group("ref")))

    wrong_option: Proposal | None = None
    for raw, ref in attempts:
        only = votes[0] if len(votes) == 1 else None
        p = only if ref is None else _find(votes, ref)
        if p is None:
            continue
        choice = _choice(p, raw)
        if choice is not None:
            return _record(db, member, p, choice, signer, now, tz)
        wrong_option = p
    if wrong_option is not None:
        card = "\n".join(_card(wrong_option, tz))
        return "\n\n".join([en.VOTE_CHAT_NO_OPTION, card, _how(wrong_option)])
    return en.VOTE_CHAT_WHICH


def cancel(
    db: Session,
    member: Member,
    request: CancelRequest,
    *,
    signer: decision_log.Signer,
    now: datetime,
) -> str:
    """Cancel a vote as `member` (board or admin), with the reason they gave."""
    if not member.role_names & governance.GOVERNING_ROLES:
        return en.VOTE_CHAT_CANCEL_NOT_ALLOWED
    if request.proposal_id is None or not request.reason:
        return en.VOTE_CHAT_CANCEL_HOW
    p = db.get(Proposal, request.proposal_id)
    if p is None:
        return en.VOTE_CHAT_CANCEL_UNKNOWN.format(id=request.proposal_id)
    question = p.question
    try:
        governance.cancel_proposal(
            db, member, p.id, request.reason, signer=signer, channel="matrix", now=now
        )
        db.commit()
    except governance.NotAllowed:
        db.rollback()
        return en.VOTE_CHAT_CANCEL_NOT_ALLOWED
    except governance.GovernanceError as e:
        db.rollback()
        return en.VOTE_CHAT_REFUSED.format(reason=e)
    return en.VOTE_CHAT_CANCEL_DONE.format(
        id=request.proposal_id, question=question, reason=request.reason
    )


# --- vote messages from the bot ------------------------------------------------


class _Bold(str):
    """A line shown in bold where the chat app supports it."""


Block = list[str]


def _render(blocks: list[Block]) -> tuple[str, str]:
    """Plain text and escaped HTML of the same message; all values are escaped here."""
    used = [[line for line in block if line] for block in blocks]
    used = [block for block in used if block]
    text = "\n\n".join("\n".join(block) for block in used)
    parts = []
    for block in used:
        lines = [
            f"<strong>{escape(line)}</strong>" if isinstance(line, _Bold) else escape(line)
            for line in block
        ]
        parts.append("<p>" + "<br>".join(lines) + "</p>")
    return text, "".join(parts)


def _proposer(p: Proposal) -> str:
    if p.anonymous:
        return en.VOTES_ANONYMOUS
    return p.creator.display_name or p.creator.matrix_user_id.removeprefix("@").split(":", 1)[0]


def _intro(n: ChatNotification) -> str:
    return n.intro or en.VOTE_MSG_INTRO[n.kind]


def _invitation(db: Session, n: ChatNotification, p: Proposal, tz: ZoneInfo) -> list[Block]:
    head = (
        en.VOTE_MSG_OPENED.format(id=p.id)
        if n.kind == "opened"
        else en.VOTE_MSG_REMINDER.format(id=p.id, time=f"{p.closes_at.astimezone(tz):%H:%M}")
    )
    top: Block = [head, _Bold(p.question), _intro(n)]
    if n.kind == "reminder":
        top.append(en.VOTE_CHAT_NOT_VOTED)
    amount = amount_text(p)
    type_name = governance.type_names(db).get(p.vote_type, p.vote_type)
    details: Block = [
        en.VOTE_MSG_WHY.format(why=p.why) if p.why else "",
        en.VOTE_CHAT_AMOUNT.format(amount=amount) if amount is not None else "",
        en.VOTE_MSG_BY.format(name=_proposer(p), type=type_name),
    ]
    options: Block = [
        _Bold(en.VOTE_MSG_OPTIONS),
        *(f"{KEYCAPS[i]} {o.label}" for i, o in enumerate(p.options)),
    ]
    closes: Block = [en.VOTE_CHAT_CLOSES.format(when=_when(p.closes_at, tz))]
    how = en.VOTE_CHAT_TAP.format(last=KEYCAPS[len(p.options) - 1], abstain=ABSTAIN_KEY)
    return [top, details, options, closes, [how]]


def _result(db: Session, n: ChatNotification, p: Proposal) -> list[Block]:
    """The result as signed in the decision log."""
    entry = db.scalar(
        select(DecisionLog).where(
            DecisionLog.kind == "proposal.closed",
            DecisionLog.payload_json["proposal_id"].as_integer() == p.id,
        )
    )
    data: dict[str, Any] = entry.payload_json if entry is not None else {}
    passed = p.outcome == "decided"
    outcome = (
        en.VOTE_CHAT_PASSED.format(option=data.get("outcome_label"))
        if passed
        else en.VOTE_CHAT_REJECTED.get(p.outcome or "", en.VOTE_CHAT_REJECTED["no_votes"])
    )
    head = en.VOTE_MSG_PASSED if passed else en.VOTE_MSG_REJECTED
    counts: Block = [f"{row['label']}: {row['votes']}" for row in data.get("tally", [])]
    if entry is not None:
        counts.append(
            en.VOTE_CHAT_COUNTS.format(
                abstained=data["abstained"],
                turnout=data["turnout"],
                eligible=data["eligible"],
                seq=entry.seq,
            )
        )
    return [[head.format(id=p.id), _Bold(p.question), _intro(n)], [_Bold(outcome)], counts]


def _message(db: Session, n: ChatNotification, p: Proposal, tz: ZoneInfo) -> tuple[str, str]:
    if n.kind == "closed":
        return _render(_result(db, n, p))
    if n.kind == "cancelled":
        return _render(
            [
                [en.VOTE_MSG_CANCELLED.format(id=p.id), _Bold(p.question), _intro(n)],
                [
                    en.VOTE_MSG_REASON.format(reason=n.reworded_reason or p.cancel_reason),
                    en.VOTE_MSG_REWORDED if n.reworded_reason else "",
                ],
            ]
        )
    return _render(_invitation(db, n, p, tz))


def _recipients(db: Session, n: ChatNotification, p: Proposal) -> list[str]:
    """Everyone active, the proposer included; reminders only go to those yet to vote."""
    query = select(Member.matrix_user_id).where(Member.deactivated_at.is_(None))
    if n.kind == "reminder":
        voted = select(Vote.member_id).where(Vote.proposal_id == p.id, Vote.superseded_at.is_(None))
        query = query.where(Member.id.not_in(voted))
    return list(db.scalars(query.order_by(Member.id)))


def _reminders_allowed(db: Session, now: datetime, tz: ZoneInfo) -> bool:
    """Reminders wait outside the configured hours, however late they were queued."""
    try:
        rules = governance.voting_rules(db)
    except governance.GovernanceError:
        return False
    return governance.in_reminder_window(rules, now, tz)


def _sendable(db: Session, now: datetime, tz: ZoneInfo) -> list[tuple[ChatNotification, Proposal]]:
    """Undelivered messages that may go out now, oldest first."""
    rows = db.execute(
        select(ChatNotification, Proposal)
        .outerjoin(Proposal, Proposal.id == ChatNotification.proposal_id)
        .where(
            ChatNotification.delivered_at.is_(None), ChatNotification.created_at >= now - MAX_AGE
        )
        .order_by(ChatNotification.id)
    ).all()
    # If the bot was away, only the latest reminder of a vote is still worth sending.
    latest = {n.proposal_id: n.id for n, _ in rows if n.kind == "reminder"}
    reminders_ok = bool(latest) and _reminders_allowed(db, now, tz)
    out = []
    for n, p in rows:
        if n.kind == "welcome":
            out.append((n, p))
            continue
        if n.kind in ("opened", "reminder") and (p.status != "open" or p.closes_at <= now):
            continue
        if n.kind == "reminder" and (not reminders_ok or latest[n.proposal_id] != n.id):
            continue
        out.append((n, p))
    return out


def clean_intro(raw: Any, labels: Iterable[str] = ()) -> str:
    """The model's line if it is one short neutral sentence, otherwise ''.

    It goes to every member, and the question it is based on is member text, so
    anything that could carry a link, a mention, a fact or a recommendation is refused.
    """
    if not isinstance(raw, str):
        return ""
    line = raw.strip()
    if not INTRO_MIN <= len(line) <= INTRO_MAX or "\n" in line or _INTRO_BANNED.search(line):
        return ""
    if len(_SENTENCE_END.findall(line)) != 1 or line[-1] not in ".!":
        return ""
    folded = line.casefold()
    if any(len(label) >= 3 and label.casefold() in folded for label in labels):
        return ""
    return line


def _word_starts(text: str) -> set[str]:
    return {w[:4].casefold() for w in re.findall(r"[^\W\d_]{3,}", text)}


def clean_reason(raw: Any, original: str) -> str:
    """The model's wording if it can't have changed what the member said, otherwise ''."""
    if not isinstance(raw, str):
        return ""
    line = raw.strip()
    if not REASON_MIN <= len(line) <= REASON_MAX or "\n" in line or _REASON_BANNED.search(line):
        return ""
    if len(_SENTENCE_END.findall(line)) > 2:
        return ""
    if not set(re.findall(r"\d+", line)) <= set(re.findall(r"\d+", original)):
        return ""  # a number the member didn't give
    if bool(_NEGATION.search(line)) != bool(_NEGATION.search(original)):
        return ""  # the meaning may have flipped
    words = _word_starts(original)
    return line if not words or words & _word_starts(line) else ""


async def _ask_reason(llm: Any, reason: str) -> str:
    messages = [
        {"role": "system", "content": en.VOTE_REASON_SYSTEM},
        {"role": "user", "content": en.VOTE_REASON_USER.format(reason=reason)},
    ]
    try:
        data = await asyncio.wait_for(llm.complete_json(messages, REASON_SCHEMA), INTRO_TIMEOUT)
    except Exception as e:  # the message then shows the member's own words
        log.info("no reworded reason from the model: %s", type(e).__name__)
        return ""
    return clean_reason(data.get("reason"), reason)


async def _ask_intro(llm: Any, kind: str, question: str, labels: list[str]) -> str:
    messages = [
        {"role": "system", "content": en.VOTE_INTRO_SYSTEM},
        {
            "role": "user",
            "content": en.VOTE_INTRO_USER.format(
                event=en.VOTE_INTRO_EVENT[kind], question=question
            ),
        },
    ]
    try:
        data = await asyncio.wait_for(llm.complete_json(messages, INTRO_SCHEMA), INTRO_TIMEOUT)
    except Exception as e:  # the message goes out with the fixed line instead
        log.info("no vote message intro from the model: %s", type(e).__name__)
        return ""
    return clean_intro(data.get("intro"), labels)


async def write_intros(db: Session, llm: Any, now: datetime, tz: ZoneInfo) -> None:
    """Let the model write the opening line of waiting messages, a few per call."""
    waiting = [
        (n.id, n.kind, p.question, [o.label for o in p.options], p.cancel_reason)
        for n, p in _sendable(db, now, tz)
        if n.intro is None and n.kind != "welcome"
    ][:MAX_INTROS]
    db.rollback()  # no open transaction while the model is thinking
    started = time.monotonic()
    for notification_id, kind, question, labels, reason in waiting:
        if time.monotonic() - started > INTRO_BUDGET:
            break  # the rest follow on the bot's next request
        intro = await _ask_intro(llm, kind, question, labels)
        reworded = await _ask_reason(llm, reason) if kind == "cancelled" and reason else ""
        n = db.get(ChatNotification, notification_id)
        n.intro = intro
        n.reworded_reason = reworded or None
        db.commit()


def pending(db: Session, *, now: datetime, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Messages ready for the bot. Invitations to a vote that has ended are not sent."""
    out: list[dict[str, Any]] = []
    for n, p in _sendable(db, now, tz):
        if n.kind == "welcome":
            item = _welcome_item(db, n)
            if item is not None:
                out.append(item)
            continue
        if n.intro is None:
            continue
        text, html = _message(db, n, p, tz)
        taps = n.kind in ("opened", "reminder")
        out.append(
            {
                "id": n.id,
                "text": text,
                "html": html,
                "recipients": _recipients(db, n, p),
                "proposal_id": p.id,
                # Pre-react with these, and report the message so taps can be mapped.
                "keys": tap_keys(p) if taps else [],
            }
        )
        if len(out) == MAX_PENDING:
            break
    return out


def mark_delivered(db: Session, notification_id: int, now: datetime) -> bool:
    n = db.get(ChatNotification, notification_id)
    if n is None:
        return False
    if n.delivered_at is None:
        n.delivered_at = now
    return True


# --- the COMMONS room and taps in direct chats -------------------------------------


def tap_keys(p: Proposal) -> list[str]:
    return [*KEYCAPS[: len(p.options)], ABSTAIN_KEY]


def announcement(db: Session, p: Proposal, tz: ZoneInfo) -> tuple[str, str]:
    """The room post for a new vote. It never takes ballots: those stay in direct chats."""
    amount = amount_text(p)
    type_name = governance.type_names(db).get(p.vote_type, p.vote_type)
    return _render(
        [
            [en.VOTE_ROOM_HEAD.format(id=p.id), _Bold(p.question)],
            [
                en.VOTE_MSG_WHY.format(why=p.why) if p.why else "",
                en.VOTE_CHAT_AMOUNT.format(amount=amount) if amount is not None else "",
                en.VOTE_MSG_BY.format(name=_proposer(p), type=type_name),
            ],
            [en.VOTE_ROOM_OPTIONS.format(options=" \u00b7 ".join(o.label for o in p.options))],
            [en.VOTE_CHAT_CLOSES.format(when=_when(p.closes_at, tz))],
            [en.VOTE_ROOM_HOW],
        ]
    )


def room_result(db: Session, p: Proposal) -> tuple[str, str]:
    if p.status == "cancelled":
        return _render(
            [
                [en.VOTE_MSG_CANCELLED.format(id=p.id), _Bold(p.question)],
                [en.VOTE_MSG_REASON.format(reason=p.cancel_reason)],
            ]
        )
    blocks = _result(db, ChatNotification(kind="closed", intro=""), p)
    blocks[0] = [b for b in blocks[0] if b]
    return _render(blocks)


def announced(db: Session, proposal_id: int) -> ProposalMessage | None:
    return db.scalar(
        select(ProposalMessage)
        .where(ProposalMessage.proposal_id == proposal_id, ProposalMessage.kind == "room")
        .order_by(ProposalMessage.id.desc())
        .limit(1)
    )


def remember_message(
    db: Session,
    proposal_id: int,
    kind: str,
    room_id: str,
    event_id: str,
    user_id: str | None,
    now: datetime,
) -> bool:
    """Store which Matrix message shows which vote (and, in a direct chat, to whom)."""
    if db.get(Proposal, proposal_id) is None:
        return False
    member_id = None
    if kind == "direct":
        member = db.scalar(select(Member).where(Member.matrix_user_id == user_id))
        if member is None:
            return False
        member_id = member.id
    exists = db.scalar(select(ProposalMessage.id).where(ProposalMessage.event_id == event_id))
    if exists is None:
        db.add(
            ProposalMessage(
                proposal_id=proposal_id,
                kind=kind,
                member_id=member_id,
                room_id=room_id,
                event_id=event_id,
                created_at=now,
            )
        )
    return True


def is_vote_message(db: Session, event_id: str) -> bool:
    """A direct vote message: the only messages whose taps can be ballots."""
    return (
        db.scalar(
            select(ProposalMessage.id).where(
                ProposalMessage.event_id == event_id, ProposalMessage.kind == "direct"
            )
        )
        is not None
    )


def _choice_for_key(p: Proposal, key: str) -> governance.Choice | None:
    key = key.replace("\ufe0f", "")
    if key == ABSTAIN_KEY:
        return governance.ABSTAIN
    caps = [k.replace("\ufe0f", "") for k in KEYCAPS]
    if key in caps and caps.index(key) < len(p.options):
        return p.options[caps.index(key)].id
    return None


def is_shared_room(db: Session, room_id: str) -> bool:
    """The COMMONS room is never a private chat, even with just one member in it."""
    from app import community_room

    return room_id == community_room.current(db)


def tap(
    db: Session,
    member: Member,
    event_id: str,
    key: str,
    *,
    room_id: str,
    signer: decision_log.Signer,
    now: datetime,
    tz: ZoneInfo,
) -> str | None:
    """A reaction on a vote message in `member`'s own direct chat; None = not a ballot."""
    if is_shared_room(db, room_id):
        return None
    message = db.scalar(
        select(ProposalMessage).where(
            ProposalMessage.event_id == event_id,
            ProposalMessage.kind == "direct",
            ProposalMessage.member_id == member.id,
            ProposalMessage.room_id == room_id,
        )
    )
    if message is None:
        return None
    p = db.get(Proposal, message.proposal_id)
    choice = _choice_for_key(p, key) if p is not None else None
    if p is None or choice is None:
        return None
    return _record(db, member, p, choice, signer, now, tz)


_QUOTE = re.compile(r"^>.*$\n?", re.MULTILINE)
_VOTE_WORD = re.compile(r"^(?:i\s+)?vote\s+(?:for\s+)?", re.IGNORECASE)


def answer(
    db: Session,
    member: Member,
    reply_to: str,
    text: str,
    *,
    room_id: str,
    signer: decision_log.Signer,
    now: datetime,
    tz: ZoneInfo,
) -> str | None:
    """A reply to the vote message in `member`'s own direct chat: their ballot.

    None if `reply_to` isn't such a message (then the reply is an ordinary message).
    """
    message = db.scalar(
        select(ProposalMessage).where(
            ProposalMessage.event_id == reply_to,
            ProposalMessage.kind == "direct",
            ProposalMessage.member_id == member.id,
            ProposalMessage.room_id == room_id,
        )
    )
    p = db.get(Proposal, message.proposal_id) if message is not None else None
    if p is None:
        return None
    # Some apps quote the message replied to ("> ..."): only the member's own words count.
    words = _VOTE_WORD.sub("", _QUOTE.sub("", text).strip()).rstrip(" .!")
    choice = _choice(p, words) if words else None
    if choice is None:
        return en.VOTE_REPLY_HOW
    return _record(db, member, p, choice, signer, now, tz)


# --- ballots in the member's own words -------------------------------------------

_YES_WORDS = re.compile(
    r"^(?:yes|yeah|yep|for|for it|in favou?r|support|agree|approve|accept|sure|ok|okay)$"
)
_NO_WORDS = re.compile(r"^(?:no|nope|against|oppose|disagree|reject|decline|not)$")
_ABSTAIN_WORDS = re.compile(
    r"^(?:abstain|neutral|pass|skip|no opinion|don'?t mind|either|undecided|count me out)$"
)


def _nl_choice(p: Proposal, raw: str) -> governance.Choice | None:
    """The option the member's words point to, or None when it isn't clear."""
    words = re.sub(r"[^\w' ]", " ", raw.casefold()).strip()
    words = " ".join(words.split())
    if not words:
        return None
    exact = _choice(p, words)
    if exact is not None:
        return exact
    if _ABSTAIN_WORDS.match(words):
        return governance.ABSTAIN
    for pattern, label in ((_YES_WORDS, "yes"), (_NO_WORDS, "no")):
        if pattern.match(words):
            found = [o.id for o in p.options if o.label.casefold() == label]
            return found[0] if found else None
    said = _word_starts(words)
    found = []
    for o in p.options:
        label = o.label.casefold()
        named = _word_starts(label)
        if (len(label) >= 3 and (label in words or words in label)) or (
            named and len(named & said) * 2 >= len(named) + 1
        ):
            found.append(o.id)
    return found[0] if len(found) == 1 else None


def understand(
    db: Session, req: BallotRequest, *, now: datetime, tz: ZoneInfo
) -> tuple[str, tuple[int, governance.Choice] | None]:
    """A reply for the member, and the ballot to record if they say yes."""
    votes = open_votes(db, now)
    if not votes:
        return en.VOTE_CHAT_NONE, None
    if req.ref is not None:
        p = next((v for v in votes if v.id == req.ref), None)
    elif req.topic:
        p = _find(votes, req.topic)
    else:
        p = votes[0] if len(votes) == 1 else None
    if p is None:
        titles = "\n".join(en.VOTE_CHAT_TITLE.format(id=v.id, question=v.question) for v in votes)
        return en.VOTE_NL_WHICH.format(votes=titles), None
    choice = _nl_choice(p, req.choice or "")
    if choice is None:
        return "\n\n".join([en.VOTE_NL_UNCLEAR, "\n".join(_card(p, tz))]), None
    label = _label(p, None if choice == governance.ABSTAIN else choice)
    return en.VOTE_NL_CONFIRM.format(choice=label, id=p.id, question=p.question), (p.id, choice)


def confirm_ballot(
    db: Session,
    member: Member,
    proposal_id: int,
    choice: governance.Choice,
    *,
    signer: decision_log.Signer,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    p = db.get(Proposal, proposal_id)
    if p is None:
        return en.VOTE_CHAT_WHICH
    return _record(db, member, p, choice, signer, now, tz)


# --- welcome ---------------------------------------------------------------------

WELCOME_WAIT_TRIES = 3 * 24 * 60 * 4  # the bot keeps waiting for the invite to be accepted


def _welcome_item(db: Session, n: ChatNotification) -> dict[str, Any] | None:
    """A new member's first private message, and an invite to the COMMONS room."""
    from app import community_room

    member = db.get(Member, n.member_id)
    if member is None or not member.is_active:
        return None
    name = member.display_name or member.matrix_user_id.removeprefix("@").split(":", 1)[0]
    text, html = _render(
        [
            [_Bold(en.WELCOME_HEAD.format(name=name)), en.WELCOME_INTRO],
            [_Bold(en.WELCOME_WHAT), *en.WELCOME_LINES],
            [en.WELCOME_VOTES],
            [en.WELCOME_HELP],
        ]
    )
    return {
        "id": n.id,
        "text": text,
        "html": html,
        "recipients": [member.matrix_user_id],
        "proposal_id": None,
        "keys": [],
        "invite_to": community_room.current(db),
        "max_tries": WELCOME_WAIT_TRIES,
    }
