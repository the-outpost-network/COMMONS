"""Create the PostgreSQL core schema and adopt existing identity tables.

Revision ID: 0001_postgresql_core
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_postgresql_core"
down_revision = None
branch_labels = None
depends_on = None

CORE_TABLES = ("member", "member_role", "session", "auth_event")
ALL_TABLES = (
    "member",
    "member_role",
    "session",
    "auth_event",
    "config_kv",
    "signing_key",
    "decision_log",
)
STATE_TABLE = "_commons_migration_0001_state"
REQUIRED_COLUMNS = {
    "member": {"id", "matrix_user_id", "display_name", "created_at", "deactivated_at"},
    "member_role": {"member_id", "role", "granted_at", "granted_by"},
    "session": {"id_hash", "member_id", "created_at", "last_seen_at", "expires_at", "revoked_at"},
    "auth_event": {"id", "ts", "member_id", "kind", "channel"},
}


def _create_member() -> None:
    op.create_table(
        "member",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("matrix_user_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matrix_user_id"),
    )


def _create_member_role() -> None:
    op.create_table(
        "member_role",
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.CheckConstraint("role IN ('member', 'board', 'admin')", name="ck_member_role_role"),
        sa.ForeignKeyConstraint(["granted_by"], ["member.id"]),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.PrimaryKeyConstraint("member_id", "role"),
    )


def _create_session() -> None:
    op.create_table(
        "session",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.PrimaryKeyConstraint("id_hash"),
    )
    op.create_index("ix_session_member_id", "session", ["member_id"], unique=False)


def _create_auth_event() -> None:
    op.create_table(
        "auth_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.CheckConstraint("channel IN ('web', 'matrix')", name="ck_auth_event_channel"),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_event_ts", "auth_event", ["ts"], unique=False)


def _create_config_kv() -> None:
    op.create_table(
        "config_kv",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["member.id"]),
        sa.PrimaryKeyConstraint("key"),
    )


def _create_signing_key() -> None:
    op.create_table(
        "signing_key",
        sa.Column("key_id", sa.String(length=50), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
    )


def _create_decision_log() -> None:
    op.create_table(
        "decision_log",
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_member_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=False),
        sa.Column("key_id", sa.String(length=50), nullable=False),
        sa.CheckConstraint(
            "payload_json IS NOT NULL AND jsonb_typeof(payload_json) = 'object'",
            name="ck_decision_log_payload_object",
        ),
        sa.CheckConstraint(
            "prev_hash ~ '^[0-9a-f]{64}$'",
            name="ck_decision_log_prev_hash_hex",
        ),
        sa.CheckConstraint(
            "entry_hash ~ '^[0-9a-f]{64}$'",
            name="ck_decision_log_entry_hash_hex",
        ),
        sa.CheckConstraint(
            "signature ~ '^[0-9a-f]{128}$'",
            name="ck_decision_log_signature_hex",
        ),
        sa.ForeignKeyConstraint(["actor_member_id"], ["member.id"]),
        sa.ForeignKeyConstraint(["key_id"], ["signing_key.key_id"]),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("entry_hash"),
    )
    op.create_index("ix_decision_log_ts", "decision_log", ["ts"], unique=False)

    op.execute(
        """
        CREATE FUNCTION commons_reject_decision_log_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'decision_log is append-only';
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER decision_log_no_update_delete
        BEFORE UPDATE OR DELETE ON decision_log
        FOR EACH ROW EXECUTE FUNCTION commons_reject_decision_log_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER decision_log_no_truncate
        BEFORE TRUNCATE ON decision_log
        FOR EACH STATEMENT EXECUTE FUNCTION commons_reject_decision_log_mutation()
        """
    )


def _adopt_existing_core_tables(bind, existing: set[str]) -> None:
    inspector = sa.inspect(bind)
    for table_name in CORE_TABLES:
        if table_name not in existing:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing = REQUIRED_COLUMNS[table_name] - columns
        if missing:
            raise RuntimeError(
                f"Cannot adopt existing {table_name} table; missing columns: "
                f"{', '.join(sorted(missing))}"
            )

    for table_name, index_name in (
        ("session", "ix_session_member_id"),
        ("auth_event", "ix_auth_event_ts"),
    ):
        if table_name not in existing:
            continue
        indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        if index_name not in indexes:
            raise RuntimeError(
                f"Cannot adopt existing {table_name} table; missing index {index_name}"
            )


def _is_legacy_config_kv(bind) -> bool:
    columns = {c["name"] for c in sa.inspect(bind).get_columns("config_kv")}
    if columns == {"key", "value", "updated_at"}:
        return True
    raise RuntimeError(
        "Cannot adopt existing config_kv table; unexpected columns: " + ", ".join(sorted(columns))
    )


def _convert_legacy_config_kv() -> None:
    """String values become JSON strings; the table gets updated_by."""
    op.add_column(
        "config_kv", sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.execute("UPDATE config_kv SET value_json = to_jsonb(value)")
    op.alter_column("config_kv", "value_json", nullable=False)
    op.drop_column("config_kv", "value")
    op.add_column("config_kv", sa.Column("updated_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "config_kv_updated_by_fkey", "config_kv", "member", ["updated_by"], ["id"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    existing_core = set(CORE_TABLES) & existing
    if existing_core and existing_core != set(CORE_TABLES):
        missing = sorted(set(CORE_TABLES) - existing_core)
        raise RuntimeError(
            "Cannot safely adopt a partial pre-migration identity schema; missing tables: "
            + ", ".join(missing)
        )
    # A database created by the pre-Alembic app (Base.metadata.create_all) also has
    # config_kv with a string `value` column; it is adopted and converted below.
    legacy_config = "config_kv" in existing and _is_legacy_config_kv(bind)
    unmanaged_targets = (set(ALL_TABLES) - set(CORE_TABLES)) & existing
    if legacy_config:
        unmanaged_targets.discard("config_kv")
    if unmanaged_targets:
        raise RuntimeError(
            "Cannot apply initial migration over untracked tables: "
            + ", ".join(sorted(unmanaged_targets))
        )

    preexisting_core = sorted((set(CORE_TABLES) & existing) | ({"config_kv"} if legacy_config else set()))
    _adopt_existing_core_tables(bind, existing)
    if legacy_config:
        _convert_legacy_config_kv()

    for table_name, create in (
        ("member", _create_member),
        ("member_role", _create_member_role),
        ("session", _create_session),
        ("auth_event", _create_auth_event),
        ("config_kv", _create_config_kv),
        ("signing_key", _create_signing_key),
        ("decision_log", _create_decision_log),
    ):
        if table_name not in existing:
            create()

    op.create_table(
        STATE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "preexisting_core",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )
    migration_state = sa.table(
        STATE_TABLE,
        sa.Column("id", sa.Integer()),
        sa.Column("preexisting_core", postgresql.JSONB()),
    )
    op.bulk_insert(migration_state, [{"id": 1, "preexisting_core": preexisting_core}])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if STATE_TABLE not in inspector.get_table_names():
        raise RuntimeError("Cannot safely downgrade without the initial migration state")
    preexisting_core = set(
        bind.execute(sa.text(f"SELECT preexisting_core FROM {STATE_TABLE} WHERE id = 1"))
        .scalar_one()
    )

    if "decision_log" not in preexisting_core:
        op.execute("DROP TRIGGER IF EXISTS decision_log_no_truncate ON decision_log")
        op.execute("DROP TRIGGER IF EXISTS decision_log_no_update_delete ON decision_log")
        op.execute("DROP FUNCTION IF EXISTS commons_reject_decision_log_mutation()")
    op.drop_table(STATE_TABLE)
    for table_name in reversed(ALL_TABLES):
        if table_name not in preexisting_core and table_name in inspector.get_table_names():
            op.drop_table(table_name)
