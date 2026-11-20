"""The database half of a restore: dump with backup.py's pg_dump flags, restore with its
pg_restore flags into an empty database, and check that nothing a member relies on is lost.

pg_dump and pg_restore run inside the test PostgreSQL container (`make test-db`; in CI
the service container, named by COMMONS_TEST_PG_CONTAINER), so their version matches
the server, exactly as on the box.
"""

# ruff: noqa: S603, S607 - fixed commands (age, docker) the tests run

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from tests import backup_script
from tests.conftest import SERVER, TEST_DATABASE_URL

from app import categories, decision_log, ledger, members
from app.db import make_engine, make_sessionmaker

backup = backup_script.load()

CONTAINER = os.environ.get("COMMONS_TEST_PG_CONTAINER", "commons-test-db")
RESTORED = "commons_restore_test"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _container_running() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() == "true"


pytestmark = pytest.mark.skipif(
    not _container_running() and not os.environ.get("CI"),
    reason=f"needs the test PostgreSQL container {CONTAINER} (make test-db)",
)


def _pg(*argv: str, stdin: bytes | None = None) -> bytes:
    if not CONTAINER:
        pytest.fail("COMMONS_TEST_PG_CONTAINER is empty: name the test PostgreSQL container")
    return subprocess.run(
        ["docker", "exec", "-i", CONTAINER, *argv],
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


def _user() -> str:
    return make_url(TEST_DATABASE_URL).username or "postgres"


def _schema(conn) -> dict[str, set]:
    """Everything that enforces an invariant: constraints, triggers, indexes, grants."""
    return {
        "constraints": set(
            conn.execute(
                text(
                    # Not the definition text: PostgreSQL re-renders casts in CHECKs.
                    "SELECT conrelid::regclass::text, conname, contype "
                    "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
                )
            )
        ),
        "triggers": set(
            conn.execute(
                text(
                    "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal"
                )
            )
        ),
        "indexes": set(
            conn.execute(
                text("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'public'")
            )
        ),
        "grants": set(
            conn.execute(
                text(
                    "SELECT table_name, grantee, privilege_type "
                    "FROM information_schema.role_table_grants WHERE table_schema = 'public'"
                )
            )
        ),
    }


@pytest.fixture
def source(clean_db, log_signer):
    """A small community: members, roles, ledger entries, a logged history."""
    sm = make_sessionmaker(clean_db)
    with sm() as db:
        log = {"signer": log_signer, "channel": "cli"}
        ada = members.add_member(db, f"@ada:{SERVER}", "Ada", ("admin",), **log)
        nora = members.add_member(db, f"@nora:{SERVER}", "Nora", ("board",), **log)
        water = categories.create(
            db,
            categories.CategorySpec(
                slug="water", name="Water", unit="l", kind="stock", shortage_threshold=500
            ),
        )
        db.commit()
        ledger.add_entry(db, ada, water, "reading", 2000, channel="web", now=NOW)
        ledger.add_entry(db, nora, water, "use", 150.5, channel="matrix", now=NOW)
        members.revoke_role(db, nora, "board", **log)
        db.commit()
    return clean_db


@pytest.fixture
def restored(source):
    user = _user()
    db_name = make_url(TEST_DATABASE_URL).database or "postgres"
    dump = _pg("pg_dump", "-U", user, *backup.PG_DUMP_ARGS, db_name)
    _pg("psql", "-U", user, "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS {RESTORED}")
    _pg("psql", "-U", user, "-d", "postgres", "-c", f"CREATE DATABASE {RESTORED}")
    _pg("pg_restore", "-U", user, "-d", RESTORED, *backup.PG_RESTORE_ARGS, stdin=dump)
    engine = make_engine(make_url(TEST_DATABASE_URL).set(database=RESTORED))
    yield engine
    engine.dispose()
    _pg("psql", "-U", user, "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS {RESTORED}")


def _snapshot(engine) -> dict:
    with make_sessionmaker(engine)() as db:
        result = decision_log.verify_db(db)
        return {
            "verify": (result.ok, result.head_seq, result.head_hash),
            "levels": [(lv.category.slug, lv.value) for lv in ledger.levels(db)],
            "members": [
                (m.matrix_user_id, sorted(r.role for r in m.roles), m.is_active)
                for m in members.list_members(db)
            ],
            "log": decision_log.load_entries(db),
        }


def test_restored_database_matches_and_its_log_verifies(source, restored):
    before, after = _snapshot(source), _snapshot(restored)
    assert after["verify"][0] is True
    assert after["verify"][1] >= 3  # role grants and the revocation
    assert after == before
    assert after["levels"] == [("water", 1849.5)]


def test_restored_database_keeps_every_invariant(source, restored):
    with source.connect() as a, restored.connect() as b:
        assert _schema(b) == _schema(a)


def test_restored_decision_log_is_still_append_only(restored):
    with restored.connect() as conn:
        for sql in (
            "UPDATE decision_log SET kind = 'x' WHERE seq = 1",
            "DELETE FROM decision_log WHERE seq = 1",
            "TRUNCATE decision_log",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                conn.execute(text(sql))
            conn.rollback()


def test_restore_refuses_a_non_empty_database(source):
    """--exit-on-error + --single-transaction: restoring over data fails and changes nothing."""
    user = _user()
    db_name = make_url(TEST_DATABASE_URL).database or "postgres"
    dump = _pg("pg_dump", "-U", user, *backup.PG_DUMP_ARGS, db_name)
    with pytest.raises(subprocess.CalledProcessError):
        _pg("pg_restore", "-U", user, "-d", db_name, *backup.PG_RESTORE_ARGS, stdin=dump)
    with source.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM member")) == 2
