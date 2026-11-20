"""Reminders go out at set times on the closing day, not hours before the close.

`lead_hours` becomes `reminder_minute`: minutes after local midnight (540 = 09:00).
Rows from the earlier scheme map to the reminder that replaces them.

Revision ID: 0009_reminder_time_of_day
Revises: 0008_vote_message_intro
"""

from __future__ import annotations

from alembic import op

revision = "0009_reminder_time_of_day"
down_revision = "0008_vote_message_intro"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("chat_notification", "lead_hours", new_column_name="reminder_minute")
    op.execute(
        "UPDATE chat_notification SET reminder_minute = "
        "CASE WHEN reminder_minute = 24 THEN 540 ELSE 1080 END WHERE kind = 'reminder'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE chat_notification SET reminder_minute = "
        "CASE WHEN reminder_minute = 540 THEN 24 ELSE 2 END WHERE kind = 'reminder'"
    )
    op.alter_column("chat_notification", "reminder_minute", new_column_name="lead_hours")
