"""scripts/usage_report.py: true counts, no personal data, never the demo database."""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from app.db import make_engine, make_sessionmaker
from app.models import Bookable, Booking, Member, ResourceCategory, ResourceEntry

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DAY_1 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
DAY_5 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
TZ = "Europe/Stockholm"
PRIVATE = ("Secret Sam", "@sam:example.test", "behind the old barn", "for my birthday")


@pytest.fixture(scope="module")
def report_module():
    spec = importlib.util.spec_from_file_location("usage_report", ROOT / "scripts/usage_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def entry(category, member, kind="reading", *, at=DAY_1, channel="matrix", **fields):
    return ResourceEntry(
        category_id=category.id,
        member_id=member.id,
        channel=channel,
        entry_type=kind,
        amount=fields.pop("amount", 10.0),
        occurred_at=at,
        created_at=at,
        **fields,
    )


def booking(thing, member, *, at=DAY_1, channel="web", **fields):
    return Booking(
        bookable_id=thing.id,
        member_id=member.id,
        starts_at=at + timedelta(days=30),
        blocks_until=at + timedelta(days=30, hours=2),
        channel=channel,
        created_at=at,
        **fields,
    )


@pytest.fixture
def box(clean_db):
    """Sam logs and books; Una's only entry is undone; Cy's only booking cancelled."""
    with make_sessionmaker(clean_db)() as db, db.begin():
        sam = Member(matrix_user_id="@sam:example.test", display_name="Secret Sam")
        una = Member(matrix_user_id="@una:example.test", display_name="Una")
        cy = Member(matrix_user_id="@cy:example.test", display_name="Cy")
        idle = Member(matrix_user_id="@idle:example.test", display_name="Idle")
        water = ResourceCategory(slug="water", name="Water", unit="l", kind="stock")
        wood = ResourceCategory(slug="wood", name="Firewood", unit="m3", kind="stock")
        old = ResourceCategory(
            slug="old", name="Old thing", unit="x", kind="flow", archived_at=DAY_1
        )
        theatre = Bookable(slug="theatre", name="Theatre", type="space")
        db.add_all([sam, una, cy, idle, water, wood, old, theatre])
        db.flush()

        fixed = entry(water, sam, note="behind the old barn")
        undone = entry(water, una, "add", at=DAY_5)
        later = entry(wood, sam, "use", at=DAY_5, channel="web")
        db.add_all([fixed, undone, later])
        db.flush()
        db.add_all(
            [
                entry(water, sam, "correction", at=DAY_1, corrects_id=fixed.id),
                entry(water, una, "correction", at=DAY_5, corrects_id=undone.id, void=True),
                booking(theatre, sam, at=DAY_5, note="for my birthday"),
                booking(
                    theatre,
                    cy,
                    at=DAY_5 + timedelta(days=1),
                    cancelled_at=DAY_5 + timedelta(days=1),
                    cancelled_by=cy.id,
                ),
            ]
        )
    return clean_db


def report(module, engine, **kw):
    with module.read_only(engine) as connection:
        return module.collect(connection, timezone=TZ, now=NOW, **kw)


def test_counts_only_what_really_happened(report_module, box):
    r = report(report_module, box)
    assert r.active_members == 1  # Una's entry was undone, Cy's booking cancelled
    assert r.member_accounts == 4
    assert r.first_use == date(2026, 11, 10)
    assert r.days_in_use == 9
    assert r.days_with_activity == 2

    water, wood = r.categories
    assert (water.name, water.entries, water.corrections, water.undone) == ("Water", 1, 1, 1)
    assert (wood.name, wood.entries, wood.corrections, wood.undone) == ("Firewood", 1, 0, 0)
    assert [(b.name, b.bookings, b.cancelled) for b in r.bookables] == [("Theatre", 1, 1)]
    # Reading + correction in chat; the use and the booking on the web.
    assert r.channels == {"matrix": 2, "web": 2, "sensor": 0}


def test_an_archived_category_without_use_is_left_out(report_module, box):
    assert "Old thing" not in {c.name for c in report(report_module, box).categories}


def test_a_later_correction_brings_an_undone_entry_back(report_module, box):
    with make_sessionmaker(box)() as db, db.begin():
        undo = db.query(ResourceEntry).filter(ResourceEntry.void.is_(True)).one()
        db.add(
            ResourceEntry(
                category_id=undo.category_id,
                member_id=undo.member_id,
                channel="matrix",
                entry_type="correction",
                amount=5.0,
                occurred_at=DAY_5 + timedelta(hours=1),
                created_at=DAY_5 + timedelta(hours=1),
                corrects_id=undo.corrects_id,
            )
        )
    r = report(report_module, box)
    assert r.active_members == 2
    assert (r.categories[0].entries, r.categories[0].undone) == (2, 0)


def test_since_leaves_out_setup_days(report_module, box):
    r = report(report_module, box, since=date(2026, 11, 12))
    assert r.first_use == date(2026, 11, 14)
    assert r.days_in_use == 5
    water, wood = r.categories
    assert (water.entries, water.corrections, water.undone) == (0, 0, 1)
    assert wood.entries == 1
    assert r.channels == {"matrix": 0, "web": 2, "sensor": 0}


def test_days_are_local_days(report_module, clean_db):
    # 23:30 UTC on the 10th is already the 11th in Stockholm.
    with make_sessionmaker(clean_db)() as db, db.begin():
        sam = Member(matrix_user_id="@sam:example.test")
        water = ResourceCategory(slug="water", name="Water", unit="l", kind="stock")
        db.add_all([sam, water])
        db.flush()
        db.add(entry(water, sam, at=datetime(2026, 11, 10, 23, 30, tzinfo=UTC)))
    assert report(report_module, clean_db).first_use == date(2026, 11, 11)


def test_an_empty_box_reports_nothing_rather_than_failing(report_module, clean_db):
    r = report(report_module, clean_db)
    assert (r.first_use, r.days_in_use, r.active_members) == (None, 0, 0)
    assert "none yet" in report_module.to_markdown(r)
    assert report_module.to_csv(r).startswith("section,item,metric,value\n")


def without_governance(engine):
    """A box from before the votes migrations."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TABLE chat_notification, vote, proposal_option, proposal CASCADE"
        )


def test_governance_is_not_shown_as_zero_before_it_exists(report_module, box):
    without_governance(box)
    r = report(report_module, box)
    assert (r.proposals, r.votes_current, r.votes_changed) == (None, None, None)
    assert "| Proposals | not on this box yet |" in report_module.to_markdown(r)
    rows = list(csv.DictReader(io.StringIO(report_module.to_csv(r))))
    assert next(x for x in rows if x["metric"] == "proposals")["value"] == ""


def test_counts_proposals_and_votes_once_they_exist(report_module, box):
    without_governance(box)
    with box.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE proposal (id int, created_at timestamptz)")
        connection.exec_driver_sql(
            "CREATE TABLE vote (member_id int, channel text, cast_at timestamptz, "
            "superseded_at timestamptz)"
        )
        connection.exec_driver_sql("INSERT INTO proposal VALUES (1, '2026-11-15T09:00Z')")
        connection.exec_driver_sql(
            "INSERT INTO vote SELECT id, 'matrix', '2026-11-15T09:00Z', '2026-11-15T10:00Z' "
            "FROM member WHERE matrix_user_id = '@una:example.test'"
        )
        connection.exec_driver_sql(
            "INSERT INTO vote SELECT id, 'matrix', '2026-11-15T10:00Z', NULL "
            "FROM member WHERE matrix_user_id = '@una:example.test'"
        )
    r = report(report_module, box)
    assert (r.proposals, r.votes_current, r.votes_changed) == (1, 1, 1)
    assert r.active_members == 2  # Una voted
    assert r.channels["matrix"] == 4


@pytest.mark.parametrize("output", ["markdown", "csv"])
def test_output_holds_no_personal_data(report_module, box, capsys, output):
    assert report_module.main(["--format", output], env={}, engine=box) == 0
    printed = capsys.readouterr().out
    assert "Water" in printed
    for private in PRIVATE:
        assert private not in printed
    assert "example.test" not in printed


def test_csv_is_machine_readable(report_module, box):
    rows = list(csv.DictReader(io.StringIO(report_module.to_csv(report(report_module, box)))))
    found = {(x["section"], x["item"], x["metric"]): x["value"] for x in rows}
    assert found["resource", "Water", "entries"] == "1"
    assert found["bookable", "Theatre", "cancelled"] == "1"
    assert found["channel", "web", "share_percent"] == "50"


def test_csv_neutralises_formulas_in_configured_names(report_module, clean_db):
    with make_sessionmaker(clean_db)() as db, db.begin():
        db.add(ResourceCategory(slug="evil", name="=HYPERLINK(1)", unit="l", kind="stock"))
    assert ",'=HYPERLINK(1)," in report_module.to_csv(report(report_module, clean_db))


@pytest.mark.parametrize("mode", ["demo", "DEMO", "test", "staging"])
def test_refuses_anything_but_production(report_module, box, capsys, mode):
    assert report_module.main([], env={"COMMONS_ENV": mode}, engine=box) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "refused" in out.err


def test_refuses_the_demo_database_before_connecting(report_module, capsys):
    # Nothing listens here: reaching the database would raise instead of refusing.
    engine = make_engine("postgresql+psycopg://x@127.0.0.1:1/commons_demo")
    assert report_module.main([], env={"COMMONS_ENV": "production"}, engine=engine) == 2
    assert "demo database" in capsys.readouterr().err


def test_the_transaction_cannot_write(report_module, box):
    with (
        pytest.raises(DBAPIError, match="read-only transaction"),
        report_module.read_only(box) as connection,
    ):
        connection.exec_driver_sql("UPDATE member SET display_name = 'x'")
