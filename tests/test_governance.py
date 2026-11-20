"""Votes through the service layer (app/governance.py)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from tests.conftest import VOTING

from app import decision_log, events, governance, members
from app.db import make_sessionmaker
from app.governance import ABSTAIN, GovernanceError, NotAllowed
from app.models import ConfigKV, DecisionLog, Proposal, Vote, utcnow

STOCKHOLM = ZoneInfo("Europe/Stockholm")


@pytest.fixture
def make_session(clean_db):
    return make_sessionmaker(clean_db)


@pytest.fixture
def db(make_session, log_signer):
    with make_session() as s:
        s.add(ConfigKV(key="voting", value_json=VOTING))
        s.commit()
        yield s


@pytest.fixture
def people(db, log_signer):
    """An admin, a board member and two plain members: four eligible voters."""

    def add(name, *roles):
        return members.add_member(db, f"@{name}:x.test", name.title(), roles, signer=log_signer)

    p = {
        "admin": add("ada", "admin"),
        "board": add("bo", "board"),
        "anna": add("anna"),
        "ben": add("ben"),
    }
    db.commit()
    return p


@pytest.fixture
def received():
    seen: list[events.Event] = []
    unsubscribe = events.subscribe(seen.append)
    yield seen
    unsubscribe()


def open_(db, signer, member, options=("Yes", "No"), vote_type="rules", **kw):
    p = governance.open_proposal(
        db,
        member,
        "Raise the firewood alert to 8 baskets?",
        options,
        vote_type_slug=vote_type,
        signer=signer,
        channel="web",
        **kw,
    )
    db.commit()
    return p


def vote(db, signer, member, proposal, position, channel="matrix", **kw):
    """Vote for option `position` (1-based), or abstain with position=ABSTAIN."""
    choice = ABSTAIN if position == ABSTAIN else proposal.options[position - 1].id
    v = governance.cast_vote(db, member, proposal.id, choice, signer=signer, channel=channel, **kw)
    db.commit()
    return v


def log_kinds(db) -> list[str]:
    return list(db.scalars(select(DecisionLog.kind).order_by(DecisionLog.seq)))


def log_of(db, kind: str) -> list[dict]:
    return [
        r.payload_json
        for r in db.scalars(select(DecisionLog).where(DecisionLog.kind == kind).order_by("seq"))
    ]


# --- opening -------------------------------------------------------------------


@pytest.mark.parametrize("who", ["anna", "board", "admin"])
def test_any_member_opens_a_vote(db, log_signer, people, who):
    p = open_(db, log_signer, people[who], options=("Yes", "No", "Later"))
    assert p.status == "open"
    assert [o.label for o in p.options] == ["Yes", "No", "Later"]
    assert p.created_by == people[who].id
    entry = log_of(db, "proposal.opened")[-1]
    assert entry["proposal_id"] == p.id
    assert entry["vote_type"] == "rules"
    assert entry["ballots"] == "secret"
    assert [o["label"] for o in entry["options"]] == ["Yes", "No", "Later"]
    assert (entry["quorum_pct"], entry["threshold_pct"]) == (None, 50)


def test_deactivated_member_cannot_open(db, log_signer, people):
    people["anna"].deactivated_at = utcnow()
    db.commit()
    with pytest.raises(NotAllowed, match="deactivated"):
        open_(db, log_signer, people["anna"])
    db.rollback()
    assert db.scalar(select(Proposal.id)) is None


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (("Yes",), "2 to 4 options"),
        (("Yes", "No", "Later", "Never", "Maybe"), "2 to 4 options"),
        (("Yes", " yes "), "different"),
        (("Yes", "  "), "option can't be empty"),
        (("Yes", "x" * 201), "at most 200"),
        (("Yes", "Abstain"), "always offered"),
    ],
)
def test_options_are_checked(db, log_signer, people, options, message):
    with pytest.raises(GovernanceError, match=message):
        open_(db, log_signer, people["anna"], options=options)


def test_extra_options_come_from_the_config(db, log_signer, people):
    db.get(ConfigKV, "voting").value_json = {**VOTING, "extra_options": 0}
    db.commit()
    with pytest.raises(GovernanceError, match="2 to 2 options"):
        open_(db, log_signer, people["anna"], options=("Yes", "No", "Later"))


def test_question_and_why_are_checked(db, log_signer, people):
    for question, why, message in [
        ("   ", "", "question can't be empty"),
        ("x" * 141, "", "at most 140"),
        ("Q?", "x" * 1001, "at most 1000"),
    ]:
        with pytest.raises(GovernanceError, match=message):
            governance.open_proposal(
                db, people["anna"], question, ("A", "B"), vote_type_slug="rules", why=why,
                signer=log_signer, channel="web",
            )  # fmt: skip
    p = governance.open_proposal(
        db, people["anna"], "Q?", ("A", "B"), vote_type_slug="rules", why="  Because\n it's cold ",
        signer=log_signer, channel="web",
    )  # fmt: skip
    assert p.why == "Because it's cold"


@pytest.mark.parametrize(("vote_type", "days", "results"), [
    ("spending", 1, "live"), ("rules", 3, "live"), ("events", 2, "after_close"),
])  # fmt: skip
def test_type_sets_window_and_visibility(db, log_signer, people, vote_type, days, results):
    now = utcnow()
    extra = {"amount_unknown": True} if vote_type == "spending" else {}
    p = open_(db, log_signer, people["anna"], vote_type=vote_type, now=now, **extra)
    assert p.opens_at == now
    assert p.closes_at == governance.closing_time(now, days, STOCKHOLM)
    assert p.closes_at - now >= timedelta(days=days)
    assert p.closes_at.astimezone(STOCKHOLM).strftime("%H:%M:%S") == "23:59:00"
    assert (p.vote_type, p.results) == (vote_type, results)
    assert log_of(db, "proposal.opened")[-1]["results"] == results


def test_unknown_type_is_rejected(db, log_signer, people):
    with pytest.raises(GovernanceError, match="not one of the vote types"):
        open_(db, log_signer, people["anna"], vote_type="elections")


def test_spending_needs_an_amount_or_amount_unknown(db, log_signer, people):
    with pytest.raises(GovernanceError, match="give the amount"):
        open_(db, log_signer, people["anna"], vote_type="spending")
    with pytest.raises(GovernanceError, match="not both"):
        open_(db, log_signer, people["anna"], vote_type="spending",
              amount=Decimal(6500), amount_unknown=True)  # fmt: skip
    priced = open_(db, log_signer, people["anna"], vote_type="spending", amount=Decimal("6500"))
    assert (priced.has_amount, priced.amount, priced.currency) == (True, Decimal(6500), "SEK")
    assert log_of(db, "proposal.opened")[-1]["amount"] == "6500.00"
    unknown = open_(db, log_signer, people["anna"], vote_type="spending", amount_unknown=True)
    assert (unknown.has_amount, unknown.amount, unknown.currency) == (True, None, "SEK")
    assert log_of(db, "proposal.opened")[-1]["amount"] is None


def test_other_types_have_no_amount(db, log_signer, people):
    with pytest.raises(GovernanceError, match="Rules votes have no amount"):
        open_(db, log_signer, people["anna"], amount=Decimal(10))
    with pytest.raises(GovernanceError, match="Rules votes have no amount"):
        open_(db, log_signer, people["anna"], amount_unknown=True)
    p = open_(db, log_signer, people["anna"])
    assert (p.has_amount, p.amount, p.currency) == (False, None, None)


@pytest.mark.parametrize(("raw", "value"), [
    ("6500", Decimal("6500.00")), ("6,500", Decimal("6500.00")), (" 12.5 ", Decimal("12.50")),
])  # fmt: skip
def test_amounts_are_parsed(raw, value):
    assert governance.parse_amount(raw) == value


@pytest.mark.parametrize(("raw", "message"), [
    ("lots", "must be a number"), ("0", "more than 0"), ("-5", "more than 0"),
    ("1.234", "2 decimals"), ("1e20", "too large"), ("NaN", "more than 0"),
])  # fmt: skip
def test_bad_amounts_are_refused(raw, message):
    with pytest.raises(GovernanceError, match=message):
        governance.parse_amount(raw)


@pytest.mark.parametrize("threshold", [Decimal(t) for t in ("50", "60", "66.67", "99.99")])
def test_the_proposer_sets_the_passing_share(db, log_signer, people, threshold):
    p = open_(db, log_signer, people["anna"], threshold_pct=threshold)
    assert p.threshold_pct == threshold
    assert Decimal(str(log_of(db, "proposal.opened")[-1]["threshold_pct"])) == threshold


@pytest.mark.parametrize(("threshold", "message"), [
    (Decimal("49.99"), "at least 50"), (Decimal(0), "at least 50"), (Decimal(100), "under 100"),
    (Decimal("60.123"), "2 decimals"), (Decimal("NaN"), "must be a number"),
])  # fmt: skip
def test_the_passing_share_is_never_below_a_majority(db, log_signer, people, threshold, message):
    with pytest.raises(GovernanceError, match=message):
        open_(db, log_signer, people["anna"], threshold_pct=threshold)
    db.rollback()
    assert log_of(db, "proposal.opened") == []


@pytest.mark.parametrize(("raw", "value"), [
    ("60", Decimal(60)), (" 66.67 ", Decimal("66.67")), ("75 %", Decimal(75)),
    ("66,67", Decimal("66.67")),
])  # fmt: skip
def test_passing_share_from_a_form(raw, value):
    assert governance.parse_threshold(raw) == value


@pytest.mark.parametrize("raw", ["lots", "49", "100", "Infinity", ""])
def test_bad_passing_shares_are_refused(raw):
    with pytest.raises(GovernanceError, match="passing share"):
        governance.parse_threshold(raw)


def test_a_higher_passing_share_decides_the_close(db, log_signer, people):
    """3 of 4 is 75 %: passes over 66.67, but not over 75."""
    results = []
    for threshold in (Decimal("66.67"), Decimal(75)):
        p = open_(db, log_signer, people["anna"], threshold_pct=threshold)
        for who, pos in (("admin", 1), ("board", 1), ("anna", 1), ("ben", 2)):
            vote(db, log_signer, people[who], p, pos)
        db.refresh(p)
        results.append((p.status, p.outcome))
    assert results == [("closed", "decided"), ("closed", "below_threshold")]


def test_rules_are_copied_at_opening(db, log_signer, people):
    p = open_(db, log_signer, people["anna"])
    changed = {**VOTING, "quorum_pct": 90, "threshold_pct": 75}
    changed["types"] = [{**t, "results": "after_close"} for t in VOTING["types"]]
    db.get(ConfigKV, "voting").value_json = changed
    db.commit()
    db.refresh(p)
    assert (p.quorum_pct, p.threshold_pct, p.results) == (None, Decimal(50), "live")
    later = open_(db, log_signer, people["anna"])
    assert (later.quorum_pct, later.threshold_pct, later.results) == (
        Decimal(90), Decimal(75), "after_close"
    )  # fmt: skip


def test_missing_or_outdated_voting_rules_are_reported(db, log_signer, people):
    db.get(ConfigKV, "voting").value_json = {
        "default_duration_days": 7, "quorum_pct": 50, "threshold_pct": 50
    }  # fmt: skip
    db.commit()
    with pytest.raises(GovernanceError, match="outdated"):
        open_(db, log_signer, people["anna"])
    db.rollback()
    db.delete(db.get(ConfigKV, "voting"))
    db.commit()
    with pytest.raises(GovernanceError, match="not configured"):
        open_(db, log_signer, people["anna"])


# --- voting ------------------------------------------------------------------


def test_member_votes_and_the_log_keeps_the_ballot_secret(db, log_signer, people, received):
    p = open_(db, log_signer, people["board"])
    v = vote(db, log_signer, people["anna"], p, 1, channel="matrix")
    assert (v.member_id, v.option_id, v.channel) == (people["anna"].id, p.options[0].id, "matrix")
    entry = db.scalars(select(DecisionLog).where(DecisionLog.kind == "vote.cast")).one()
    assert entry.actor_member_id == people["anna"].id
    # Who voted, on what, through which channel: never the choice.
    assert entry.payload_json == {
        "proposal_id": p.id,
        "question": p.question,
        "member_id": people["anna"].id,
        "matrix_user_id": "@anna:x.test",
        "channel": "matrix",
    }
    assert received[-1].kind == "vote.cast"
    assert received[-1].data == {
        "proposal_id": p.id, "member_id": people["anna"].id, "channel": "matrix"
    }  # fmt: skip


def test_change_supersedes_the_old_vote_and_logs_no_choice(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    first = vote(db, log_signer, people["anna"], p, 1)
    second = vote(db, log_signer, people["anna"], p, 2, channel="web")
    rows = db.scalars(select(Vote).where(Vote.member_id == people["anna"].id).order_by(Vote.id))
    old, new = rows.all()
    assert (old.id, new.id) == (first.id, second.id)
    assert old.superseded_at is not None
    assert new.superseded_at is None
    assert governance.standing(db, p).counts == {p.options[0].id: 0, p.options[1].id: 1}
    changed = log_of(db, "vote.changed")[-1]
    assert set(changed) == {"proposal_id", "question", "member_id", "matrix_user_id", "channel"}


def test_abstaining_counts_for_turnout_not_as_a_vote_cast(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    v = vote(db, log_signer, people["anna"], p, ABSTAIN)
    assert v.option_id is None
    vote(db, log_signer, people["ben"], p, 1)
    s = governance.standing(db, p)
    assert (s.abstained, s.cast, s.turnout) == (1, 1, 2)
    # Abstaining, then choosing: an ordinary change.
    vote(db, log_signer, people["anna"], p, 2)
    assert governance.standing(db, p).abstained == 0
    assert log_kinds(db)[-1] == "vote.changed"


def test_same_vote_again_changes_nothing(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    first = vote(db, log_signer, people["anna"], p, 1)
    before = log_kinds(db)
    assert vote(db, log_signer, people["anna"], p, 1).id == first.id
    abstained = vote(db, log_signer, people["ben"], p, ABSTAIN)
    assert vote(db, log_signer, people["ben"], p, ABSTAIN).id == abstained.id
    assert log_kinds(db) == [*before, "vote.cast"]
    assert db.query(Vote).count() == 2


def test_vote_on_unknown_option_is_rejected(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    other = open_(db, log_signer, people["board"])
    for choice in (other.options[0].id, 999_999, True, "yes", None):
        with pytest.raises(GovernanceError, match="not one of this proposal's options"):
            governance.cast_vote(db, people["anna"], p.id, choice, signer=log_signer, channel="web")
        db.rollback()


def test_vote_on_unknown_proposal_is_rejected(db, log_signer, people):
    with pytest.raises(GovernanceError, match="no such proposal"):
        governance.cast_vote(db, people["anna"], 999_999, 1, signer=log_signer, channel="web")


def test_vote_after_close_is_rejected(db, log_signer, people):
    p = open_(db, log_signer, people["board"], now=utcnow() - timedelta(days=4))
    with pytest.raises(GovernanceError, match="has ended"):
        vote(db, log_signer, people["anna"], p, 1)
    db.rollback()
    governance.close_if_due(db, p.id, signer=log_signer)
    db.commit()
    with pytest.raises(GovernanceError, match="is closed"):
        vote(db, log_signer, people["anna"], p, 1)


def test_vote_on_cancelled_proposal_is_rejected(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    governance.cancel_proposal(
        db, people["board"], p.id, "Asked the wrong question", signer=log_signer, channel="web"
    )
    db.commit()
    with pytest.raises(GovernanceError, match="is cancelled"):
        vote(db, log_signer, people["anna"], p, 1)


def test_deactivated_member_cannot_vote(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    people["ben"].deactivated_at = utcnow()
    db.commit()
    with pytest.raises(NotAllowed, match="deactivated"):
        vote(db, log_signer, people["ben"], p, 1)


def test_unknown_channel_is_rejected(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    with pytest.raises(GovernanceError, match="unknown channel"):
        vote(db, log_signer, people["anna"], p, 1, channel="sms")


def test_results_visibility_follows_the_type(db, log_signer, people):
    live = open_(db, log_signer, people["anna"])
    hidden = open_(db, log_signer, people["anna"], vote_type="events")
    assert governance.results_visible(live) is True
    assert governance.results_visible(hidden) is False
    governance.cancel_proposal(db, people["board"], hidden.id, "x", signer=log_signer,
                               channel="web")  # fmt: skip
    db.commit()
    assert governance.results_visible(hidden) is True


# --- closing -----------------------------------------------------------------


def test_closes_early_when_everyone_voted_or_abstained(db, log_signer, people, received):
    p = open_(db, log_signer, people["board"])
    vote(db, log_signer, people["admin"], p, 1)
    vote(db, log_signer, people["board"], p, 1)
    vote(db, log_signer, people["anna"], p, ABSTAIN)
    assert db.get(Proposal, p.id).status == "open"
    vote(db, log_signer, people["ben"], p, 2)
    db.refresh(p)
    assert (p.status, p.outcome, p.outcome_option_id) == ("closed", "decided", p.options[0].id)
    closed = log_of(db, "proposal.closed")[-1]
    assert closed["trigger"] == "all_voted"
    assert (closed["cast"], closed["abstained"], closed["turnout"], closed["eligible"]) == (
        3, 1, 4, 4
    )  # fmt: skip
    assert [t["votes"] for t in closed["tally"]] == [2, 1]
    assert (closed["result"], closed["outcome_label"]) == ("passed", "Yes")
    assert [e.kind for e in received][-2:] == ["vote.cast", "proposal.closed"]


def test_exactly_half_is_rejected(db, log_signer, people):
    """More than 50 % of the votes cast wins; 2 of 4 is not more than half."""
    p = open_(db, log_signer, people["board"], options=("Yes", "No", "Later"))
    vote(db, log_signer, people["admin"], p, 1)
    vote(db, log_signer, people["board"], p, 1)
    vote(db, log_signer, people["anna"], p, 2)
    vote(db, log_signer, people["ben"], p, 3)
    db.refresh(p)
    assert (p.outcome, p.outcome_option_id) == ("below_threshold", None)
    assert log_of(db, "proposal.closed")[-1]["result"] == "rejected"


def test_everyone_abstaining_decides_nothing(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    for who in people:
        vote(db, log_signer, people[who], p, ABSTAIN)
    db.refresh(p)
    assert (p.status, p.outcome) == ("closed", "no_votes")


def test_deactivated_members_neither_count_nor_block_early_close(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    vote(db, log_signer, people["ben"], p, 2)
    people["ben"].deactivated_at = utcnow()
    db.commit()
    for who in ("admin", "board"):
        vote(db, log_signer, people[who], p, 1)
    assert db.get(Proposal, p.id).status == "open"
    vote(db, log_signer, people["anna"], p, 1)
    closed = log_of(db, "proposal.closed")[-1]
    assert (closed["cast"], closed["eligible"]) == (3, 3)
    assert [t["votes"] for t in closed["tally"]] == [3, 0]


def test_closes_at_the_end_of_the_window(db, log_signer, people, make_session):
    due = open_(db, log_signer, people["board"], now=utcnow() - timedelta(days=4))
    running = open_(db, log_signer, people["board"])
    assert governance.close_due(make_session, log_signer) == 1
    db.expire_all()
    assert db.get(Proposal, due.id).status == "closed"
    assert db.get(Proposal, due.id).outcome == "no_votes"
    assert db.get(Proposal, running.id).status == "open"
    assert log_of(db, "proposal.closed")[-1]["trigger"] == "window_ended"


def test_one_vote_is_enough_without_a_quorum(db, log_signer, people):
    p = open_(db, log_signer, people["board"], now=utcnow() - timedelta(days=3, minutes=-1))
    vote(db, log_signer, people["anna"], p, 2)
    governance.close_if_due(db, p.id, signer=log_signer, now=p.closes_at)
    db.commit()
    db.refresh(p)
    assert (p.outcome, p.outcome_option_id) == ("decided", p.options[1].id)
    assert log_of(db, "proposal.closed")[-1]["quorum_met"] is True


def test_closing_is_idempotent(db, log_signer, people, make_session):
    p = open_(db, log_signer, people["board"], now=utcnow() - timedelta(days=4))
    assert governance.close_if_due(db, p.id, signer=log_signer) is not None
    db.commit()
    assert governance.close_if_due(db, p.id, signer=log_signer) is None
    assert governance.close_due(make_session, log_signer) == 0
    assert log_kinds(db).count("proposal.closed") == 1


def test_not_due_is_not_closed(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    assert governance.close_if_due(db, p.id, signer=log_signer) is None
    assert db.get(Proposal, p.id).status == "open"


def test_closer_loop_closes_due_proposals(db, log_signer, people, make_session):
    p = open_(db, log_signer, people["board"], now=utcnow() - timedelta(days=4))

    async def run_briefly():
        task = asyncio.create_task(governance.run_closer(make_session, log_signer, interval=0.01))
        for _ in range(500):
            await asyncio.sleep(0.01)
            with make_session() as s:
                if s.get(Proposal, p.id).status == "closed":
                    break
        task.cancel()

    asyncio.run(run_briefly())
    db.expire_all()
    assert db.get(Proposal, p.id).status == "closed"


# --- cancelling --------------------------------------------------------------


def test_board_cancels_with_a_reason(db, log_signer, people, received):
    p = open_(db, log_signer, people["anna"])
    governance.cancel_proposal(
        db, people["admin"], p.id, "  Duplicate of an earlier proposal ", signer=log_signer,
        channel="web",
    )  # fmt: skip
    db.commit()
    db.refresh(p)
    assert (p.status, p.ended_by, p.cancel_reason) == (
        "cancelled",
        people["admin"].id,
        "Duplicate of an earlier proposal",
    )
    entry = db.scalars(select(DecisionLog).where(DecisionLog.kind == "proposal.cancelled")).one()
    assert entry.actor_member_id == people["admin"].id
    assert entry.payload_json["reason"] == "Duplicate of an earlier proposal"
    assert received[-1].kind == "proposal.cancelled"


def test_cancel_rules(db, log_signer, people):
    p = open_(db, log_signer, people["anna"])
    with pytest.raises(NotAllowed, match="board or an admin"):
        governance.cancel_proposal(db, people["anna"], p.id, "No", signer=log_signer, channel="web")
    with pytest.raises(GovernanceError, match="reason can't be empty"):
        governance.cancel_proposal(db, people["board"], p.id, " ", signer=log_signer, channel="web")
    with pytest.raises(GovernanceError, match="at most 500"):
        governance.cancel_proposal(
            db, people["board"], p.id, "x" * 501, signer=log_signer, channel="web"
        )
    governance.cancel_proposal(db, people["board"], p.id, "Oops", signer=log_signer, channel="web")
    db.commit()
    with pytest.raises(GovernanceError, match="already cancelled"):
        governance.cancel_proposal(
            db, people["board"], p.id, "Again", signer=log_signer, channel="web"
        )


# --- tally -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "abstained", "eligible", "quorum", "threshold", "outcome", "winner"),
    [
        ({1: 0, 2: 0}, 0, 4, None, 50, "no_votes", None),
        ({1: 0, 2: 0}, 3, 4, None, 50, "no_votes", None),
        # More than 50 %: 1 of 1 passes, 2 of 4 doesn't, 3 of 5 does.
        ({1: 1, 2: 0}, 0, 9, None, 50, "decided", 1),
        ({1: 2, 2: 1, 3: 1}, 0, 4, None, 50, "below_threshold", None),
        ({1: 3, 2: 1, 3: 1}, 0, 5, None, 50, "decided", 1),
        # Abstentions are not votes cast: 1 Yes + 3 abstain passes.
        ({1: 1, 2: 0}, 3, 4, None, 50, "decided", 1),
        # Ties are never a decision, whatever the threshold.
        ({1: 2, 2: 2}, 0, 4, None, 50, "tie", None),
        ({1: 1, 2: 1, 3: 1}, 0, 3, None, 0, "tie", None),
        # Quorum, if a community sets one: abstentions count as voters.
        ({1: 2, 2: 0}, 0, 4, 50, 50, "decided", 1),
        ({1: 1, 2: 0}, 0, 4, 50, 50, "no_quorum", None),
        ({1: 1, 2: 0}, 1, 4, 50, 50, "decided", 1),
        ({1: 2, 2: 0}, 0, 5, 50, 50, "no_quorum", None),
        # Exact decimals: 2 of 3 is 66.666… %, more than 66.66 but not 66.67.
        ({1: 2, 2: 1}, 0, 3, None, Decimal("66.67"), "below_threshold", None),
        ({1: 2, 2: 1}, 0, 3, None, Decimal("66.66"), "decided", 1),
        ({1: 3, 2: 2}, 0, 5, None, 60, "below_threshold", None),
        ({1: 3, 2: 2}, 0, 5, None, Decimal("59.99"), "decided", 1),
        ({1: 4, 2: 0}, 0, 4, None, Decimal("99.99"), "decided", 1),
    ],
)
def test_tally(counts, abstained, eligible, quorum, threshold, outcome, winner):
    q = None if quorum is None else Decimal(quorum)
    result = governance.tally(counts, eligible, q, Decimal(threshold), abstained)
    assert (result.outcome, result.option_id) == (outcome, winner)
    assert result.decided is (outcome == "decided")
    assert result.cast == sum(counts.values())
    assert result.turnout == result.cast + abstained


# --- the log tells the same story as the tables -----------------------------


def test_log_records_turnout_and_counts_but_no_choices(db, log_signer, people):
    p = open_(db, log_signer, people["board"], options=("Buy", "Rent", "Wait"))
    vote(db, log_signer, people["anna"], p, 1)
    vote(db, log_signer, people["ben"], p, 3)
    vote(db, log_signer, people["anna"], p, 2)
    vote(db, log_signer, people["admin"], p, 2)
    vote(db, log_signer, people["board"], p, ABSTAIN)  # everyone voted: closes

    assert [k for k in log_kinds(db) if k.startswith(("proposal.", "vote."))] == [
        "proposal.opened",
        "vote.cast",
        "vote.cast",
        "vote.changed",
        "vote.cast",
        "vote.cast",
        "proposal.closed",
    ]
    # Secret ballot: no vote entry names an option, a label or a vote row.
    option_ids = {o.id for o in p.options}
    for kind in ("vote.cast", "vote.changed"):
        for e in log_of(db, kind):
            assert not {"option_id", "label", "vote_id", "from_label"} & set(e)
            text = json.dumps(e)
            assert not any(label in text for label in ("Buy", "Rent", "Wait"))
            assert not option_ids & {v for v in e.values() if isinstance(v, int)} - {
                e["proposal_id"], e["member_id"]
            }  # fmt: skip

    # What members can check from the log alone: turnout = members who voted, and
    # the outcome follows from the logged counts under the logged rules.
    voters = {e["member_id"] for k in ("vote.cast", "vote.changed") for e in log_of(db, k)}
    opened = log_of(db, "proposal.opened")[0]
    closed = log_of(db, "proposal.closed")[0]
    assert closed["turnout"] == len(voters) == 4
    assert closed["cast"] + closed["abstained"] == closed["turnout"]
    counts = {t["option_id"]: t["votes"] for t in closed["tally"]}
    assert counts == governance.standing(db, p).counts == {
        p.options[0].id: 0, p.options[1].id: 2, p.options[2].id: 1
    }  # fmt: skip
    recount = governance.tally(
        counts, closed["eligible"], opened["quorum_pct"],
        Decimal(str(opened["threshold_pct"])), closed["abstained"],
    )  # fmt: skip
    assert (recount.outcome, recount.option_id) == (closed["outcome"], closed["outcome_option_id"])
    db.refresh(p)
    assert (p.outcome, p.outcome_option_id) == ("decided", p.options[1].id)
    assert decision_log.verify_db(db).ok


def test_failed_step_leaves_no_log_entry(db, log_signer, people):
    p = open_(db, log_signer, people["board"])
    before = log_kinds(db)
    with pytest.raises(GovernanceError):
        governance.cast_vote(db, people["anna"], p.id, 999_999, signer=log_signer, channel="web")
    db.rollback()
    assert log_kinds(db) == before


# --- events ------------------------------------------------------------------


def test_events_go_out_after_commit_only(db, log_signer, people, received):
    governance.open_proposal(
        db, people["anna"], "Q?", ("A", "B"), vote_type_slug="rules", signer=log_signer,
        channel="web",
    )  # fmt: skip
    assert received == []
    db.rollback()
    assert received == []
    p = open_(db, log_signer, people["anna"])
    assert [e.kind for e in received] == ["proposal.opened"]
    assert received[0].data["proposal_id"] == p.id
    assert received[0].data["options"] == ["Yes", "No"]
    assert received[0].data["results"] == "live"


@pytest.mark.parametrize(
    ("opened", "days", "closes"),
    [
        ("2026-11-16 10:00:00", 1, "2026-11-17 23:59:00"),
        ("2026-11-16 10:00:00", 2, "2026-11-18 23:59:00"),
        ("2026-11-16 00:00:00", 1, "2026-11-17 23:59:00"),
        ("2026-11-16 23:59:00", 1, "2026-11-17 23:59:00"),  # exactly 24 hours
        ("2026-11-16 23:59:30", 1, "2026-11-18 23:59:00"),  # 23:59 next day is too soon
        ("2026-03-28 12:00:00", 1, "2026-03-29 23:59:00"),  # clocks change that night
    ],
)
def test_votes_close_at_the_end_of_the_day(opened, days, closes):
    start = datetime.fromisoformat(opened).replace(tzinfo=STOCKHOLM)
    end = governance.closing_time(start.astimezone(UTC), days, STOCKHOLM)
    assert end.tzinfo == UTC
    assert end.astimezone(STOCKHOLM).strftime("%Y-%m-%d %H:%M:%S") == closes
    assert end - start >= timedelta(days=days)
