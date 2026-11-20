"""Allow several reminders per vote: one per number of hours before the close.

Revision ID: 0007_reminder_lead_hours
Revises: 0006_chat_notification
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_reminder_lead_hours"
down_revision = "0006_chat_notification"
branch_labels = None
depends_on = None

CONSTRAINT = "uq_chat_notification_proposal_kind"


def upgrade() -> None:
    op.add_column(
        "chat_notification",
        sa.Column("lead_hours", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute("UPDATE chat_notification SET lead_hours = 2 WHERE kind = 'reminder'")
    op.drop_constraint(CONSTRAINT, "chat_notification", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT, "chat_notification", ["proposal_id", "kind", "lead_hours"]
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM chat_notification a USING chat_notification b "
        "WHERE a.kind = 'reminder' AND b.kind = 'reminder' "
        "AND a.proposal_id = b.proposal_id AND a.id < b.id"
    )
    op.drop_constraint(CONSTRAINT, "chat_notification", type_="unique")
    op.create_unique_constraint(CONSTRAINT, "chat_notification", ["proposal_id", "kind"])
    op.drop_column("chat_notification", "lead_hours")
