"""Welcome each new member in a private chat with the bot, through the same outbox
as the vote messages: a `welcome` row names the member instead of a vote.

Revision ID: 0016_member_welcome
Revises: 0015_proposal_message
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_member_welcome"
down_revision = "0015_proposal_message"
branch_labels = None
depends_on = None

KINDS = "kind IN ('opened', 'reminder', 'closed', 'cancelled', 'welcome')"
OLD_KINDS = "kind IN ('opened', 'reminder', 'closed', 'cancelled')"


def upgrade() -> None:
    op.add_column(
        "chat_notification",
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id"), nullable=True),
    )
    op.alter_column("chat_notification", "proposal_id", nullable=True)
    op.drop_constraint("ck_chat_notification_kind", "chat_notification", type_="check")
    op.create_check_constraint("ck_chat_notification_kind", "chat_notification", KINDS)
    op.create_check_constraint(
        "ck_chat_notification_subject",
        "chat_notification",
        "(kind = 'welcome') = (member_id IS NOT NULL AND proposal_id IS NULL)",
    )
    op.create_index(
        "uq_chat_notification_welcome",
        "chat_notification",
        ["member_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'welcome'"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM chat_notification WHERE kind = 'welcome'")
    op.drop_index("uq_chat_notification_welcome", table_name="chat_notification")
    op.drop_constraint("ck_chat_notification_subject", "chat_notification", type_="check")
    op.drop_constraint("ck_chat_notification_kind", "chat_notification", type_="check")
    op.create_check_constraint("ck_chat_notification_kind", "chat_notification", OLD_KINDS)
    op.alter_column("chat_notification", "proposal_id", nullable=False)
    op.drop_column("chat_notification", "member_id")
