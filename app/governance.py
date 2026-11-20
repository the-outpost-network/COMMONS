"""Votes: the one code path that opens, records, closes and cancels them.

The web, the Matrix bot and the closing scheduler call these functions, with
the same checks, as the acting member. Callers commit (except `close_due`,
which runs its own transactions); every step's decision-log entry is appended
in the same transaction, and events go out after the commit (`app.events`).

Rules (Votes PRD v0.1; values come from the `voting` config):
- any active member opens a proposal: a vote type, a question (max 140), an
  optional "why", 2 options plus up to `extra_options` more, and for types that
  ask for one an amount or "amount unknown". There is no discussion;
- the type sets the voting period (the proposer can't change it) and whether
  anonymous counts show while voting (`live`) or only after the close
  (`after_close`). Type and rule set (quorum, threshold) are copied onto the
  proposal, so a later config change can't alter a live vote. The proposer may
  set the passing share (`threshold_pct`) for this vote: at least 50, under
  100; without one the config's default applies;
- every active member may vote for one option or abstain while the proposal
  is open; voting again changes the vote (the old row is superseded, never
  rewritten);
- ballots are secret: the log says who voted (turnout, reminders), never what
  they chose, and no page shows another member's choice. Only the `vote` table
  links a member to an option. At the close the log gets the counts;
- a proposal closes when its window ends (`close_due`, run in the background)
  or as soon as every active member has voted or abstained. Closing is
  idempotent; only the board or an admin cancels, with a reason.

The outcome is computed by `tally` from the proposal's own rule set:
- no votes cast (nobody voted, or everybody abstained): `no_votes`;
- fewer voters (abstentions count as voters) than `quorum_pct` of the active
  members: `no_quorum` (the default config has no quorum);
- two or more options share the most votes: `tie`;
- the leading option has `threshold_pct` or less of the votes cast:
  `below_threshold` (it needs MORE than the threshold, e.g. more than 50 %);
- otherwise `decided`, for the leading option: the vote passed.
Every outcome except `decided` means the vote was rejected; it can be proposed
again right away. Only current votes of members who are still active count.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import anchors, community, decision_log
from app.chain_verify import NO_DECISION, Result, tally  # noqa: F401  (re-exported)
from app.config import Settings
from app.events import Event, publish
from app.models import (
    MAX_OPTIONS,
    MAX_THRESHOLD_PCT,
    MIN_OPTIONS,
    MIN_THRESHOLD_PCT,
    ChatNotification,
    Member,
    Proposal,
    ProposalOption,
    Vote,
    utcnow,
)

log = logging.getLogger("commons.governance")

Channel = Literal["web", "matrix"]
CHANNELS = ("web", "matrix")
GOVERNING_ROLES = frozenset({"board", "admin"})
VOTING_KEY = community.VOTING_KEY
QUESTION_MAX = 140
WHY_MAX = 1000
LABEL_MAX = 200
REASON_MAX = 500
AMOUNT_MAX = Decimal("9999999999.99")  # Numeric(12, 2)
CLOSE_CHECK_SECONDS = 60
CLOSING_TIME = time(23, 59)
DEFAULT_TZ = ZoneInfo(Settings.timezone)

ABSTAIN: Final = "abstain"
Choice = int | Literal["abstain"]


class GovernanceError(ValueError):
    pass


class NotAllowed(GovernanceError):
    pass


# --- tally (`Result` and `tally` live in app.chain_verify, shared with the
# offline verifier) ------------------------------------------------------------


def eligible_count(db: Session) -> int:
    """Members who may vote: everyone active (every member holds the role `member`)."""
    return db.scalar(
        select(func.count()).select_from(Member).where(Member.deactivated_at.is_(None))
    )


@dataclass(frozen=True)
class Standing:
    """Current anonymous counts of a proposal (active members only)."""

    counts: dict[int, int]  # option id -> votes, every option listed
    abstained: int

    @property
    def cast(self) -> int:
        return sum(self.counts.values())

    @property
    def turnout(self) -> int:
        return self.cast + self.abstained


def standing(db: Session, proposal: Proposal) -> Standing:
    """Votes per option and abstentions right now; counts only, never names."""
    counts = {o.id: 0 for o in proposal.options}
    abstained = 0
    rows = db.execute(
        select(Vote.option_id, func.count())
        .join(Member, Member.id == Vote.member_id)
        .where(
            Vote.proposal_id == proposal.id,
            Vote.superseded_at.is_(None),
            Member.deactivated_at.is_(None),
        )
        .group_by(Vote.option_id)
    ).all()
    for option_id, n in rows:
        if option_id is None:
            abstained = n
        else:
            counts[option_id] = n
    return Standing(counts, abstained)


def current_vote(db: Session, proposal: Proposal, member: Member) -> Vote | None:
    """`member`'s own current ballot. Show it to that member only."""
    return db.scalar(
        select(Vote).where(
            Vote.proposal_id == proposal.id,
            Vote.member_id == member.id,
            Vote.superseded_at.is_(None),
        )
    )


