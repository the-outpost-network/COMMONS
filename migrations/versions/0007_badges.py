"""Add participation badges: badge rules in their own table, and who earned which.

Badge rules used to be one JSON value in `config_kv` (key `badges`), seeded
from community.yaml. This moves them into `badge_rule`, keeping their order,
and removes the old key, so there is one place they live. It is a change of
storage, not of configuration: the rules were logged as `config.changed` when
they were seeded, and they are the same rules afterwards.

Revision ID: 0007_badges
Revises: 0006_calendar_tokens
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_badges"
down_revision = "0006_calendar_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "badge_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("rule_json", postgresql.JSONB(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "member_badge",
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("badge_rule_id", sa.Integer(), nullable=False),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.ForeignKeyConstraint(["badge_rule_id"], ["badge_rule.id"]),
        sa.PrimaryKeyConstraint("member_id", "badge_rule_id"),
    )

    conn = op.get_bind()
    old = conn.execute(sa.text("SELECT value_json FROM config_kv WHERE key = 'badges'")).scalar()
    for position, badge in enumerate(old or []):
        conn.execute(
            sa.text(
                "INSERT INTO badge_rule (slug, name, description, rule_json, position, created_at)"
                " VALUES (:slug, :name, :description, CAST(:rule AS jsonb), :position, now())"
            ),
            {
                "slug": badge["slug"],
                "name": badge["name"],
                "description": badge.get("description") or "",
                "rule": json.dumps(badge["rule"]),
                "position": position,
            },
        )
    conn.execute(sa.text("DELETE FROM config_kv WHERE key = 'badges'"))


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT slug, name, description, rule_json FROM badge_rule"
            " WHERE archived_at IS NULL ORDER BY position, id"
        )
    ).all()
    if rows:
        value = [
            {"slug": r.slug, "name": r.name, "description": r.description, "rule": r.rule_json}
            for r in rows
        ]
        conn.execute(
            sa.text(
                "INSERT INTO config_kv (key, value_json, updated_at)"
                " VALUES ('badges', CAST(:v AS jsonb), now())"
            ),
            {"v": json.dumps(value)},
        )
    op.drop_table("member_badge")
    op.drop_table("badge_rule")
