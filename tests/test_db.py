"""Database settings and table invariants."""

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError

from app import community, members
from app.db import make_engine, make_sessionmaker, upgrade_database
from app.models import AuthEvent, Base, Member, MemberRole, Session

GOVERNANCE_TABLES = {"proposal_message", "chat_notification", "proposal", "proposal_option", "vote"}


@pytest.fixture
def file_db(clean_db):
    with make_sessionmaker(clean_db)() as s:
        yield s


def test_only_postgresql_is_accepted():
    with pytest.raises(ValueError, match="PostgreSQL"):
        make_engine("sqlite:///commons.db")


def test_timestamps_are_timestamptz(file_db):
    rows = file_db.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'member' AND column_name IN ('created_at', 'deactivated_at')"
        )
    ).all()
    assert {r.data_type for r in rows} == {"timestamp with time zone"}


def test_no_password_columns(file_db):
    cols = set(
        file_db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'member'")
        ).scalars()
    )
    assert cols == {"id", "matrix_user_id", "display_name", "created_at", "deactivated_at"}


def test_matrix_user_id_unique(file_db):
    file_db.add_all([Member(matrix_user_id="@a:x.test"), Member(matrix_user_id="@a:x.test")])
    with pytest.raises(IntegrityError):
        file_db.commit()


def test_role_check_constraint(file_db):
    m = Member(matrix_user_id="@a:x.test")
    file_db.add(m)
    file_db.flush()
    file_db.add(MemberRole(member_id=m.id, role="superuser"))
    with pytest.raises(IntegrityError):
        file_db.commit()


def test_foreign_keys_enforced(file_db):
    file_db.add(MemberRole(member_id=999, role="member"))
    with pytest.raises(IntegrityError):
        file_db.commit()


def test_naive_datetime_rejected(file_db):
    file_db.add(Member(matrix_user_id="@a:x.test", created_at=datetime(2026, 1, 1)))  # noqa: DTZ001
    with pytest.raises(StatementError):
        file_db.commit()


def test_add_member_always_gets_member_role(file_db, log_signer):
    m = members.add_member(file_db, "@a:x.test", roles=("admin",), signer=log_signer)
    assert m.role_names == {"member", "admin"}


@pytest.mark.parametrize("bad", ["nora", "@Nora:x.test", "@nora", "nora:x.test", "@a b:x.test"])
def test_add_member_rejects_bad_matrix_ids(file_db, bad):
    with pytest.raises(members.MemberError):
        members.add_member(file_db, bad)


def test_add_member_rejects_duplicates_and_unknown_roles(file_db, log_signer):
    members.add_member(file_db, "@a:x.test")
    with pytest.raises(members.MemberError):
        members.add_member(file_db, "@a:x.test")
    with pytest.raises(members.MemberError):
        members.add_member(file_db, "@b:x.test", roles=("superuser",), signer=log_signer)


def test_initial_migration_is_idempotent(clean_db):
    upgrade_database(clean_db)
    with make_sessionmaker(clean_db)() as session:
        tables = session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename IN "
                "('member', 'member_role', 'session', 'auth_event', 'config_kv', "
                "'signing_key', 'decision_log')"
            )
        ).scalars()
    assert set(tables) == {
        "member",
        "member_role",
        "session",
        "auth_event",
        "config_kv",
        "signing_key",
        "decision_log",
    }


def test_initial_migration_adopts_existing_identity_tables(pg_engine):
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        identity_tables = (
            Member.__table__,
            MemberRole.__table__,
            Session.__table__,
            AuthEvent.__table__,
        )
        for table in identity_tables:
            table.create(connection)
        connection.exec_driver_sql(
            "INSERT INTO member (id, matrix_user_id, created_at) "
            "VALUES (7, '@existing:example.test', CURRENT_TIMESTAMP)"
        )

    upgrade_database(pg_engine)

    with pg_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT matrix_user_id FROM member WHERE id = 7")).scalar_one()
            == "@existing:example.test"
        )
        assert set(
            connection.execute(
                text("SELECT preexisting_core FROM _commons_migration_0001_state")
            ).scalar_one()
        ) == {"member", "member_role", "session", "auth_event"}


