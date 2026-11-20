"""scripts/seed_demo.py: three small demo days, a valid log, and no way into real data."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app import categories, community, decision_log, ledger
from app.db import make_engine, make_sessionmaker, upgrade_database
from app.models import (
    Bookable,
    Booking,
    ChatNotification,
    ConfigKV,
    EventFeed,
    Member,
    MemberBadge,
    Proposal,
    ResourceEntry,
    Vote,
)

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 18)
DEMO_DB = "commons_demo_test"
SERVER = "example.test"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed_demo():
    return load("seed_demo")


@pytest.fixture(scope="module")
def usage_report():
    return load("usage_report")


@pytest.fixture(scope="module")
def config():
    return community.load(ROOT / "community.yaml.example")


def run_seed(module, engine, config, tmp_path, cast=None):
    sessions = make_sessionmaker(engine)
    with sessions() as db, db.begin():
        signer = decision_log.init_signer(db, tmp_path / "demo" / "keys")
    with sessions() as db, db.begin():
        return module.seed(
            db,
            config,
            signer,
            cast=cast or module.parse_cast(""),
            server_name=SERVER,
            as_of=AS_OF,
        )


@pytest.fixture
def seeded(seed_demo, clean_db, config, tmp_path):
    run_seed(seed_demo, clean_db, config, tmp_path)
    return clean_db


@pytest.fixture
def demo_engine(pg_engine):
    """A real, empty database with "demo" in its name."""
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {DEMO_DB} WITH (FORCE)")
        connection.exec_driver_sql(f"CREATE DATABASE {DEMO_DB}")
    url = pg_engine.url.set(database=DEMO_DB).render_as_string(hide_password=False)
    engine = make_engine(url)
    yield engine
    engine.dispose()
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {DEMO_DB} WITH (FORCE)")


def demo_env(tmp_path, **extra):
    return {
        "COMMONS_ENV": "demo",
        "COMMONS_DATA_DIR": str(tmp_path / "demo"),
        "COMMONS_COMMUNITY_FILE": str(ROOT / "community.yaml.example"),
        "COMMONS_MATRIX_SERVER_NAME": SERVER,
        **extra,
    }


def rows(engine, sql):
    with engine.connect() as connection:
        return [tuple(r) for r in connection.execute(text(sql))]


def test_covers_the_last_three_days_with_a_mix_of_members_and_roles(seeded):
    with make_sessionmaker(seeded)() as db:
        people = db.scalars(select(Member)).all()
        assert {r for m in people for r in m.role_names} == {"member", "board", "admin"}
        first, last = db.execute(
            select(func.min(ResourceEntry.created_at), func.max(ResourceEntry.created_at))
        ).one()
        assert first.date() == date(2026, 11, 15)
        assert last.date() == date(2026, 11, 17)
        assert last < datetime(2026, 11, 18, tzinfo=UTC)
        authors = db.scalars(select(ResourceEntry.member_id).distinct()).all()
        assert len(authors) == len(people)
        channels = set(db.scalars(select(ResourceEntry.channel).distinct()))
        assert channels == {"matrix", "web"}


def test_has_corrections_and_undone_entries(seeded):
    with make_sessionmaker(seeded)() as db:
        fixes = db.scalars(
            select(ResourceEntry).where(ResourceEntry.entry_type == "correction")
        ).all()
    assert any(f.void for f in fixes)
    assert any(not f.void for f in fixes)


def test_one_category_runs_low_and_the_others_do_not(seeded):
    with make_sessionmaker(seeded)() as db:
        active = {c.slug: c for c in categories.list_active(db)}
        levels = {slug: ledger.level(db, c).value for slug, c in active.items()}
        thresholds = {slug: c.shortage_threshold for slug, c in active.items()}
        water = [
            c.amount
            for c in ledger.counted_entries(db, active["water"])
            if c.entry.entry_type == "reading" and not c.void
        ]
    # Just above the threshold, so a reading on camera can set off the alert.
    assert thresholds["water"] < levels["water"] <= thresholds["water"] * 1.25
    assert levels["firewood"] > thresholds["firewood"]
    assert water[0] > water[1] > water[-1]  # falling towards the warning level


def test_bookings_keep_the_rules_and_some_are_still_ahead(seeded):
    with make_sessionmaker(seeded)() as db:
        bookings = db.scalars(select(Booking)).all()
    assert {b.bookable.type for b in bookings} == {"space", "vehicle", "tool"}
    assert any(b.starts_at > datetime(2026, 11, 18, tzinfo=UTC) for b in bookings)
    assert any(b.cancelled_at for b in bookings)
    assert any(b.returned_at for b in bookings)
    live = [b for b in bookings if b.cancelled_at is None]
    for a in live:
        for b in live:
            if a.id < b.id and a.bookable_id == b.bookable_id:
                assert a.blocks_until <= b.starts_at or b.blocks_until <= a.starts_at


def test_the_decision_log_is_a_valid_chain_that_says_it_is_demo(seeded):
    with make_sessionmaker(seeded)() as db:
        result = decision_log.verify_db(db)
        assert result.ok, result
        assert community.is_demo(db)
        entries = decision_log.load_entries(db)
    assert any(e["payload"].get("key") == community.DEMO_KEY for e in entries)
    assert any(e["kind"] == "role.granted" for e in entries)


def test_members_earn_badges(seeded):
    with make_sessionmaker(seeded)() as db:
        assert db.scalar(select(func.count()).select_from(MemberBadge)) > 0


def test_nothing_is_queued_for_the_community_room(seeded):
    with make_sessionmaker(seeded)() as db:
        assert db.scalar(select(func.count()).select_from(EventFeed)) == 0


def test_votes_show_a_pass_a_cancel_and_one_still_open(seeded):
    with make_sessionmaker(seeded)() as db:
        votes = db.scalars(select(Proposal).order_by(Proposal.opens_at)).all()
        assert [p.status for p in votes] == ["closed", "cancelled", "open"]
        assert votes[0].outcome is not None
        assert votes[0].outcome_option_id is not None  # passed
        end = datetime(2026, 11, 18, tzinfo=UTC)
        assert votes[0].closes_at < end < votes[2].closes_at
        assert {p.vote_type for p in votes} == {"spending", "rules", "events"}
        assert any(p.anonymous for p in votes)
        ballots = db.scalars(select(Vote)).all()
        assert all(v.cast_at < end for v in ballots)
        assert any(v.superseded_at is not None for v in ballots)  # a changed vote
        assert any(v.option_id is None for v in ballots)  # an abstention


def test_open_votes_wait_for_someone(seeded):
    with make_sessionmaker(seeded)() as db:
        voters = db.scalar(select(func.count()).select_from(Member))
        for p in db.scalars(select(Proposal).where(Proposal.status == "open")):
            current = select(func.count()).where(
                Vote.proposal_id == p.id, Vote.superseded_at.is_(None)
            )
            assert 0 < db.scalar(current) < voters


def test_no_old_vote_message_reaches_the_bot(seeded):
    with make_sessionmaker(seeded)() as db:
        assert db.scalar(select(func.count()).select_from(ChatNotification)) > 0
        waiting = (
            select(func.count())
            .select_from(ChatNotification)
            .where(ChatNotification.delivered_at.is_(None))
        )
        assert db.scalar(waiting) == 0


def test_the_log_replays_the_demo_votes(seeded):
    with make_sessionmaker(seeded)() as db:
        kinds = [e["kind"] for e in decision_log.load_entries(db)]
        for kind in (
            "proposal.opened",
            "vote.cast",
            "vote.changed",
            "proposal.closed",
            "proposal.cancelled",
        ):
            assert kind in kinds
        assert decision_log.verify_db(db).ok


def test_the_same_date_gives_the_same_history(seed_demo, seeded, config, tmp_path, pg_engine):
    sql_entries = (
        "SELECT category_id, member_id, channel, entry_type, amount, occurred_at, void "
        "FROM resource_entry ORDER BY id"
    )
    sql_bookings = "SELECT bookable_id, member_id, starts_at, ends_at FROM booking ORDER BY id"
    first = rows(seeded, sql_entries), rows(seeded, sql_bookings)
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    upgrade_database(pg_engine)
    run_seed(seed_demo, pg_engine, config, tmp_path)
    assert (rows(pg_engine, sql_entries), rows(pg_engine, sql_bookings)) == first


def test_uses_the_given_cast(seed_demo, clean_db, config, tmp_path):
    cast = seed_demo.parse_cast(
        json.dumps(
            [
                {"localpart": "riva", "name": "Riva", "roles": ["admin"]},
                {"localpart": "tam", "name": "Tam", "roles": []},
            ]
        )
    )
    run_seed(seed_demo, clean_db, config, tmp_path, cast)
    assert sorted(r[0] for r in rows(clean_db, "SELECT display_name FROM member")) == [
        "Riva",
        "Tam",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '[{"localpart": "a"}]',
        json.dumps([{"localpart": p, "name": p, "roles": []} for p in ("a", "b")]),  # no admin
    ],
)
def test_refuses_an_unusable_cast(seed_demo, raw):
    with pytest.raises(seed_demo.SeedRefused):
        seed_demo.parse_cast(raw)


def test_an_older_community_file_still_gives_a_complete_demo(seed_demo, clean_db, tmp_path):
    older = community.parse(
        "resource_categories:\n"
        "  - {slug: water, name: Water, unit: l, kind: stock, shortage_threshold: 1000}\n"
        "bookable:\n"
        "  - {name: Hall, type: space}\n"
        "badges:\n"
        "  - {slug: first-log, name: First log, rule: {count: ledger_entries, min: 1}}\n"
    )
    run_seed(seed_demo, clean_db, older, tmp_path)
    with make_sessionmaker(clean_db)() as db:
        assert {b.bookable.type for b in db.scalars(select(Booking))} == {
            "space",
            "vehicle",
            "tool",
        }
    earned = rows(
        clean_db,
        "SELECT DISTINCT r.slug FROM member_badge b JOIN badge_rule r ON r.id = b.badge_rule_id",
    )
    assert {slug for (slug,) in earned} >= {"first-log"}


def test_every_resource_is_logged_and_every_thing_is_booked(seeded):
    with make_sessionmaker(seeded)() as db:
        active = categories.list_active(db)
        for category in active:
            assert ledger.counted_entries(db, category), category.slug
        booked = {b.bookable.name for b in db.scalars(select(Booking))}
        things = {t.name for t in db.scalars(select(Bookable))}
    assert booked == things


def test_only_one_resource_is_close_to_running_out(seeded):
    with make_sessionmaker(seeded)() as db:
        low = [
            c.slug
            for c in categories.list_active(db)
            if c.kind == "stock" and ledger.level(db, c).value <= c.shortage_threshold * 1.25
        ]
    assert low == ["water"]


def test_default_cast_is_fictional(seed_demo):
    assert all("Example" in person["name"] for person in seed_demo.DEFAULT_CAST)


@pytest.mark.parametrize(
    ("env", "database", "data_dir"),
    [
        ({}, "commons_demo", "/data/demo"),
        ({"COMMONS_ENV": "production"}, "commons_demo", "/data/demo"),
        ({"COMMONS_ENV": "demo"}, "commons", "/data/demo"),
        ({"COMMONS_ENV": "demo"}, "commons_demo", "/data"),
    ],
)
def test_refuses_anything_but_the_demo_setup(seed_demo, env, database, data_dir):
    assert seed_demo.refusal(env, database, Path(data_dir))


def test_main_refuses_production_before_connecting(seed_demo, tmp_path, capsys):
    # Nothing listens here: reaching the database would raise instead of refusing.
    engine = make_engine("postgresql+psycopg://x@127.0.0.1:1/commons")
    assert seed_demo.main([], env=demo_env(tmp_path), engine=engine) == 2
    assert "not a demo database" in capsys.readouterr().err


def test_refuses_a_database_with_data_in_it(seed_demo, seeded, config, tmp_path):
    with pytest.raises(seed_demo.SeedRefused, match="not empty"):
        run_seed(seed_demo, seeded, config, tmp_path)


def test_main_seeds_a_demo_database_with_its_own_key(seed_demo, demo_engine, tmp_path, capsys):
    env = demo_env(tmp_path)
    assert seed_demo.main(["--as-of", AS_OF.isoformat()], env=env, engine=demo_engine) == 0
    assert "members" in capsys.readouterr().out
    assert (tmp_path / "demo" / "keys").is_dir()
    assert rows(demo_engine, "SELECT count(*) FROM member")[0][0] == len(seed_demo.DEFAULT_CAST)
    # Running it again doesn't pile a second history on top.
    assert seed_demo.main(["--as-of", AS_OF.isoformat()], env=env, engine=demo_engine) == 2


def test_usage_report_refuses_marked_data_under_any_name(usage_report, seeded, capsys):
    assert seeded.url.database == "postgres"  # the name alone wouldn't give it away
    assert usage_report.main([], env={}, engine=seeded) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "demo data" in captured.err


def test_usage_report_checks_the_real_marker(usage_report):
    assert f"'{community.DEMO_KEY}'" in str(usage_report.IS_DEMO)


@pytest.mark.parametrize(
    ("environment", "database", "marked", "allowed"),
    [
        ("production", "commons", False, True),
        ("demo", "commons_demo", True, True),
        ("production", "commons_demo", False, False),
        ("production", "commons", True, False),
        ("demo", "commons", False, False),
    ],
)
def test_mode_and_database_must_match(environment, database, marked, allowed):
    assert (community.demo_refusal(environment, database, marked) is None) is allowed


def test_the_app_will_not_start_on_demo_data_in_production(app, clean_db):
    with make_sessionmaker(clean_db)() as db, db.begin():
        db.add(ConfigKV(key=community.DEMO_KEY, value_json=True))

    async def start():
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(SystemExit, match="demo database"):
        asyncio.run(start())


def test_makefile_only_ever_drops_the_demo_database():
    makefile = (ROOT / "Makefile").read_text()
    assert "DEMO_DB" not in makefile
    drops = [line for line in makefile.splitlines() if "DROP DATABASE" in line]
    assert len(drops) == 1
    assert "DROP DATABASE IF EXISTS commons_demo;" in drops[0]
