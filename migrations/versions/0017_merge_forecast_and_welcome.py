"""Join the forecast line with the vote messages and welcome line of migrations.

Both start from 0014_merge_bookings_and_votes and touch different tables, so nothing
changes here.

Revision ID: 0017_merge_forecast_and_welcome
Revises: 0015_forecast_run, 0016_member_welcome
"""

from __future__ import annotations

revision = "0017_merge_forecast_and_welcome"
down_revision = ("0015_forecast_run", "0016_member_welcome")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
