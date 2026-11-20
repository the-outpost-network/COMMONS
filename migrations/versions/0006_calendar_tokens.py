"""Add phone-calendar links: one secret ICS token per member, stored as a hash.

Revision ID: 0006_calendar_tokens
Revises: 0005_bookings
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_calendar_tokens"
down_revision = "0005_bookings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_token",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_calendar_token_member_id", "calendar_token", ["member_id"])
    # One active link per member.
    op.create_index(
        "uq_calendar_token_active",
        "calendar_token",
        ["member_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("calendar_token")
