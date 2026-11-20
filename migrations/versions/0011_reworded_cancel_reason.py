"""Keep the local model's clearer wording of a cancellation reason for the chat message.

The member's own words stay in `proposal.cancel_reason` and the decision log. Empty
or NULL here means the message shows those words.

Revision ID: 0011_reworded_cancel_reason
Revises: 0010_anonymous_proposer
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_reworded_cancel_reason"
down_revision = "0010_anonymous_proposer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_notification", sa.Column("reworded_reason", sa.String(length=200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("chat_notification", "reworded_reason")
