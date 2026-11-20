"""Let a correction say "this entry didn't happen" (undo from chat).

A use or add could already be corrected to 0, but a reading of 0 is a real
level (an empty tank), so it needs its own marker: `void` on a correction row.
The newest correction still wins, so a later correction with a value brings
the entry back.

The edit guard now also refuses changing `void`, so an undo can't be turned
into something else after the fact.

Revision ID: 0003_entry_void
Revises: 0002_resources
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_entry_void"
down_revision = "0002_resources"
branch_labels = None
depends_on = None

# Frozen copy of the edit window (models.EDIT_WINDOW_MINUTES at the time of writing).
GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION resource_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'resource entries are never deleted; add a correction';
  END IF;
  IF OLD.created_at < now() - interval '15 minutes' THEN
    RAISE EXCEPTION 'resource entry % is past its edit window; add a correction', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.category_id <> OLD.category_id
     OR NEW.member_id <> OLD.member_id OR NEW.entry_type <> OLD.entry_type
     OR NEW.created_at <> OLD.created_at
     OR NEW.corrects_id IS DISTINCT FROM OLD.corrects_id
     OR NEW.void IS DISTINCT FROM OLD.void THEN
    RAISE EXCEPTION 'only amount, note and time of an entry can be edited';
  END IF;
  RETURN NEW;
END $$
"""

# The 0002 guard, restored on downgrade.
OLD_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION resource_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'resource entries are never deleted; add a correction';
  END IF;
  IF OLD.created_at < now() - interval '15 minutes' THEN
    RAISE EXCEPTION 'resource entry % is past its edit window; add a correction', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.category_id <> OLD.category_id
     OR NEW.member_id <> OLD.member_id OR NEW.entry_type <> OLD.entry_type
     OR NEW.created_at <> OLD.created_at
     OR NEW.corrects_id IS DISTINCT FROM OLD.corrects_id THEN
    RAISE EXCEPTION 'only amount, note and time of an entry can be edited';
  END IF;
  RETURN NEW;
END $$
"""


def upgrade() -> None:
    # A plain ADD COLUMN with a constant default doesn't rewrite rows in
    # PostgreSQL 11+, so the guard trigger isn't involved.
    op.add_column(
        "resource_entry",
        sa.Column("void", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_resource_entry_void", "resource_entry", "NOT void OR entry_type = 'correction'"
    )
    op.execute(GUARD_FUNCTION)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM resource_entry WHERE void)")
    ).scalar_one():
        raise RuntimeError("Refusing to downgrade: resource_entry holds undone entries")
    op.execute(OLD_GUARD_FUNCTION)
    op.drop_constraint("ck_resource_entry_void", "resource_entry", type_="check")
    op.drop_column("resource_entry", "void")
