"""Add proposals, their options and votes, with the vote invariants in the database.

- One current vote per (proposal, member): partial unique index WHERE
  superseded_at IS NULL. Changing a vote sets superseded_at on the old row
  and inserts a new one, in that order, in one transaction.
- A vote is never deleted or rewritten. The only change the database allows is
  setting superseded_at once, from NULL to a time no earlier than cast_at.
- A vote's option belongs to the vote's proposal: composite foreign key
  (proposal_id, option_id) -> proposal_option(proposal_id, id). A NULL option
  is an abstention: it counts for turnout, not as a vote cast.
- Ballots are secret: the decision log records who voted, never the choice; only
  this table links a member to an option (needed for one changeable vote each).
- A new vote needs an open proposal and an active member.
- A proposal has 2-6 options (positions 1-6, checked at commit); options are
  fixed once written. Proposals are never deleted; their question, type, amount
  and rule set (quorum, threshold, result visibility, copied from config at
  opening; the proposer may set the threshold, from 50 to under 100) never change, and status
  only moves from open to closed (with an outcome) or cancelled (with a
  reason), once; after that the row is frozen.

Revision ID: 0005_governance
Revises: 0003_entry_void
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_governance"
down_revision = "0003_entry_void"
branch_labels = None
depends_on = None

TABLES = ("proposal", "proposal_option", "vote")

PROPOSAL_GUARD = """
CREATE FUNCTION proposal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'proposals are never deleted; cancel them';
  END IF;
  IF NEW.id <> OLD.id OR NEW.question <> OLD.question OR NEW.why <> OLD.why
     OR NEW.vote_type <> OLD.vote_type OR NEW.results <> OLD.results
     OR NEW.has_amount <> OLD.has_amount OR NEW.amount IS DISTINCT FROM OLD.amount
     OR NEW.currency IS DISTINCT FROM OLD.currency
     OR NEW.created_by <> OLD.created_by OR NEW.created_at <> OLD.created_at
     OR NEW.opens_at <> OLD.opens_at OR NEW.closes_at <> OLD.closes_at
     OR NEW.quorum_pct IS DISTINCT FROM OLD.quorum_pct
     OR NEW.threshold_pct <> OLD.threshold_pct THEN
    RAISE EXCEPTION 'a proposal''s question and rules are fixed once it is opened';
  END IF;
  IF OLD.status <> 'open' THEN
    RAISE EXCEPTION 'proposal % is already %', OLD.id, OLD.status;
  END IF;
  RETURN NEW;
END $$
"""

OPTION_GUARD = """
CREATE FUNCTION proposal_option_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'proposal options are fixed once written';
END $$
"""

NO_TRUNCATE = """
CREATE FUNCTION governance_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% rows are never deleted', TG_TABLE_NAME;
END $$
"""

# Deferred to commit, so a proposal and its options go in one transaction.
OPTION_COUNT = """
CREATE FUNCTION proposal_option_count() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  SELECT count(*) INTO n FROM proposal_option WHERE proposal_id = NEW.id;
  IF n < 2 OR n > 6 THEN
    RAISE EXCEPTION 'proposal % has % options; it needs 2 to 6', NEW.id, n;
  END IF;
  RETURN NULL;
END $$
"""

VOTE_GUARD = """
CREATE FUNCTION vote_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'votes are never deleted; a changed vote supersedes the old one';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NOT EXISTS (SELECT 1 FROM proposal WHERE id = NEW.proposal_id AND status = 'open') THEN
      RAISE EXCEPTION 'proposal % is not open for votes', NEW.proposal_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM member WHERE id = NEW.member_id AND deactivated_at IS NULL) THEN
      RAISE EXCEPTION 'member % is not an active member', NEW.member_id;
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.superseded_at IS NOT NULL THEN
    RAISE EXCEPTION 'vote % is superseded and can no longer change', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.proposal_id <> OLD.proposal_id
     OR NEW.option_id IS DISTINCT FROM OLD.option_id OR NEW.member_id <> OLD.member_id
     OR NEW.channel <> OLD.channel OR NEW.cast_at <> OLD.cast_at THEN
    RAISE EXCEPTION 'a vote is never rewritten; cast a new one';
  END IF;
  RETURN NEW;