def test_initial_migration_rejects_partial_identity_schema(pg_engine):
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        Member.__table__.create(connection)

    with pytest.raises(RuntimeError, match="partial pre-migration identity schema"):
        upgrade_database(pg_engine)


def test_decision_log_rejects_update_delete_and_truncate(clean_db):
    with clean_db.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO signing_key (key_id, public_key, created_at) "
                "VALUES ('k1', 'public-key', CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO decision_log "
                "(seq, ts, kind, payload_json, prev_hash, entry_hash, signature, key_id) "
                "VALUES (1, CURRENT_TIMESTAMP, 'test', '{}'::jsonb, :prev, :hash, :sig, 'k1')"
            ),
            {"prev": "0" * 64, "hash": "a" * 64, "sig": "b" * 128},
        )

    for statement in (
        "UPDATE decision_log SET kind = 'changed' WHERE seq = 1",
        "DELETE FROM decision_log WHERE seq = 1",
        "TRUNCATE decision_log",
    ):
        with (
            pytest.raises(DBAPIError, match="decision_log is append-only"),
            clean_db.begin() as connection,
        ):
            connection.exec_driver_sql(statement)


def test_migrations_adopt_a_database_built_before_alembic(pg_engine):
    """A box that ran main (create_all, no Alembic) keeps its members, categories and ledger."""
    from importlib import import_module

    resources = import_module("migrations.versions.0002_resources")
    legacy = [
        t
        for t in Base.metadata.sorted_tables
        if t.name
        not in {
            "config_kv",
            "signing_key",
            "decision_log",
            "event_feed",
            "bookable",
            "booking",
            "calendar_token",
            "badge_rule",
            "member_badge",
            "forecast_run",
        }
        | GOVERNANCE_TABLES
    ]
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        for table in legacy:
            table.create(connection)
        # main before Alembic had no undo marker (added by 0003).
        connection.exec_driver_sql("ALTER TABLE resource_entry DROP COLUMN void")
        connection.execute(text(resources.GUARD_FUNCTION))
        connection.execute(text(resources.GUARD_TRIGGER))
        connection.exec_driver_sql(
            "CREATE TABLE config_kv (key varchar(100) PRIMARY KEY, value varchar(1000) NOT NULL, "
            "updated_at timestamptz NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO config_kv VALUES ('community.name', 'The Outpost', now()), "
            "('community_yaml.seeded_at', '2026-11-09T08:00:00+00:00', now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO member (id, matrix_user_id, created_at) VALUES (1, '@a:x.test', now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO resource_category (id, slug, name, unit, kind, forecast_horizon_days, "
            "position, created_at) VALUES (1, 'water', 'Water', 'l', 'stock', 30, 0, now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO resource_entry (id, category_id, member_id, channel, entry_type, amount, "
            "occurred_at, created_at) VALUES (1, 1, 1, 'matrix', 'reading', 2400, now(), now())"
        )

    upgrade_database(pg_engine)

    with pg_engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0017_merge_forecast_and_welcome"
        )
        values = dict(connection.execute(text("SELECT key, value_json FROM config_kv")).all())
        assert values["community.name"] == "The Outpost"
        assert connection.execute(text("SELECT amount FROM resource_entry")).scalar_one() == 2400
    with make_sessionmaker(pg_engine)() as db:
        assert community.is_seeded(db)
    with pytest.raises(DBAPIError, match="never deleted"), pg_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM resource_entry")


def test_resource_migration_rejects_partial_schema(pg_engine):
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    upgrade_database(pg_engine)
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE resource_alias")
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num = '0001_postgresql_core'"
        )
    with pytest.raises(RuntimeError, match="partial resource schema"):
        upgrade_database(pg_engine)
