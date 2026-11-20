"""Store every shortage forecast: its data basis, parameters and result.

Revision ID: 0015_forecast_run
Revises: 0014_merge_bookings_and_votes
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_forecast_run"
down_revision = "0014_merge_bookings_and_votes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "category_id", sa.Integer(), sa.ForeignKey("resource_category.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("entries", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("method", sa.String(length=20), nullable=True),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("inputs", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("shortage_on", sa.Date(), nullable=True),
        sa.Column("flagged", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'insufficient_data')", name="ck_forecast_run_status"
        ),
        sa.CheckConstraint("NOT flagged OR shortage_on IS NOT NULL", name="ck_forecast_run_flag"),
    )
    op.create_index(
        "ix_forecast_run_category", "forecast_run", ["category_id", sa.text("id DESC")]
    )


def downgrade() -> None:
    op.drop_index("ix_forecast_run_category", table_name="forecast_run")
    op.drop_table("forecast_run")
