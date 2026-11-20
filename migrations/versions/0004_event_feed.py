"""Add the event feed: one short line per event for the COMMONS room.

Revision ID: 0004_event_feed
Revises: 0003_entry_void
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_event_feed"
down_revision = "0003_entry_void"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_feed",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_feed_created_at", "event_feed", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_event_feed_created_at", table_name="event_feed")
    op.drop_table("event_feed")
