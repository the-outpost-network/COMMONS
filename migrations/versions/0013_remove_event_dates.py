"""Remove the dates on space and event votes again; the community decided against them.

Revision ID: 0013_remove_event_dates
Revises: 0012_event_dates
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013_remove_event_dates"
down_revision = "0012_event_dates"
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
     OR NEW.anonymous <> OLD.anonymous
     OR NEW.event_starts_on IS DISTINCT FROM OLD.event_starts_on
     OR NEW.event_ends_on IS DISTINCT FROM OLD.event_ends_on
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
    op.execute(GUARD)
    op.drop_constraint("ck_proposal_event_dates", "proposal", type_="check")
    op.drop_column("proposal", "event_ends_on")
    op.drop_column("proposal", "event_starts_on")


def downgrade() -> None:
    op.add_column("proposal", sa.Column("event_starts_on", sa.Date(), nullable=True))
    op.add_column("proposal", sa.Column("event_ends_on", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_proposal_event_dates",
        "proposal",
        "(event_starts_on IS NULL AND event_ends_on IS NULL) "
        "OR (event_starts_on IS NOT NULL AND event_ends_on >= event_starts_on)",
    )
    op.execute(OLD_GUARD)