def results_visible(proposal: Proposal) -> bool:
    """Counts may be shown: always for `live` types, otherwise once voting has ended."""
    return proposal.results == "live" or proposal.status != "open"


# --- helpers -----------------------------------------------------------------


def _pct(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _json_pct(value: Decimal | None) -> int | float | None:
    """A percentage as a plain JSON number (50, 66.67) for the log."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _json_amount(value: Decimal | None) -> str | None:
    """An amount as an exact string ("6500.00") for the log; None = unknown."""
    return None if value is None else f"{value:.2f}"


def _text(raw: str, what: str, limit: int, *, required: bool = True) -> str:
    value = " ".join((raw or "").split())
    if not value and required:
        raise GovernanceError(f"the {what} can't be empty")
    if len(value) > limit:
        raise GovernanceError(f"the {what} can be at most {limit} characters")
    return value


def parse_amount(raw: str) -> Decimal:
    """'6500', '6,500' or '6500.50' as a positive amount with at most 2 decimals."""
    text = (raw or "").strip().replace(",", "").replace(" ", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise GovernanceError("the amount must be a number, e.g. 6500") from None
    if not value.is_finite() or value <= 0:
        raise GovernanceError("the amount must be more than 0")
    if value != value.quantize(Decimal("0.01")):
        raise GovernanceError("the amount can have at most 2 decimals")
    if value > AMOUNT_MAX:
        raise GovernanceError("the amount is too large")
    return value.quantize(Decimal("0.01"))


def parse_threshold(raw: str) -> Decimal:
    """'60', '66.67' or '75 %' as a passing share: at least 50, under 100."""
    text = (raw or "").strip().removesuffix("%").strip().replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise GovernanceError("the passing share must be a number, e.g. 60") from None
    return check_threshold(value)


def check_threshold(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise GovernanceError("the passing share must be a number, e.g. 60")
    if not MIN_THRESHOLD_PCT <= value < MAX_THRESHOLD_PCT:
        raise GovernanceError(
            f"the passing share must be at least {MIN_THRESHOLD_PCT} % "
            f"and under {MAX_THRESHOLD_PCT} %"
        )
    if value != value.quantize(Decimal("0.01")):
        raise GovernanceError("the passing share can have at most 2 decimals")
    return value.quantize(Decimal("0.01"))


def _check_channel(channel: str) -> None:
    if channel not in CHANNELS:
        raise GovernanceError("unknown channel")


def _check_active(member: Member) -> None:
    if not member.is_active:
        raise NotAllowed("your membership is deactivated")


def can_govern(member: Member) -> bool:
    """Cancel proposals: active board members and admins."""
    return member.is_active and bool(member.role_names & GOVERNING_ROLES)


def _check_governs(member: Member) -> None:
    _check_active(member)
    if not can_govern(member):
        raise NotAllowed("only the board or an admin can do that")


def voting_rules(db: Session) -> community.Voting:
    """The current voting rules and vote types, checked."""
    raw = community.get(db, VOTING_KEY)
    if raw is None:
        raise GovernanceError("the voting rules are not configured")
    try:
        return community.Voting.model_validate(raw)
    except ValidationError:
        raise GovernanceError(
            "the voting rules are outdated or wrong; an admin has to fix them"
        ) from None


def vote_type(rules: community.Voting, slug: str) -> community.VoteType:
    for t in rules.types:
        if t.slug == slug:
            return t
    raise GovernanceError("that is not one of the vote types")


def type_names(db: Session) -> dict[str, str]:
    """Slug -> current name, for display; empty if the rules are missing."""
    try:
        return {t.slug: t.name for t in voting_rules(db).types}
    except GovernanceError:
        return {}


def _lock(db: Session, proposal_id: int) -> Proposal:
    """The proposal row, locked until commit: votes and closing queue up behind it."""
    proposal = db.scalar(
        select(Proposal)
        .where(Proposal.id == proposal_id)
        .with_for_update(of=Proposal)
        .execution_options(populate_existing=True)
    )
    if proposal is None:
        raise GovernanceError("there is no such proposal")
    return proposal


def _option_entries(proposal: Proposal) -> list[dict[str, Any]]:
    return [{"option_id": o.id, "position": o.position, "label": o.label} for o in proposal.options]


def label_of(proposal: Proposal, option_id: int | None) -> str | None:
    return next((o.label for o in proposal.options if o.id == option_id), None)


def get(db: Session, proposal_id: int) -> Proposal | None:
    return db.get(Proposal, proposal_id)


# --- opening -----------------------------------------------------------------


def closing_time(opens_at: datetime, days: int, tz: ZoneInfo) -> datetime:
    """23:59 local time on the day the full period ends, never less than `days` x 24 h."""
    earliest = opens_at + timedelta(days=days)
    day = earliest.astimezone(tz).date()
    closes = datetime.combine(day, CLOSING_TIME, tz)
    if closes < earliest:  # opened in the last minute of a day
        closes = datetime.combine(day + timedelta(days=1), CLOSING_TIME, tz)
    return closes.astimezone(UTC)


def _notify(db: Session, proposal: Proposal, kind: str, now: datetime) -> None:
    """Queue the bot's message in the same transaction as the change it announces."""
    db.add(ChatNotification(proposal_id=proposal.id, kind=kind, created_at=now))


def open_proposal(
    db: Session,
    member: Member,
    question: str,
    options: Sequence[str],
    *,
    vote_type_slug: str,
    signer: decision_log.Signer,
    channel: Channel,
    why: str = "",
    amount: Decimal | None = None,
    amount_unknown: bool = False,
    threshold_pct: Decimal | None = None,
    now: datetime | None = None,
    tz: ZoneInfo = DEFAULT_TZ,
    anonymous: bool = False,
) -> Proposal:
    """Open a proposal now; it closes at 23:59 (`tz`) once its type's period has passed.

    `threshold_pct` is this vote's passing share (an option needs more than it);
    None takes the config's default. It can't be below 50 or reach 100.
    """
    now = now or utcnow()
    _check_active(member)
    _check_channel(channel)
    rules = voting_rules(db)
    kind = vote_type(rules, vote_type_slug)
    question = _text(question, "question", QUESTION_MAX)
    why = _text(why, "explanation", WHY_MAX, required=False)
    labels = [_text(o, "option", LABEL_MAX) for o in options]
    most = min(MIN_OPTIONS + rules.extra_options, MAX_OPTIONS)
    if not MIN_OPTIONS <= len(labels) <= most:
        raise GovernanceError(f"a proposal needs {MIN_OPTIONS} to {most} options")
    if any(label.casefold() == ABSTAIN for label in labels):
        raise GovernanceError("“Abstain” is always offered; it can't be an option")
    if len({label.casefold() for label in labels}) != len(labels):
        raise GovernanceError("the options must be different from each other")
    if kind.amount:
        if amount is None and not amount_unknown:
            raise GovernanceError("give the amount, or say that it is unknown")
        if amount is not None and amount_unknown:
            raise GovernanceError("give an amount or say that it is unknown, not both")
        if amount is not None and not (0 < amount <= AMOUNT_MAX):
            raise GovernanceError("the amount must be more than 0")
    elif amount is not None or amount_unknown:
        raise GovernanceError(f"{kind.name} votes have no amount")
    quorum = None if rules.quorum_pct is None else _pct(rules.quorum_pct)
    threshold = check_threshold(
        _pct(rules.threshold_pct) if threshold_pct is None else Decimal(str(threshold_pct))
    )

    proposal = Proposal(
        question=question,
        why=why,
        vote_type=kind.slug,
        results=kind.results,
        has_amount=kind.amount,
        amount=amount,
        currency=rules.currency if kind.amount else None,
        created_by=member.id,
        anonymous=anonymous,
        created_at=now,
        opens_at=now,
        closes_at=closing_time(now, kind.duration_days, tz),
        quorum_pct=quorum,
        threshold_pct=threshold,
        status="open",
    )
    db.add(proposal)
    db.flush()
    db.add_all(
        ProposalOption(proposal_id=proposal.id, position=i, label=label)
        for i, label in enumerate(labels, start=1)
    )
    db.flush()
    db.refresh(proposal)
    payload = {
        "proposal_id": proposal.id,
        "vote_type": kind.slug,
        "question": question,
        "why": why,
        "options": _option_entries(proposal),
        "amount": _json_amount(proposal.amount),
        "currency": proposal.currency,
        "has_amount": proposal.has_amount,
        "results": proposal.results,
        "ballots": "secret",
        "opens_at": decision_log.format_ts(proposal.opens_at),
        "closes_at": decision_log.format_ts(proposal.closes_at),
        "quorum_pct": _json_pct(quorum),
        "threshold_pct": _json_pct(threshold),
        "channel": channel,
    }
    if anonymous:
        payload["anonymous"] = True
    actor = None if anonymous else member.id
    decision_log.append(db, signer, "proposal.opened", payload, actor)
    _notify(db, proposal, "opened", now)
    publish(
        db,
        Event(
            "proposal.opened",
            {
                "proposal_id": proposal.id,
                "vote_type": kind.slug,
                "question": question,
                "options": [o.label for o in proposal.options],
                "results": proposal.results,
                "closes_at": proposal.closes_at.isoformat(),
            },
        ),
    )
    return proposal


# --- voting ------------------------------------------------------------------


def cast_vote(
    db: Session,
    member: Member,
    proposal_id: int,
    choice: Choice,
    *,
    signer: decision_log.Signer,
    channel: Channel,
    now: datetime | None = None,
) -> Vote:
    """Vote for an option id or `ABSTAIN`, or change the vote, as `member`.

    The same choice again is a no-op. The log records that `member` voted, not
    the choice. Closes the proposal right away when every active member has voted.
    """
    now = now or utcnow()
    _check_active(member)
    _check_channel(channel)
    proposal = _lock(db, proposal_id)
    if proposal.status != "open":
        raise GovernanceError(f"this proposal is {proposal.status}; votes are no longer taken")
    if now >= proposal.closes_at:
        raise GovernanceError("voting on this proposal has ended")
    if now < proposal.opens_at:
        raise GovernanceError("voting on this proposal hasn't started")
    if choice == ABSTAIN:
        option_id = None
    elif isinstance(choice, int) and not isinstance(choice, bool) and label_of(proposal, choice):
        option_id = choice
    else:
        raise GovernanceError("that is not one of this proposal's options")

    previous = current_vote(db, proposal, member)
    if previous is not None and previous.option_id == option_id:
        return previous
    if previous is not None:
        # Supersede first: the database allows one current vote per member.
        db.execute(update(Vote).where(Vote.id == previous.id).values(superseded_at=now))
    vote = Vote(
        proposal_id=proposal.id,
        option_id=option_id,
        member_id=member.id,
        channel=channel,
        cast_at=now,
    )
    db.add(vote)
    db.flush()
    kind = "vote.cast" if previous is None else "vote.changed"
    # Secret ballot: who voted and how, never what. No option, label or vote id.
    payload = {
        "proposal_id": proposal.id,
        "question": proposal.question,
        "member_id": member.id,
        "matrix_user_id": member.matrix_user_id,
        "channel": channel,
    }
    decision_log.append(db, signer, kind, payload, member.id)
    publish(
        db,
        Event(
            kind,
            {"proposal_id": proposal.id, "member_id": member.id, "channel": channel},
        ),
    )
    if standing(db, proposal).turnout >= eligible_count(db):
        _close(db, proposal, signer, now, trigger="all_voted")
    db.refresh(vote)
    return vote


# --- closing and cancelling -------------------------------------------------


def _close(
    db: Session, proposal: Proposal, signer: decision_log.Signer, now: datetime, *, trigger: str
) -> Result:
    now_standing = standing(db, proposal)
    result = tally(
        now_standing.counts,
        eligible_count(db),
        proposal.quorum_pct,
        proposal.threshold_pct,
        now_standing.abstained,
    )
    proposal.status = "closed"
    proposal.ended_at = now
    proposal.outcome = result.outcome
    proposal.outcome_option_id = result.option_id
    db.flush()
    payload = {
        "proposal_id": proposal.id,
        "question": proposal.question,
        "trigger": trigger,  # window_ended or all_voted
        "tally": [
            {"option_id": o.id, "label": o.label, "votes": result.counts[o.id]}
            for o in proposal.options
        ],
        "cast": result.cast,
        "abstained": result.abstained,
        "turnout": result.turnout,
        "eligible": result.eligible,
        "quorum_pct": _json_pct(proposal.quorum_pct),
        "threshold_pct": _json_pct(proposal.threshold_pct),
        "quorum_met": result.quorum_met,
        "outcome": result.outcome,
        "result": "passed" if result.decided else "rejected",
        "outcome_option_id": result.option_id,
        "outcome_label": label_of(proposal, result.option_id),
    }
    # Closing follows the rules, not a person: no actor.
    decision_log.append(db, signer, "proposal.closed", payload, None)
    # Members get the fingerprint of this close in the community room.
    anchors.anchor(db, signer, "proposal.closed", proposal_id=proposal.id)
    _notify(db, proposal, "closed", now)
    publish(
        db,
        Event(
            "proposal.closed",
            {
                "proposal_id": proposal.id,
                "question": proposal.question,
                "outcome": result.outcome,
                "outcome_label": payload["outcome_label"],
                "tally": payload["tally"],
                "cast": result.cast,
                "abstained": result.abstained,
                "eligible": result.eligible,
            },
        ),
    )
    return result


def close_if_due(
    db: Session, proposal_id: int, *, signer: decision_log.Signer, now: datetime | None = None
) -> Result | None:
    """Close the proposal if its window has ended. Idempotent: None if nothing to do."""
    now = now or utcnow()
    proposal = _lock(db, proposal_id)
    if proposal.status != "open" or now < proposal.closes_at:
        return None
    return _close(db, proposal, signer, now, trigger="window_ended")


def close_due(
    make_session: sessionmaker[Session],
    signer: decision_log.Signer,
    now: datetime | None = None,
) -> int:
    """Close every open proposal whose window has ended, one transaction each."""
    now = now or utcnow()
    with make_session() as db:
        due = db.scalars(
            select(Proposal.id).where(Proposal.status == "open", Proposal.closes_at <= now)
        ).all()
    closed = 0
    for proposal_id in due:
        try:
            with make_session() as db, db.begin():
                if close_if_due(db, proposal_id, signer=signer, now=now) is not None:
                    closed += 1
        except Exception:
            log.exception("closing proposal %s failed", proposal_id)
    return closed


def in_reminder_window(rules: community.Voting, now: datetime, tz: ZoneInfo) -> bool:
    start, end = rules.reminder_window
    return start <= now.astimezone(tz).time() <= end


def due_reminder(
    rules: community.Voting, opens_at: datetime, closes_at: datetime, now: datetime, tz: ZoneInfo
) -> time | None:
    """The latest reminder time of the closing day that has passed, if one may go out now."""
    if now >= closes_at or not in_reminder_window(rules, now, tz):
        return None
    day = closes_at.astimezone(tz).date()
    due = [
        t
        for t in rules.reminder_times
        if opens_at < datetime.combine(day, t, tz) <= now
        and datetime.combine(day, t, tz) < closes_at
    ]
    return max(due, default=None)


def queue_reminders(
    make_session: sessionmaker[Session], now: datetime | None = None, tz: ZoneInfo = DEFAULT_TZ
) -> int:
    """Queue reminders due on a vote's closing day (`voting.reminder_times`, local time).

    If several are due at once (the box was off), only the latest counts. Nothing is
    queued outside `voting.reminder_window`.
    """
    now = now or utcnow()
    queued = 0
    with make_session() as db, db.begin():
        rules = voting_rules(db)
        votes = db.scalars(
            select(Proposal).where(Proposal.status == "open", Proposal.closes_at > now)
        ).all()
        for proposal in votes:
            due = due_reminder(rules, proposal.opens_at, proposal.closes_at, now, tz)
            if due is None:
                continue
            added = db.execute(
                insert(ChatNotification)
                .values(
                    proposal_id=proposal.id,
                    kind="reminder",
                    reminder_minute=due.hour * 60 + due.minute,
                    created_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_chat_notification_proposal_kind")
                .returning(ChatNotification.id)
            )
            queued += added.first() is not None
    return queued


async def run_closer(
    make_session: sessionmaker[Session],
    signer: decision_log.Signer,
    interval: float = CLOSE_CHECK_SECONDS,
    tz: ZoneInfo = DEFAULT_TZ,
) -> None:
    """Background loop started by the app's lifespan (like the weather refresh)."""
    while True:
        try:
            await asyncio.to_thread(close_due, make_session, signer)
        except Exception:
            log.exception("closing due proposals failed")
        try:
            await asyncio.to_thread(queue_reminders, make_session, None, tz)
        except Exception:
            log.exception("queueing vote reminders failed")
        await asyncio.sleep(interval)


def cancel_proposal(
    db: Session,
    member: Member,
    proposal_id: int,
    reason: str,
    *,
    signer: decision_log.Signer,
    channel: Channel,
    now: datetime | None = None,
) -> Proposal:
    """Withdraw an open proposal (board or admin, with a reason). No outcome is computed."""
    now = now or utcnow()
    _check_governs(member)
    _check_channel(channel)
    reason = _text(reason, "reason", REASON_MAX)
    proposal = _lock(db, proposal_id)
    if proposal.status != "open":
        raise GovernanceError(f"this proposal is already {proposal.status}")
    proposal.status = "cancelled"
    proposal.ended_at = now
    proposal.ended_by = member.id
    proposal.cancel_reason = reason
    db.flush()
    decision_log.append(
        db,
        signer,
        "proposal.cancelled",
        {
            "proposal_id": proposal.id,
            "question": proposal.question,
            "reason": reason,
            "channel": channel,
        },
        member.id,
    )
    _notify(db, proposal, "cancelled", now)
    publish(
        db,
        Event(
            "proposal.cancelled",
            {"proposal_id": proposal.id, "question": proposal.question, "reason": reason},
        ),
    )
    return proposal
