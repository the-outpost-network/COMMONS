"""The Votes tab: /proposals*. Members propose, vote and read the results.

Every write goes through `app.governance` as the signed-in member, exactly like
the Matrix bot will, so the rules and the decision-log entries are the same.
Every POST needs the CSRF token and redirects afterwards, so reloading never
votes twice. There are no comments or threads: a proposal is its question, its
options and the votes.

Ballots are secret. Pages show anonymous counts (for `after_close` types only
once voting has ended), turnout, and the signed-in member's own choice, never
who chose what. Closed votes show the counts recorded in the decision log.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import case, select

from app import governance
from app.auth import DB, BoardCsrfChecked, CsrfChecked, CurrentSession, SignedIn
from app.i18n import en
from app.models import (
    MAX_OPTIONS,
    MAX_THRESHOLD_PCT,
    MIN_OPTIONS,
    MIN_THRESHOLD_PCT,
    DecisionLog,
    Member,
    Proposal,
    Vote,
)

router = APIRouter(prefix="/proposals")

Question = Annotated[str, Form(max_length=governance.QUESTION_MAX + 20)]
Why = Annotated[str, Form(max_length=governance.WHY_MAX + 50)]
OptionLabel = Annotated[str, Form(max_length=governance.LABEL_MAX + 20)]
Slug = Annotated[str, Form(max_length=40)]
Amount = Annotated[str, Form(max_length=30)]
Threshold = Annotated[str, Form(max_length=10)]
Reason = Annotated[str, Form(max_length=governance.REASON_MAX + 50)]

DONE = {"opened": en.PROPOSAL_OPENED, "cancelled": en.PROPOSAL_CANCELLED_DONE}
OUTCOME_FILTERS = ("passed", "rejected", "cancelled")
LOG_KINDS = ("proposal.opened", "proposal.closed", "proposal.cancelled")
TYPE_TINTS = 3  # .type-0 .. .type-2 in app.css, by the type's place in the config


def _name(member: Member) -> str:
    return member.display_name or member.matrix_user_id


def chat_link(member: Member) -> str:
    """A matrix: URI that opens a private chat in the member's own Matrix app."""
    return f"matrix:u/{quote(member.matrix_user_id.removeprefix('@'), safe=':')}?action=chat"


def _fill(pct: float) -> int:
    """A share as one of the 5 % bar classes (fill-0 .. fill-100)."""
    return max(0, min(100, round(pct / 5) * 5))


def _share(n: int, of: int) -> int:
    """A whole-number share, for bar widths only."""
    return round(n * 100 / of) if of else 0


