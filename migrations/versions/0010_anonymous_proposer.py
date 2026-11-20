"""Let a member propose a vote anonymously; the choice is fixed once the vote opens.

`created_by` still records the proposer in the database. Member-facing pages, chat
messages and the decision log entry don't name them.

Revision ID: 0010_anonymous_proposer
Revises: 0009_reminder_time_of_day
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_anonymous_proposer"
down_revision = "0009_reminder_time_of_day"
branch_labels = None
depends_on = None

GUARD = """
CREATE OR REPLACE FUNCTION proposal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
     OR NEW.anonymous <> OLD.anonymous
     OR NEW.threshold_pct <> OLD.threshold_pct THEN
    RAISE EXCEPTION 'a proposal''s question and rules are fixed once it is opened';
  END IF;
  IF OLD.status <> 'open' THEN
    RAISE EXCEPTION 'proposal % is already %', OLD.id, OLD.status;
  END IF;
  RETURN NEW;
END $$
"""

OLD_GUARD = """
CREATE OR REPLACE FUNCTION proposal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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


def upgrade() -> None:
    op.add_column(
        "proposal",
        sa.Column("anonymous", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute(GUARD)


def downgrade() -> None:
    op.execute(OLD_GUARD)
    op.drop_column("proposal", "anonymous")
