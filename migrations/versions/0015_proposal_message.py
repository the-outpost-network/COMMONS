"""Remember the Matrix messages that show a vote: the room announcement and each
member's direct message, so results can reply to the announcement and a tap on
1️⃣ in a direct chat maps to that vote and that member.

Revision ID: 0015_proposal_message
Revises: 0014_merge_bookings_and_votes
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015_proposal_message"
down_revision = "0014_merge_bookings_and_votes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposal_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id"), nullable=True),
        sa.Column("room_id", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('room', 'direct')", name="ck_proposal_message_kind"),
        sa.CheckConstraint(
            "(kind = 'direct') = (member_id IS NOT NULL)", name="ck_proposal_message_member"
        ),
        sa.UniqueConstraint("event_id", name="uq_proposal_message_event"),
    )
    op.create_index("ix_proposal_message_proposal", "proposal_message", ["proposal_id"])


def downgrade() -> None:
    op.drop_index("ix_proposal_message_proposal", table_name="proposal_message")
    op.drop_table("proposal_message")