def share_text(n: int, of: int) -> str:
    """A vote share as shown: rounded DOWN to 2 decimals, the precision of a
    passing share, so it never looks above the threshold when it isn't.
    2 of 3 -> '66.66', 1 of 2 -> '50', 0 -> '0'."""
    if not of:
        return "0"
    value = (Decimal(n * 100) / Decimal(of)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{value.normalize():f}"


def _pct_text(value: Decimal | float | int | None) -> str:
    if value is None:
        return ""
    return f"{Decimal(str(value)).normalize():f} %"


def _pct_number(value: Decimal | float | int) -> str:
    """50 -> '50', 66.67 -> '66.67': the form field's value, without the % sign."""
    return f"{Decimal(str(value)).normalize():f}"


def amount_text(p: Proposal) -> str | None:
    if not p.has_amount:
        return None
    if p.amount is None:
        return en.VOTES_AMOUNT_UNKNOWN
    whole = p.amount == p.amount.to_integral_value()
    number = f"{p.amount:,.0f}" if whole else f"{p.amount:,.2f}"
    return f"{number} {p.currency}"


@dataclass(frozen=True)
class OptionView:
    id: int
    label: str
    count: int | None  # None while counts are hidden
    pct: str | None  # e.g. '66.66'
    fill: int
    leading: bool
    mine: bool


@dataclass(frozen=True)
class VoteView:
    """One proposal as a member may see it: counts only, their own choice only."""

    p: Proposal
    type_name: str
    tint: int
    proposer: str
    proposer_chat: str | None  # open votes, not your own, proposer still active
    amount: str | None
    threshold: str  # this vote's passing share, e.g. "50 %"
    options: list[OptionView]
    show_counts: bool
    cast: int | None
    abstained: int | None
    turnout: int
    eligible: int
    turnout_fill: int
    mine: governance.Choice | None
    mine_label: str | None
    can_vote: bool
    note: str | None  # live hint: is any option above the threshold?
    result: str | None  # passed / rejected / cancelled
    result_text: str | None
    result_pct: str | None
    log_seq: dict[str, int]  # kind -> seq of this proposal's log entries

    @property
    def abstained_mine(self) -> bool:
        return self.mine == governance.ABSTAIN


def _log_entries(db: DB, ids: list[int]) -> dict[int, dict[str, tuple[int, dict[str, Any]]]]:
    """proposal id -> kind -> (seq, payload) of its opened/closed/cancelled entries."""
    if not ids:
        return {}
    rows = db.execute(
        select(DecisionLog.seq, DecisionLog.kind, DecisionLog.payload_json).where(
            DecisionLog.kind.in_(LOG_KINDS),
            DecisionLog.payload_json["proposal_id"].as_integer().in_(ids),
        )
    ).all()
    out: dict[int, dict[str, tuple[int, dict[str, Any]]]] = {}
    for seq, kind, payload in rows:
        out.setdefault(payload["proposal_id"], {})[kind] = (seq, payload)
    return out


def build_views(db: DB, member: Member, proposals: list[Proposal]) -> list[VoteView]:
    ids = [p.id for p in proposals]
    if not ids:
        return []
    eligible_now = governance.eligible_count(db)
    names = governance.type_names(db)
    order = {slug: i for i, slug in enumerate(names)}
    entries = _log_entries(db, ids)
    mine = {
        v.proposal_id: v.option_id
        for v in db.scalars(
            select(Vote).where(
                Vote.member_id == member.id,
                Vote.superseded_at.is_(None),
                Vote.proposal_id.in_(ids),
            )
        )
    }
    views = []
    for p in proposals:
        logged = entries.get(p.id, {})
        closed = logged.get("proposal.closed", (None, None))[1]
        if p.status == "closed" and closed is not None:
            # The record: counts and eligible members as logged at the close.
            counts = {t["option_id"]: t["votes"] for t in closed["tally"]}
            abstained = closed.get("abstained", 0)
            eligible = closed["eligible"]
        else:
            s = governance.standing(db, p)
            counts, abstained, eligible = s.counts, s.abstained, eligible_now
        cast = sum(counts.values())
        turnout = cast + abstained
        show = governance.results_visible(p) and p.status != "cancelled"
        top = max(counts.values(), default=0)
        leaders = [oid for oid, n in counts.items() if n == top and n > 0]
        choice: governance.Choice | None = None
        if p.id in mine:
            choice = governance.ABSTAIN if mine[p.id] is None else mine[p.id]
        options = [
            OptionView(
                id=o.id,
                label=o.label,
                count=counts.get(o.id, 0) if show else None,
                pct=share_text(counts.get(o.id, 0), cast) if show else None,
                fill=_fill(_share(counts.get(o.id, 0), cast)) if show else 0,
                leading=show
                and (o.id == p.outcome_option_id if p.status == "closed" else leaders == [o.id]),
                mine=choice == o.id,
            )
            for o in p.options
        ]
        note = None
        if p.status == "open" and show:
            threshold = _pct_text(p.threshold_pct)
            lead = next((o for o in options if o.leading), None)
            if cast == 0:
                note = en.VOTES_NOTE_NONE
            elif lead and lead.count * 100 > p.threshold_pct * cast:
                note = en.VOTES_NOTE_PASSING.format(label=lead.label, threshold=threshold)
            else:
                note = en.VOTES_NOTE_FAILING.format(threshold=threshold)
        result = result_text = result_pct = None
        if p.status == "cancelled":
            result, result_text = "cancelled", en.VOTES_RESULT["cancelled"]
        elif p.status == "closed":
            result = "passed" if p.outcome == "decided" else "rejected"
            result_pct = share_text(top, cast) if cast else None
            if result == "passed":
                result_text = en.VOTES_RESULT["passed"].format(pct=result_pct)
            elif p.outcome == "tie":
                result_text = en.VOTES_RESULT["tie"]
            elif result_pct is None:
                result_text = en.VOTES_RESULT["no_votes"]
            else:
                result_text = en.VOTES_RESULT["rejected"].format(pct=result_pct)
        views.append(
            VoteView(
                p=p,
                type_name=names.get(p.vote_type, p.vote_type),
                tint=order.get(p.vote_type, 0) % TYPE_TINTS,
                proposer=en.VOTES_ANONYMOUS if p.anonymous else _name(p.creator),
                proposer_chat=(
                    chat_link(p.creator)
                    if p.status == "open"
                    and not p.anonymous
                    and p.created_by != member.id
                    and p.creator.is_active
                    else None
                ),
                amount=amount_text(p),
                threshold=_pct_text(p.threshold_pct),
                options=options,
                show_counts=show,
                cast=cast if show else None,
                abstained=abstained if show else None,
                turnout=turnout,
                eligible=eligible,
                turnout_fill=_fill(_share(turnout, eligible)),
                mine=choice,
                mine_label=(
                    en.VOTES_ABSTAIN
                    if choice == governance.ABSTAIN
                    else governance.label_of(p, choice)
                    if choice is not None
                    else None
                ),
                can_vote=p.status == "open" and member.is_active,
                note=note,
                result=result,
                result_text=result_text,
                result_pct=result_pct,
                log_seq={kind: seq for kind, (seq, _) in logged.items()},
            )
        )
    return views


def soonest_open(db: DB, member: Member) -> VoteView | None:
    """The open vote that closes first, for the Board tab's card."""
    p = db.scalar(
        select(Proposal)
        .where(Proposal.status == "open")
        .order_by(Proposal.closes_at, Proposal.id)
        .limit(1)
    )
    return build_views(db, member, [p])[0] if p is not None else None


def _rules(db: DB):
    try:
        return governance.voting_rules(db)
    except governance.GovernanceError:
        return None


def _render(request: Request, current: CurrentSession, template: str, status=200, **context):
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {
            "current": current,
            "nav": "proposals",
            "can_govern": governance.can_govern(current.member),
            "tz": request.app.state.settings.timezone,
            **context,
        },
        status_code=status,
    )