END $$
"""

TRIGGERS = (
    "CREATE TRIGGER proposal_guard BEFORE UPDATE OR DELETE ON proposal "
    "FOR EACH ROW EXECUTE FUNCTION proposal_guard()",
    "CREATE TRIGGER proposal_option_guard BEFORE UPDATE OR DELETE ON proposal_option "
    "FOR EACH STATEMENT EXECUTE FUNCTION proposal_option_guard()",
    "CREATE CONSTRAINT TRIGGER proposal_option_count AFTER INSERT ON proposal "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION proposal_option_count()",
    "CREATE TRIGGER vote_guard BEFORE INSERT OR UPDATE OR DELETE ON vote "
    "FOR EACH ROW EXECUTE FUNCTION vote_guard()",
    *(
        f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
        "FOR EACH STATEMENT EXECUTE FUNCTION governance_no_truncate()"
        for t in TABLES
    ),
)


def upgrade() -> None:
    op.create_table(
        "proposal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("question", sa.String(length=140), nullable=False),
        sa.Column("why", sa.String(length=1000), nullable=False),
        sa.Column("vote_type", sa.String(length=40), nullable=False),
        sa.Column("results", sa.String(length=12), nullable=False),
        sa.Column("has_amount", sa.Boolean(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quorum_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("threshold_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("outcome_option_id", sa.Integer(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_proposal_status"),
        sa.CheckConstraint("closes_at > opens_at", name="ck_proposal_window"),
        sa.CheckConstraint(
            "quorum_pct IS NULL OR quorum_pct BETWEEN 0 AND 100", name="ck_proposal_quorum"
        ),
        sa.CheckConstraint(
            "threshold_pct >= 50 AND threshold_pct < 100", name="ck_proposal_threshold"
        ),
        sa.CheckConstraint("results IN ('live', 'after_close')", name="ck_proposal_results"),
        sa.CheckConstraint(
            "has_amount OR (amount IS NULL AND currency IS NULL)", name="ck_proposal_no_amount"
        ),
        sa.CheckConstraint(
            "NOT has_amount OR coalesce(currency ~ '^[A-Z]{3}$', false)",
            name="ck_proposal_currency",
        ),
        sa.CheckConstraint("amount IS NULL OR amount >= 0", name="ck_proposal_amount"),
        sa.CheckConstraint("(status = 'open') = (ended_at IS NULL)", name="ck_proposal_ended"),
        sa.CheckConstraint(
            "(status = 'closed') = (outcome IS NOT NULL)", name="ck_proposal_outcome"
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('decided', 'no_votes', 'no_quorum', 'tie', 'below_threshold')",
            name="ck_proposal_outcome_kind",
        ),
        sa.CheckConstraint(
            "coalesce(outcome = 'decided', false) = (outcome_option_id IS NOT NULL)",
            name="ck_proposal_outcome_option",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled') = (coalesce(length(btrim(cancel_reason)), 0) > 0)",
            name="ck_proposal_cancel_reason",
        ),
        sa.CheckConstraint("length(btrim(question)) > 0", name="ck_proposal_question"),
        sa.ForeignKeyConstraint(["created_by"], ["member.id"]),
        sa.ForeignKeyConstraint(["ended_by"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_proposal_status", "proposal", ["status"])
    op.create_table(
        "proposal_option",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.CheckConstraint("position BETWEEN 1 AND 6", name="ck_proposal_option_position"),
        sa.CheckConstraint("length(btrim(label)) > 0", name="ck_proposal_option_label"),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposal.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", "position", name="uq_proposal_option_position"),
        # Target of vote's composite foreign key.
        sa.UniqueConstraint("proposal_id", "id", name="uq_proposal_option_proposal_id"),
    )
    # The outcome is one of the proposal's own options (added once both tables exist).
    op.create_foreign_key(
        "fk_proposal_outcome_option",
        "proposal",
        "proposal_option",
        ["id", "outcome_option_id"],
        ["proposal_id", "id"],
    )
    op.create_table(
        "vote",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        # NULL = abstained.
        sa.Column("option_id", sa.Integer(), nullable=True),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("cast_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("channel IN ('web', 'matrix')", name="ck_vote_channel"),
        sa.CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= cast_at", name="ck_vote_superseded"
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposal.id"]),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.ForeignKeyConstraint(
            ["proposal_id", "option_id"],
            ["proposal_option.proposal_id", "proposal_option.id"],
            name="fk_vote_option_of_proposal",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_vote_current",
        "vote",
        ["proposal_id", "member_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index("ix_vote_member_id", "vote", ["member_id"])
    for sql in (PROPOSAL_GUARD, OPTION_GUARD, NO_TRUNCATE, OPTION_COUNT, VOTE_GUARD, *TRIGGERS):
        op.execute(sql)


def downgrade() -> None:
    # Proposals and votes are community history; refuse rather than drop them.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM proposal)")).scalar_one():
        raise RuntimeError("Refusing to downgrade: proposal holds governance history")
    for table in TABLES:
        op.execute(f"DROP TABLE {table} CASCADE")
    for fn in (
        "vote_guard",
        "governance_no_truncate",
        "proposal_option_count",
        "proposal_option_guard",
        "proposal_guard",
    ):
        op.execute(f"DROP FUNCTION {fn}()")
