"""Add bookable things (spaces, vehicles, tools) and overlap-safe bookings.

Revision ID: 0005_bookings
Revises: 0004_event_feed

A bookable thing
has a name, a type (space, vehicle or tool), where it is, and a description. A
booking has a start, an optional end that is only a hint, a note, and for
vehicles where the member is going.

`blocks_until` is the end the overlap check uses: the end, or for a booking
without one its start plus the community's open-booking window; a return moves
it to the return time if that is earlier. The exclusion constraint
`ex_booking_overlap` is the one rule that keeps two members from holding the
same thing at the same time; it needs `btree_gist` (a trusted extension, so the
database owner may create it).

Bookings are history: a trigger refuses DELETE and TRUNCATE, keeps who booked
what fixed, lets the member or an admin change the times, the note and the
destination until the booking is given back or cancelled, and lets the return
and cancel fields be set once.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_bookings"
down_revision = "0004_event_feed"
branch_labels = None
depends_on = None

OVERLAP = """
ALTER TABLE booking ADD CONSTRAINT ex_booking_overlap
  EXCLUDE USING gist (bookable_id WITH =, tstzrange(starts_at, blocks_until, '[)') WITH &&)
  WHERE (cancelled_at IS NULL)
"""

GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION booking_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'bookings are never deleted; cancel instead';
  END IF;
  IF NEW.id <> OLD.id OR NEW.bookable_id <> OLD.bookable_id
     OR NEW.member_id <> OLD.member_id OR NEW.created_at <> OLD.created_at
     OR NEW.channel <> OLD.channel THEN
    RAISE EXCEPTION 'who booked what can''t change; cancel and book again';
  END IF;
  IF OLD.returned_at IS NOT NULL AND (
       NEW.returned_at IS DISTINCT FROM OLD.returned_at
       OR NEW.starts_at <> OLD.starts_at
       OR NEW.ends_at IS DISTINCT FROM OLD.ends_at
       OR NEW.blocks_until <> OLD.blocks_until) THEN
    RAISE EXCEPTION 'booking % was already given back', OLD.id;
  END IF;
  IF OLD.cancelled_at IS NOT NULL AND (
       NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at
       OR NEW.cancelled_by IS DISTINCT FROM OLD.cancelled_by
       OR NEW.cancel_reason IS DISTINCT FROM OLD.cancel_reason
       OR NEW.starts_at <> OLD.starts_at
       OR NEW.ends_at IS DISTINCT FROM OLD.ends_at
       OR NEW.blocks_until <> OLD.blocks_until) THEN
    RAISE EXCEPTION 'booking % was already cancelled', OLD.id;
  END IF;
  RETURN NEW;
END $$
"""
GUARD_TRIGGER = """
CREATE TRIGGER booking_guard BEFORE UPDATE OR DELETE ON booking
  FOR EACH ROW EXECUTE FUNCTION booking_guard()
"""
NO_TRUNCATE = """
CREATE TRIGGER booking_no_truncate BEFORE TRUNCATE ON booking
  FOR EACH STATEMENT EXECUTE FUNCTION booking_guard()
"""


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "bookable",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("location", sa.String(length=100), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        _ts("created_at"),
        _ts("archived_at", nullable=True),
        sa.CheckConstraint("type IN ('space', 'vehicle', 'tool')", name="ck_bookable_type"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "booking",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bookable_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        _ts("starts_at"),
        _ts("ends_at", nullable=True),
        _ts("blocks_until"),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("destination", sa.String(length=100), nullable=True),
        sa.Column("channel", sa.String(length=10), nullable=False),
        _ts("created_at"),
        _ts("updated_at", nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        _ts("returned_at", nullable=True),
        _ts("cancelled_at", nullable=True),
        sa.Column("cancelled_by", sa.Integer(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=200), nullable=True),
        sa.CheckConstraint("channel IN ('web', 'matrix')", name="ck_booking_channel"),
        sa.CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="ck_booking_times"),
        sa.CheckConstraint(
            "blocks_until > starts_at AND (ends_at IS NULL OR blocks_until = ends_at "
            "OR returned_at IS NOT NULL)",
            name="ck_booking_blocks",
        ),
        sa.CheckConstraint(
            "(cancelled_at IS NULL) = (cancelled_by IS NULL)", name="ck_booking_cancelled_by"
        ),
        sa.CheckConstraint(
            "cancelled_at IS NULL OR returned_at IS NULL", name="ck_booking_cancel_or_return"
        ),
        sa.ForeignKeyConstraint(["bookable_id"], ["bookable.id"]),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["member.id"], name="fk_booking_updated_by"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_booking_bookable_id", "booking", ["bookable_id"])
    op.create_index("ix_booking_member_id", "booking", ["member_id"])
    op.create_index("ix_booking_starts_at", "booking", ["starts_at"])
    op.execute(OVERLAP)
    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)
    op.execute(NO_TRUNCATE)


def downgrade() -> None:
    # Bookings are community history; this refuses rather than dropping them.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM booking)")).scalar_one():
        raise RuntimeError("Refusing to downgrade: booking holds booking history")
    op.execute("DROP TRIGGER IF EXISTS booking_no_truncate ON booking")
    op.execute("DROP TRIGGER IF EXISTS booking_guard ON booking")
    op.execute("DROP FUNCTION IF EXISTS booking_guard()")
    for table in ("booking", "bookable"):
        op.drop_table(table)