def _proposal(db: DB, proposal_id: int) -> Proposal:
    p = governance.get(db, proposal_id)
    if p is None:
        raise HTTPException(status_code=404)
    return p


# --- list --------------------------------------------------------------------


@router.get("")
def list_proposals(
    request: Request,
    db: DB,
    current: SignedIn,
    type: Annotated[str, Query(max_length=40)] = "",
    outcome: Annotated[str, Query(max_length=20)] = "",
    done: Annotated[str, Query(max_length=20)] = "",
    p: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """Member: open votes (closing soonest first) with one-tap voting, then past votes."""
    proposals = list(
        db.scalars(
            select(Proposal).order_by(
                case((Proposal.status == "open", 0), else_=1),
                case((Proposal.status == "open", Proposal.closes_at), else_=None),
                Proposal.ended_at.desc(),
                Proposal.id.desc(),
            )
        )
    )
    views = build_views(db, current.member, proposals)
    open_ = [v for v in views if v.p.status == "open"]
    past = [v for v in views if v.p.status != "open"]
    if type:
        past = [v for v in past if v.p.vote_type == type]
    if outcome in OUTCOME_FILTERS:
        past = [v for v in past if v.result == outcome]
    message = None
    if done == "voted" and p is not None:
        voted = next((v for v in open_ + past if v.p.id == p), None)
        if voted is not None and voted.mine_label:
            message = en.VOTES_VOTED.format(label=voted.mine_label, question=voted.p.question)
    return _render(
        request,
        current,
        "proposals.html",
        title=en.VOTES_TITLE,
        open_=open_,
        past=past,
        rules=_rules(db),
        eligible=governance.eligible_count(db),
        filter_type=type,
        filter_outcome=outcome if outcome in OUTCOME_FILTERS else "",
        outcome_filters=OUTCOME_FILTERS,
        done=message,
        pct=_pct_text,
        limits_min=MIN_THRESHOLD_PCT,
    )


# --- new ---------------------------------------------------------------------


def _form(
    request: Request,
    db: DB,
    current: CurrentSession,
    form: dict,
    error: str | None = None,
    status: int = 200,
) -> Response:
    rules = _rules(db)
    count = MIN_OPTIONS + (rules.extra_options if rules else 2)
    form["options"] = (list(form["options"]) + [""] * MAX_OPTIONS)[: min(count, MAX_OPTIONS)]
    return _render(
        request,
        current,
        "proposal_form.html",
        status,
        title=en.PROPOSAL_FORM_TITLE,
        form=form,
        rules=rules,
        error=error or (None if rules else en.VOTES_NOT_CONFIGURED),
        pct=_pct_text,
        limits={
            "question": governance.QUESTION_MAX,
            "why": governance.WHY_MAX,
            "option": governance.LABEL_MAX,
            "threshold_min": MIN_THRESHOLD_PCT,
            "threshold_max": MAX_THRESHOLD_PCT,
        },
    )


@router.get("/new")
def new_proposal(request: Request, db: DB, current: SignedIn) -> Response:
    """Member: an empty proposal, Yes / No filled in."""
    rules = _rules(db)
    form = {
        "vote_type": rules.types[0].slug if rules else "",
        "question": "",
        "why": "",
        "options": list(en.VOTES_DEFAULT_OPTIONS),
        "amount": "",
        "amount_unknown": False,
        "threshold_pct": _pct_number(rules.threshold_pct) if rules else "",
        "anonymous": False,
    }
    return _form(request, db, current, form)


@router.post("")
def create_proposal(
    request: Request,
    db: DB,
    current: CsrfChecked,
    vote_type: Slug,
    question: Question,
    why: Why = "",
    amount: Amount = "",
    amount_unknown: Annotated[str, Form(max_length=5)] = "",
    threshold_pct: Threshold = "",
    anonymous: Annotated[str, Form(max_length=5)] = "",
    option_1: OptionLabel = "",
    option_2: OptionLabel = "",
    option_3: OptionLabel = "",
    option_4: OptionLabel = "",
    option_5: OptionLabel = "",
    option_6: OptionLabel = "",
) -> Response:
    """Member: open a vote. Its type sets the voting period; the proposer may set
    the passing share (at least 50 %; empty = the community's default)."""
    options = [option_1, option_2, option_3, option_4, option_5, option_6]
    unknown = amount_unknown == "1"
    hide_name = anonymous == "1"
    form = {
        "vote_type": vote_type,
        "question": question,
        "why": why,
        "options": options,
        "amount": amount,
        "amount_unknown": unknown,
        "threshold_pct": threshold_pct,
        "anonymous": hide_name,
    }
    try:
        value = governance.parse_amount(amount) if amount.strip() else None
        threshold = governance.parse_threshold(threshold_pct) if threshold_pct.strip() else None
        proposal = governance.open_proposal(
            db,
            current.member,
            question,
            [o for o in options if o.strip()],
            vote_type_slug=vote_type,
            why=why,
            amount=value,
            amount_unknown=unknown,
            threshold_pct=threshold,
            anonymous=hide_name,
            signer=request.app.state.log_signer,
            channel="web",
            tz=ZoneInfo(request.app.state.settings.timezone),
        )
    except governance.GovernanceError as e:
        db.rollback()
        status = 403 if isinstance(e, governance.NotAllowed) else 400
        return _form(request, db, current, form, error=str(e), status=status)
    db.commit()
    return RedirectResponse(f"/proposals/{proposal.id}?done=opened", status_code=303)


# --- one proposal ------------------------------------------------------------


def _detail(
    request: Request,
    db: DB,
    current: CurrentSession,
    proposal: Proposal,
    *,
    done: str | None = None,
    error: str | None = None,
    status: int = 200,
) -> Response:
    (view,) = build_views(db, current.member, [proposal])
    message = DONE.get(done or "")
    if done == "voted" and view.mine_label:
        message = en.VOTES_VOTED.format(label=view.mine_label, question=proposal.question)
    if proposal.quorum_pct is None:
        rules = en.VOTES_RULE_NO_QUORUM.format(threshold=_pct_text(proposal.threshold_pct))
    else:
        rules = en.VOTES_RULE_QUORUM.format(
            quorum=_pct_text(proposal.quorum_pct), threshold=_pct_text(proposal.threshold_pct)
        )
    return _render(
        request,
        current,
        "proposal.html",
        status,
        title=proposal.question,
        v=view,
        rules=rules,
        done=message,
        error=error,
    )


@router.get("/{proposal_id}")
def show_proposal(
    request: Request,
    db: DB,
    current: SignedIn,
    proposal_id: int,
    done: Annotated[str, Query(max_length=20)] = "",
) -> Response:
    """Member: one vote, its anonymous counts, my own choice and the result."""
    return _detail(request, db, current, _proposal(db, proposal_id), done=done)


@router.post("/{proposal_id}/vote")
def vote(
    request: Request,
    db: DB,
    current: CsrfChecked,
    proposal_id: int,
    choice: Annotated[str, Form(max_length=20)] = "",
    back: Annotated[str, Form(max_length=10)] = "",
) -> Response:
    """Member: vote for an option, abstain, or change their vote (one tap)."""
    proposal = _proposal(db, proposal_id)
    picked: governance.Choice
    if choice == governance.ABSTAIN:
        picked = governance.ABSTAIN
    elif choice.isdigit():
        picked = int(choice)
    else:
        return _detail(request, db, current, proposal, error=en.PROPOSAL_PICK_ONE, status=400)
    try:
        governance.cast_vote(
            db,
            current.member,
            proposal.id,
            picked,
            signer=request.app.state.log_signer,
            channel="web",
        )
    except governance.GovernanceError as e:
        db.rollback()
        status = 403 if isinstance(e, governance.NotAllowed) else 400
        db.expire_all()
        return _detail(request, db, current, proposal, error=str(e), status=status)
    db.commit()
    if back == "list":
        return RedirectResponse(
            f"/proposals?done=voted&p={proposal.id}#vote-{proposal.id}", status_code=303
        )
    if back == "board":
        return RedirectResponse("/#open-vote", status_code=303)
    return RedirectResponse(f"/proposals/{proposal.id}?done=voted", status_code=303)


@router.post("/{proposal_id}/cancel")
def cancel(
    request: Request,
    db: DB,
    current: BoardCsrfChecked,
    proposal_id: int,
    reason: Reason = "",
) -> Response:
    """Board/admin: cancel an open proposal, with a reason."""
    proposal = _proposal(db, proposal_id)
    try:
        governance.cancel_proposal(
            db,
            current.member,
            proposal.id,
            reason,
            signer=request.app.state.log_signer,
            channel="web",
        )
    except governance.GovernanceError as e:
        db.rollback()
        db.expire_all()
        return _detail(request, db, current, proposal, error=str(e), status=400)
    db.commit()
    return RedirectResponse(f"/proposals/{proposal.id}?done=cancelled", status_code=303)
