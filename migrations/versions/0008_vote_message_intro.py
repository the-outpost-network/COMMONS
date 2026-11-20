"""Keep the local model's checked intro line for each vote message.

NULL: not written yet. Empty: the model was unavailable or its line was refused,
so the message uses the fixed sentence.

Revision ID: 0008_vote_message_intro
Revises: 0007_reminder_lead_hours
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_vote_message_intro"
down_revision = "0007_reminder_lead_hours"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_notification", sa.Column("intro", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_notification", "intro")
