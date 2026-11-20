"""Join the bookings and badges line with the votes line of migrations.

Both start from 0003_entry_void and touch different tables, so nothing changes here.

Revision ID: 0014_merge_bookings_and_votes
Revises: 0013_remove_event_dates, 0007_badges
"""

from __future__ import annotations

revision = "0014_merge_bookings_and_votes"
down_revision = ("0013_remove_event_dates", "0007_badges")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
