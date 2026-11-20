"""Add invites, resource categories, aliases and the resource ledger.

Adopts the same tables when the pre-Alembic app already created them with
Base.metadata.create_all (a box that ran main before migrations existed), so
real ledger data is kept.

Revision ID: 0002_resources
Revises: 0001_postgresql_core
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_resources"
down_revision = "0001_postgresql_core"
branch_labels = None
depends_on = None

TABLES = ("invite", "resource_category", "resource_alias", "resource_entry")
REQUIRED_COLUMNS = {
    "invite": {
        "id", "code_hash", "mas_token_id", "note", "created_by", "created_at", "expires_at",
        "revoked_at",
    },
    "resource_category": {
        "id", "slug", "name", "unit", "kind", "shortage_threshold", "capacity",
        "forecast_horizon_days", "position", "created_at", "archived_at",
    },
    "resource_alias": {"alias", "category_id"},
    "resource_entry": {
        "id", "category_id", "member_id", "channel", "entry_type", "amount", "note",
        "occurred_at", "created_at", "edited_at", "corrects_id",
    },
}

# Frozen copy of the edit window (models.EDIT_WINDOW_MINUTES at the time of writing).
GUARD_FUNCTION = """
CREATE FUNCTION resource_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
GUARD_TRIGGER = """
CREATE TRIGGER resource_entry_guard BEFORE UPDATE OR DELETE ON resource_entry
  FOR EACH ROW EXECUTE FUNCTION resource_entry_guard()
"""


def _create_tables() -> None:
    op.create_table(
        "invite",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("mas_token_id", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_table(
        "resource_category",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("shortage_threshold", sa.Float(), nullable=True),
        sa.Column("capacity", sa.Float(), nullable=True),
        sa.Column("forecast_horizon_days", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('stock', 'flow')", name="ck_resource_category_kind"),
        sa.CheckConstraint(
            "shortage_threshold IS NULL OR shortage_threshold >= 0",
            name="ck_resource_category_threshold",
        ),
        sa.CheckConstraint(
            "capacity IS NULL OR capacity > 0", name="ck_resource_category_capacity"
        ),
        sa.CheckConstraint(
            "forecast_horizon_days BETWEEN 1 AND 365", name="ck_resource_category_horizon"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "resource_alias",
        sa.Column("alias", sa.String(length=40), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["resource_category.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("alias"),
    )
    op.create_index("ix_resource_alias_category_id", "resource_alias", ["category_id"])
    op.create_table(
        "resource_entry",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("entry_type", sa.String(length=12), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("note", sa.String(length=140), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("corrects_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "entry_type IN ('reading', 'use', 'add', 'correction')",
            name="ck_resource_entry_type",
        ),
        sa.CheckConstraint(
            "channel IN ('web', 'matrix', 'sensor')", name="ck_resource_entry_channel"
        ),
        sa.CheckConstraint("amount >= 0", name="ck_resource_entry_amount"),
        sa.CheckConstraint(
            "(entry_type = 'correction') = (corrects_id IS NOT NULL)",
            name="ck_resource_entry_corrects",
        ),
        sa.ForeignKeyConstraint(["category_id"], ["resource_category.id"]),
        sa.ForeignKeyConstraint(["corrects_id"], ["resource_entry.id"]),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("category_id", "member_id", "occurred_at", "corrects_id"):
        op.create_index(f"ix_resource_entry_{column}", "resource_entry", [column])
    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)


def _check_adoptable(bind) -> None:
    inspector = sa.inspect(bind)
    for table in TABLES:
        missing = REQUIRED_COLUMNS[table] - {c["name"] for c in inspector.get_columns(table)}
        if missing:
            raise RuntimeError(
                f"Cannot adopt existing {table} table; missing columns: {', '.join(sorted(missing))}"
            )
    triggers = bind.execute(
        sa.text(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'resource_entry'::regclass "
            "AND NOT tgisinternal"
        )
    ).scalars()
    if "resource_entry_guard" not in set(triggers):
        raise RuntimeError("Cannot adopt resource_entry: the resource_entry_guard trigger is missing")


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(TABLES) & set(sa.inspect(bind).get_table_names())
    if not existing:
        _create_tables()
    elif existing == set(TABLES):
        _check_adoptable(bind)
    else:
        raise RuntimeError(
            "Cannot adopt a partial resource schema; missing tables: "
            + ", ".join(sorted(set(TABLES) - existing))
        )


def downgrade() -> None:
    # Ledger entries are community history; this refuses rather than dropping them.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM resource_entry)")).scalar_one():
        raise RuntimeError("Refusing to downgrade: resource_entry holds ledger history")
    op.execute("DROP TRIGGER IF EXISTS resource_entry_guard ON resource_entry")
    op.execute("DROP FUNCTION IF EXISTS resource_entry_guard()")
    for table in reversed(TABLES):
        op.drop_table(table)
