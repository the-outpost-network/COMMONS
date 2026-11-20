"""Add the outbox of vote messages the Matrix bot sends to members.

One row per proposal and kind (opened, reminder, closed, cancelled), so a
message can't be queued twice. It is written in the same transaction as the
change it announces and marked delivered by the bot.

Revision ID: 0006_chat_notification
Revises: 0005_governance
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_chat_notification"
down_revision = "0005_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_notification",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('opened', 'reminder', 'closed', 'cancelled')",
            name="ck_chat_notification_kind",
        ),
        sa.UniqueConstraint("proposal_id", "kind", name="uq_chat_notification_proposal_kind"),
    )
    op.create_index(
        "ix_chat_notification_pending",
        "chat_notification",
        ["id"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_notification_pending", table_name="chat_notification")
    op.drop_table("chat_notification")
