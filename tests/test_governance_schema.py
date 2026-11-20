"""Proposal and vote invariants enforced by PostgreSQL itself (migration 0005)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import make_sessionmaker
from app.models import Member, Proposal, ProposalOption, Vote, utcnow


@pytest.fixture
def db(clean_db):
    with make_sessionmaker(clean_db)() as s:
        yield s


@pytest.fixture
def people(db):
    nora, bob = Member(matrix_user_id="@nora:x.test"), Member(matrix_user_id="@bob:x.test")
    db.add_all([nora, bob])
    db.commit()
    return nora, bob


def make_proposal(db, creator, labels=("Yes", "No"), commit=True) -> Proposal:
    now = utcnow()
    p = Proposal(
        question="Should we buy a second 1000 l tank?",
        why="",
        vote_type="spending",
        results="live",
        has_amount=True,
        amount=None,
        currency="SEK",
        created_by=creator.id,
        opens_at=now,
        closes_at=now + timedelta(days=7),
        quorum_pct=50,
        threshold_pct=50,
        status="open",
    )
    db.add(p)
    db.flush()
    db.add_all(
        ProposalOption(proposal_id=p.id, position=i, label=label)
        for i, label in enumerate(labels, start=1)
    )
    if commit:
        db.commit()
    return p


def cast(db, proposal, member, position=1, commit=True) -> Vote:
    v = Vote(
        proposal_id=proposal.id,
        option_id=proposal.options[position - 1].id,
        member_id=member.id,
        channel="matrix",
    )
    db.add(v)
    if commit:
        db.commit()
    return v


ENDED = {
    "closed": {"outcome": "no_votes"},
    "cancelled": {"cancel_reason": "Wrong question"},
}


def end(db, proposal, status="closed"):
    db.execute(
        update(Proposal)
        .where(Proposal.id == proposal.id)
        .values(status=status, ended_at=utcnow(), **ENDED[status])
    )
    db.commit()


def change_vote(db, old: Vote, position: int) -> Vote:
    """The supported way to change a vote: supersede, then insert."""
    db.execute(update(Vote).where(Vote.id == old.id).values(superseded_at=utcnow()))
    proposal = db.get(Proposal, old.proposal_id)
    new = Vote(
        proposal_id=proposal.id,
        option_id=proposal.options[position - 1].id,
        member_id=old.member_id,
        channel="web",
    )
    db.add(new)
    db.commit()
    return new


# --- one current vote ---------------------------------------------------------


def test_second_current_vote_is_rejected(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    cast(db, p, nora, 1)
    cast(db, p, nora, 2, commit=False)
    with pytest.raises(IntegrityError, match="uq_vote_current"):
        db.commit()


def test_changed_vote_supersedes_the_old_row(db, people):
    nora, bob = people
    p = make_proposal(db, nora)
    first = cast(db, p, nora, 1)
    cast(db, p, bob, 1)
    second = change_vote(db, first, 2)
    db.expire_all()
    rows = db.query(Vote).filter_by(proposal_id=p.id, member_id=nora.id).order_by(Vote.id).all()
    assert [r.id for r in rows] == [first.id, second.id]
    assert rows[0].superseded_at is not None
    assert rows[1].superseded_at is None
    assert rows[1].option.label == "No"


def test_members_vote_independently_per_proposal(db, people):
    nora, bob = people
    p1, p2 = make_proposal(db, nora), make_proposal(db, nora)
    for p in (p1, p2):
        cast(db, p, nora)
        cast(db, p, bob)
    assert db.query(Vote).count() == 4


# --- superseded votes can't be rewritten -------------------------------------


def test_superseded_at_cannot_be_set_twice(db, people):
    nora, _ = people
    first = cast(db, make_proposal(db, nora), nora)
    change_vote(db, first, 2)
    with pytest.raises(DBAPIError, match="superseded and can no longer change"):
        db.execute(
            update(Vote)
            .where(Vote.id == first.id)
            .values(superseded_at=utcnow() + timedelta(hours=1))
        )
    db.rollback()


def test_superseded_at_cannot_be_cleared(db, people):
    nora, _ = people
    first = cast(db, make_proposal(db, nora), nora)
    change_vote(db, first, 2)
    with pytest.raises(DBAPIError, match="superseded and can no longer change"):
        db.execute(update(Vote).where(Vote.id == first.id).values(superseded_at=None))
    db.rollback()


@pytest.mark.parametrize(
    "column",
    ["option_id", "member_id", "channel", "cast_at"],
)
def test_current_vote_cannot_be_rewritten(db, people, column):
    nora, bob = people
    p = make_proposal(db, nora)
    v = cast(db, p, nora)
    values = {
        "option_id": p.options[1].id,
        "member_id": bob.id,
        "channel": "web",
        "cast_at": v.cast_at - timedelta(minutes=1),
    }
    with pytest.raises(DBAPIError, match="never rewritten"):
        db.execute(update(Vote).where(Vote.id == v.id).values({column: values[column]}))
    db.rollback()


def test_superseded_at_cannot_predate_the_vote(db, people):
    nora, _ = people
    v = cast(db, make_proposal(db, nora), nora)
    with pytest.raises(IntegrityError, match="ck_vote_superseded"):
        db.execute(
            update(Vote)
            .where(Vote.id == v.id)
            .values(superseded_at=v.cast_at - timedelta(seconds=1))
        )
    db.rollback()


def test_votes_are_never_deleted(db, people):
    nora, _ = people
    v = cast(db, make_proposal(db, nora), nora)
    with pytest.raises(DBAPIError, match="votes are never deleted"):
        db.execute(delete(Vote).where(Vote.id == v.id))
    db.rollback()
    with pytest.raises(DBAPIError, match="vote rows are never deleted"):
        db.execute(text("TRUNCATE vote"))
    db.rollback()


# --- abstaining ------------------------------------------------------------------


def test_abstaining_is_a_vote_without_an_option(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    v = Vote(proposal_id=p.id, option_id=None, member_id=nora.id, channel="web")
    db.add(v)
    db.commit()
    assert v.option_id is None


def test_an_abstention_is_one_current_vote_too(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    db.add(Vote(proposal_id=p.id, option_id=None, member_id=nora.id, channel="web"))
    db.commit()
    db.add(Vote(proposal_id=p.id, option_id=None, member_id=nora.id, channel="web"))
    with pytest.raises(IntegrityError, match="uq_vote_current"):
        db.commit()
    db.rollback()


def test_an_abstention_cannot_be_rewritten_into_a_choice(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    v = Vote(proposal_id=p.id, option_id=None, member_id=nora.id, channel="web")
    db.add(v)
    db.commit()
    with pytest.raises(DBAPIError, match="never rewritten"):
        db.execute(update(Vote).where(Vote.id == v.id).values(option_id=p.options[0].id))
    db.rollback()
    w = cast(db, make_proposal(db, nora), nora)
    with pytest.raises(DBAPIError, match="never rewritten"):
        db.execute(update(Vote).where(Vote.id == w.id).values(option_id=None))
    db.rollback()


# --- options belong to their proposal ----------------------------------------


def test_option_from_another_proposal_is_rejected(db, people):
    nora, _ = people
    p1, p2 = make_proposal(db, nora), make_proposal(db, nora)
    db.add(Vote(proposal_id=p1.id, option_id=p2.options[0].id, member_id=nora.id, channel="web"))
    with pytest.raises(IntegrityError, match="fk_vote_option_of_proposal"):
        db.commit()


@pytest.mark.parametrize("labels", [("Only one",), tuple("ABCDEFG")])
def test_proposal_needs_two_to_six_options(db, people, labels):
    nora, _ = people
    with pytest.raises((DBAPIError, IntegrityError)):
        make_proposal(db, nora, labels=labels)
    db.rollback()


def test_proposal_without_options_is_rejected_at_commit(db, people):
    nora, _ = people
    make_proposal(db, nora, labels=(), commit=False)
    with pytest.raises(DBAPIError, match="needs 2 to 6"):
        db.commit()
    db.rollback()


def test_six_options_are_fine(db, people):
    nora, _ = people
    assert len(make_proposal(db, nora, labels=tuple("ABCDEF")).options) == 6


def test_duplicate_option_position_is_rejected(db, people):
    nora, _ = people
    p = make_proposal(db, nora, labels=("Yes", "No"), commit=False)
    db.add(ProposalOption(proposal_id=p.id, position=2, label="Maybe"))
    with pytest.raises(IntegrityError, match="uq_proposal_option_position"):
        db.commit()


def test_options_are_fixed_once_written(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    opt = p.options[0].id
    with pytest.raises(DBAPIError, match="options are fixed"):
        db.execute(update(ProposalOption).where(ProposalOption.id == opt).values(label="No!"))
    db.rollback()
    with pytest.raises(DBAPIError, match="options are fixed"):
        db.execute(delete(ProposalOption).where(ProposalOption.id == opt))
    db.rollback()


# --- who and when ------------------------------------------------------------


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_no_new_votes_once_a_proposal_is_not_open(db, people, status):
    nora, bob = people
    p = make_proposal(db, nora)
    cast(db, p, nora)
    end(db, p, status)
    with pytest.raises(DBAPIError, match="not open for votes"):
        cast(db, p, bob)
    db.rollback()


def test_deactivated_member_cannot_vote(db, people):
    nora, bob = people
    p = make_proposal(db, nora)
    bob.deactivated_at = utcnow()
    db.commit()
    with pytest.raises(DBAPIError, match="not an active member"):
        cast(db, p, bob)
    db.rollback()


def test_vote_channel_is_checked(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    db.add(Vote(proposal_id=p.id, option_id=p.options[0].id, member_id=nora.id, channel="sms"))
    with pytest.raises(IntegrityError, match="ck_vote_channel"):
        db.commit()


# --- proposals ---------------------------------------------------------------


def test_status_only_leaves_open_once(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    end(db, p)
    changes = [
        {"status": "open", "ended_at": None, "outcome": None},
        {"status": "cancelled", "outcome": None, "cancel_reason": "Oops"},
        {"outcome": "tie"},
        {"ended_at": utcnow()},
    ]
    for values in changes:
        with pytest.raises(DBAPIError, match="already closed"):
            db.execute(update(Proposal).where(Proposal.id == p.id).values(values))
        db.rollback()


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"status": "closed", "ended_at": None, "outcome": "tie"}, "ck_proposal_ended"),
        ({"status": "closed"}, "ck_proposal_outcome"),
        ({"outcome": "tie"}, "ck_proposal_outcome"),
        ({"status": "closed", "outcome": "maybe"}, "ck_proposal_outcome_kind"),
        ({"status": "closed", "outcome": "decided"}, "ck_proposal_outcome_option"),
        ({"status": "cancelled"}, "ck_proposal_cancel_reason"),
        ({"status": "cancelled", "cancel_reason": "  "}, "ck_proposal_cancel_reason"),
        ({"cancel_reason": "Why not"}, "ck_proposal_cancel_reason"),
    ],
)
def test_ending_needs_consistent_fields(db, people, values, constraint):
    nora, _ = people
    p = make_proposal(db, nora)
    values = {"ended_at": utcnow(), **values} if values.get("status") else values
    with pytest.raises(IntegrityError, match=constraint):
        db.execute(update(Proposal).where(Proposal.id == p.id).values(values))
    db.rollback()


def test_outcome_option_must_belong_to_the_proposal(db, people):
    nora, _ = people
    p1, p2 = make_proposal(db, nora), make_proposal(db, nora)
    with pytest.raises(IntegrityError, match="fk_proposal_outcome_option"):
        db.execute(
            update(Proposal)
            .where(Proposal.id == p1.id)
            .values(
                status="closed",
                ended_at=utcnow(),
                outcome="decided",
                outcome_option_id=p2.options[0].id,
            )
        )
    db.rollback()


@pytest.mark.parametrize(
    "values",
    [
        {"quorum_pct": 10},
        {"threshold_pct": 90},
        {"question": "Something else?"},
        {"why": "Because"},
        {"vote_type": "rules"},
        {"results": "after_close"},
        {"amount": 6500},
        {"currency": "EUR"},
        {"has_amount": False},
        {"closes_at": utcnow() + timedelta(days=30)},
    ],
)
def test_question_and_rules_are_fixed(db, people, values):
    nora, _ = people
    p = make_proposal(db, nora)
    with pytest.raises(DBAPIError, match="fixed once it is opened"):
        db.execute(update(Proposal).where(Proposal.id == p.id).values(values))
    db.rollback()


def test_proposals_are_never_deleted(db, people):
    nora, _ = people
    p = make_proposal(db, nora)
    with pytest.raises(DBAPIError, match="never deleted"):
        db.execute(delete(Proposal).where(Proposal.id == p.id))
    db.rollback()


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"status": "draft", "ended_at": utcnow()}, "ck_proposal_status"),
        # An option needs MORE than the threshold: 100 % could never be reached.
        ({"threshold_pct": 100}, "ck_proposal_threshold"),
        ({"threshold_pct": -1}, "ck_proposal_threshold"),
        # Never below a majority, whoever writes the row.
        ({"threshold_pct": Decimal("49.99")}, "ck_proposal_threshold"),
        ({"quorum_pct": 101}, "ck_proposal_quorum"),
        ({"question": "  "}, "ck_proposal_question"),
        ({"results": "never"}, "ck_proposal_results"),
        ({"amount": 100}, "ck_proposal_no_amount"),
        ({"currency": "SEK"}, "ck_proposal_no_amount"),
        ({"has_amount": True}, "ck_proposal_currency"),
        ({"has_amount": True, "currency": "sek"}, "ck_proposal_currency"),
        ({"has_amount": True, "currency": "SEK", "amount": -5}, "ck_proposal_amount"),
    ],
)
def test_proposal_checks(db, people, values, constraint):
    nora, _ = people
    now = utcnow()
    fields = {
        "question": "Q?",
        "why": "",
        "vote_type": "rules",
        "results": "live",
        "has_amount": False,
        "created_by": nora.id,
        "opens_at": now,
        "closes_at": now + timedelta(days=1),
        "threshold_pct": 50,
        "status": "open",
        **values,
    }
    db.add(Proposal(**fields))
    with pytest.raises(IntegrityError, match=constraint):
        db.flush()
    db.rollback()


def test_closing_window_must_be_after_opening(db, people):
    nora, _ = people
    now = utcnow()
    db.add(
        Proposal(
            question="Q?",
            why="",
            vote_type="rules",
            results="live",
            has_amount=False,
            created_by=nora.id,
            opens_at=now,
            closes_at=now,
            threshold_pct=50,
            status="open",
        )
    )
    with pytest.raises(IntegrityError, match="ck_proposal_window"):
        db.flush()
    db.rollback()


# --- migration -----------------------------------------------------------------


def _alembic(engine, fn, revision):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        getattr(command, fn)(config, revision)


def test_downgrade_refuses_to_drop_governance_history(clean_db, db, people):
    make_proposal(db, people[0])
    db.close()
    with pytest.raises(RuntimeError, match="governance history"):
        _alembic(clean_db, "downgrade", "0003_entry_void")


def test_empty_governance_schema_downgrades_and_upgrades(clean_db):
    _alembic(clean_db, "downgrade", "0003_entry_void")
    with clean_db.connect() as c:
        assert c.execute(text("SELECT to_regclass('vote')")).scalar() is None
    _alembic(clean_db, "upgrade", "head")
    with clean_db.connect() as c:
        assert c.execute(text("SELECT to_regclass('vote')")).scalar() == "vote"
