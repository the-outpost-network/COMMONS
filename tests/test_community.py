"""community.yaml: the strict non-category sections and the signed seed.

Category rules, seeding once and the startup error live in test_categories.py.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import bookables, categories, community
from app import decision_log as dl
from app.main import create_app
from app.models import BadgeRule, ConfigKV, DecisionLog

EXAMPLE = Path(__file__).parent.parent / "community.yaml.example"
LOG = logging.getLogger("test")


def example() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def problems(source: str) -> str:
    with pytest.raises(community.CommunityConfigError) as e:
        community.parse(source)
    return str(e.value)


# --- schema -----------------------------------------------------------------


def test_example_is_valid():
    config = community.parse(example())
    assert config.community.name == "The Outpost"
    assert {r.slug for r in config.roles} == {"member", "board", "admin"}
    assert [(t.name, t.type) for t in config.all_things()] == [
        ("Theatre", "space"),
        ("Workshop", "space"),
        ("Car", "vehicle"),
        ("Saw", "tool"),
    ]
    assert config.bookings.open_booking_hours == 2
    assert config.voting.quorum_pct is None
    assert config.voting.threshold_pct == 50
    assert [t.slug for t in config.voting.types] == ["spending", "rules", "events"]


def test_example_guide_fits_the_model_context():
    config = community.parse(example())
    titles = [t.title for t in config.guide]
    assert "Dishwasher" in titles
    assert "Pant (deposit bottles and cans)" in titles
    assert sum(len(t.title) + len(t.text) for t in config.guide) <= community.GUIDE_MAX_CHARS


def test_guide_is_checked():
    base = "resource_categories:\n  - {slug: water, name: Water, unit: l, kind: stock}\nguide:\n"
    assert "there twice" in problems(
        base + "  - {title: Sauna, text: a}\n  - {title: sauna, text: b}\n"
    )
    too_long = "x" * community.GUIDE_TOPIC_MAX_CHARS
    many = "".join(f"  - {{title: T{n}, text: {too_long}}}\n" for n in range(6))
    assert "fits the model's context" in problems(base + many)
    assert "guide[0].text" in problems(base + "  - {title: Sauna, text: ''}\n")
    assert "guide[0].when" in problems(base + "  - {title: Sauna, text: a, when: now}\n")


def test_sections_other_than_categories_are_optional():
    config = community.parse(
        "resource_categories:\n  - {slug: water, name: Water, unit: l, kind: stock}\n"
    )
    assert config.roles is None
    assert config.voting is None
    assert set(config.config_values()) == {community.TIMEZONE_KEY}
    assert config.all_things() == []
    assert config.guide == []
    assert config.bookings == bookables.BookingSettings()


def test_typo_is_an_error_not_ignored():
    msg = problems(example().replace("quorum_pct:", "quorom_pct:"))
    assert "voting.quorom_pct: unknown setting (typo?)" in msg


def test_all_problems_reported_with_location():
    source = (
        example()
        .replace("Europe/Stockholm", "Mars/Base")
        .replace("threshold_pct: 50", "threshold_pct: 150")
        .replace("type: tool", "type: gadget")
        .replace("count: votes", "count: water_used")
    )
    msg = problems(source)
    for expected in (
        "community.timezone: unknown time zone 'Mars/Base'",
        "bookable[3].type: must be space, vehicle, tool",
        "voting.threshold_pct: must be less than 100",
        "badges[4] (voter).rule.count: must be",
    ):
        assert expected in msg


def test_duplicate_slugs_and_names_rejected():
    source = example().replace("name: Workshop", "name: theatre")
    assert "the name 'theatre' is used for two bookable things" in problems(source)
    assert "spaces: slug 'theatre' is used twice" in problems(
        LEGACY.replace("slug: workshop", "slug: theatre")
    )


def test_roles_must_match_the_database():
    source = example().replace("slug: board", "slug: council")
    assert "roles: must be exactly member, board, admin" in problems(source)


def test_roles_can_be_renamed():
    config = community.parse(example().replace("name: Board", "name: Council"))
    assert {r.name for r in config.roles} == {"Member", "Council", "Admin"}


def test_vote_types_are_checked():
    source = example().replace("slug: rules", "slug: spending")
    assert "voting.types: slug 'spending' is used twice" in problems(source)
    assert "voting.types[2] (events).results: must be 'live' or 'after_close'" in problems(
        example().replace("results: after_close", "results: secret")
    )
    assert "set a currency" in problems(example().replace("currency: SEK", "currency: null"))


def test_an_option_must_win_with_more_than_the_threshold():
    # 100 % would mean no option could ever win.
    assert "voting.threshold_pct: must be less than 100" in problems(
        example().replace("threshold_pct: 50", "threshold_pct: 100")
    )


def test_the_default_threshold_is_at_least_a_majority():
    assert "voting.threshold_pct: must be greater than or equal to 50" in problems(
        example().replace("threshold_pct: 50", "threshold_pct: 40")
    )


def test_yaml_syntax_error_has_line():
    msg = problems("community:\n  name: [unclosed\n")
    assert msg.startswith("community.yaml (line 3")
    assert "not valid YAML" in msg


def test_directory_instead_of_file(tmp_path):
    (tmp_path / "community.yaml").mkdir()  # what Docker creates for a missing bind-mount source
    with pytest.raises(community.CommunityConfigError, match="a directory"):
        community.load(tmp_path / "community.yaml")


# --- signed seed ------------------------------------------------------------


@pytest.fixture
def signer(clean_db, tmp_path):
    with Session(clean_db) as db, db.begin():
        return dl.init_signer(db, tmp_path / "keys")


def test_seed_writes_config_and_signed_log(clean_db, signer):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        assert [c.slug for c in categories.list_all(db)] == ["water", "firewood"]
        assert community.get(db, "voting")["threshold_pct"] == 50
        assert community.get(db, community.TIMEZONE_KEY) == "Europe/Stockholm"
        assert community.get(db, "badges") is None  # badge rules have their own table
        rules = db.scalars(select(BadgeRule).order_by(BadgeRule.position)).all()
        assert [(r.slug, r.rule_json) for r in rules] == [
            ("first-log", {"count": "ledger_entries", "min": 1}),
            ("regular", {"count": "ledger_entries", "min": 10}),
            ("week-of-logs", {"count": "ledger_days_in_a_row", "min": 7}),
            ("good-borrower", {"count": "bookings_honoured", "min": 1}),
            ("voter", {"count": "votes", "min": 5}),
        ]
        entries = dl.load_entries(db)
        assert {e["kind"] for e in entries} == {"config.changed"}
        keys = [e["payload"]["key"] for e in entries]
        assert keys[0] == "resource_categories"
        assert set(keys) >= {community.NAME_KEY, "roles", "voting", "badges"}
        assert set(keys) >= {"bookable", "bookings", community.GUIDE_KEY}
        assert community.guide_seeded(db)
        prompt = community.guide_for_prompt(db)
        assert prompt.startswith("## About the Outpost\n")
        assert "\n\n## Dishwasher\n" in prompt
        first = entries[0]
        assert (first["payload"]["old"], first["payload"]["source"]) == (None, "community.yaml")
        assert first["actor"] is None
        assert [c["slug"] for c in first["payload"]["new"]] == ["water", "firewood"]
        assert dl.verify_db(db).ok
    assert community.community_name(clean_db) == "The Outpost"


def test_seed_logs_only_once(clean_db, signer, tmp_path):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        before = db.scalar(select(func.count()).select_from(DecisionLog))
    changed = tmp_path / "community.yaml"
    changed.write_text(example().replace("threshold_pct: 50", "threshold_pct: 80"), "utf-8")
    community.seed_once(clean_db, changed, LOG, signer)
    with Session(clean_db) as db:
        assert community.get(db, "voting")["threshold_pct"] == 50
        assert db.scalar(select(func.count()).select_from(DecisionLog)) == before


def test_invalid_seed_writes_nothing(clean_db, signer, tmp_path):
    bad = tmp_path / "community.yaml"
    bad.write_text(example().replace("threshold_pct: 50", "threshold_pct: 150"), encoding="utf-8")
    with pytest.raises(community.CommunityConfigError, match="threshold_pct"):
        community.seed_once(clean_db, bad, LOG, signer)
    with Session(clean_db) as db:
        assert not community.is_seeded(db)
        assert categories.list_all(db) == []
        assert dl.load_entries(db) == []


OLD_VOTING = {"default_duration_days": 7, "quorum_pct": 50, "threshold_pct": 50}


def _store_old_voting(clean_db):
    with Session(clean_db) as db, db.begin():
        row = db.get(ConfigKV, "voting")
        row.value_json = OLD_VOTING


def test_outdated_voting_rules_are_replaced_once_and_signed(clean_db, signer):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    _store_old_voting(clean_db)  # what a box seeded before vote types holds
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        voting = community.get(db, "voting")
        assert [t["slug"] for t in voting["types"]] == ["spending", "rules", "events"]
        last = dl.load_entries(db)[-1]
        assert last["kind"] == "config.changed"
        assert last["payload"]["key"] == "voting"
        assert last["payload"]["old"] == OLD_VOTING
        assert last["payload"]["new"] == voting
        assert dl.verify_db(db).ok
        count = db.scalar(select(func.count()).select_from(DecisionLog))
    community.seed_once(clean_db, EXAMPLE, LOG, signer)  # current rules: left alone
    with Session(clean_db) as db:
        assert db.scalar(select(func.count()).select_from(DecisionLog)) == count


def _delete_voting(clean_db):
    with Session(clean_db) as db, db.begin():
        db.delete(db.get(ConfigKV, "voting"))


def test_missing_voting_rules_are_added_once_and_signed(clean_db, signer):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    _delete_voting(clean_db)  # a box seeded before voting rules were copied
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        voting = community.get(db, "voting")
        assert [t["slug"] for t in voting["types"]] == ["spending", "rules", "events"]
        last = dl.load_entries(db)[-1]
        assert last["kind"] == "config.changed"
        assert last["payload"]["key"] == "voting"
        assert last["payload"]["old"] is None
        assert last["payload"]["new"] == voting
        assert dl.verify_db(db).ok
        count = db.scalar(select(func.count()).select_from(DecisionLog))
    community.seed_once(clean_db, EXAMPLE, LOG, signer)  # now current: left alone
    with Session(clean_db) as db:
        assert db.scalar(select(func.count()).select_from(DecisionLog)) == count


def test_missing_rules_and_a_file_without_voting_do_not_stop_the_box(clean_db, signer, tmp_path):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    _delete_voting(clean_db)
    no_voting = tmp_path / "community.yaml"
    no_voting.write_text(example().split("\nvoting:")[0] + "\n", encoding="utf-8")
    community.seed_once(clean_db, no_voting, LOG, signer)  # logs a warning, raises nothing
    with Session(clean_db) as db:
        assert community.get(db, "voting") is None


def test_current_voting_rules_are_never_overwritten_from_the_file(clean_db, signer, tmp_path):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    changed = tmp_path / "community.yaml"
    changed.write_text(example().replace("threshold_pct: 50", "threshold_pct: 80"), "utf-8")
    community.seed_once(clean_db, changed, LOG, signer)
    with Session(clean_db) as db:
        assert community.get(db, "voting")["threshold_pct"] == 50


def test_outdated_rules_and_a_broken_file_do_not_stop_the_box(clean_db, signer, tmp_path):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    _store_old_voting(clean_db)
    bad = tmp_path / "community.yaml"
    bad.write_text("voting: [", encoding="utf-8")
    community.seed_once(clean_db, bad, LOG, signer)  # logs a warning, raises nothing
    with Session(clean_db) as db:
        assert community.get(db, "voting") == OLD_VOTING


# --- app startup ------------------------------------------------------------


def test_app_start_creates_key_seeds_and_keeps_both(settings, fake_llm):
    seeded = dataclasses.replace(settings, community_file=EXAMPLE)
    first = create_app(seeded, require_secrets=False, llm=fake_llm)
    with TestClient(first, base_url="https://commons.example.test"):
        key_id = first.state.log_signer.key_id
    assert (settings.key_dir / dl.KEY_FILE).exists()
    with first.state.sessionmaker() as db:
        entries = len(dl.load_entries(db))
        assert set(dl.public_keys(db)) == {key_id}
    first.state.engine.dispose()

    again = create_app(seeded, require_secrets=False, llm=fake_llm)
    with TestClient(again, base_url="https://commons.example.test"):
        assert again.state.log_signer.key_id == key_id
    with again.state.sessionmaker() as db:
        assert len(dl.load_entries(db)) == entries
        assert dl.verify_db(db).ok
    again.state.engine.dispose()


# --- bookable things and booking settings ----------------------------------------

LEGACY = """\
resource_categories:
  - {slug: water, name: Water, unit: l, kind: stock}
spaces:
  - {slug: theatre, name: Theatre}
  - {slug: workshop, name: Workshop, description: Tools and benches}
"""


def test_bookings_sections_are_checked():
    assert "must be less than or equal to 24" in problems(
        example().replace("open_booking_hours: 2", "open_booking_hours: 48")
    )
    # Unquoted 22:00 is a number in YAML; the message says to quote it.
    assert 'use a time like "22:00", in quotes' in problems(
        example().replace('quiet_hours_start: "22:00"', "quiet_hours_start: 22:00")
    )
    clash = LEGACY + "bookable:\n  - {name: THEATRE, type: space}\n"
    assert "the name 'Theatre' is used for two bookable things" in problems(clash)


def test_seed_creates_bookable_and_settings(clean_db, signer):
    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        things = {t.slug: (t.type, t.location) for t in bookables.list_active(db)}
        assert things == {
            "theatre": ("space", None),
            "workshop": ("space", None),
            "car": ("vehicle", "the barn"),
            "saw": ("tool", "the workshop"),
        }
        assert bookables.settings(db).quiet_hours_start == "22:00"
        assert community.bookings_seeded(db)
        assert community.get(db, "spaces") is None


def test_old_spaces_list_becomes_type_space(clean_db, signer, tmp_path):
    legacy = tmp_path / "community.yaml"
    legacy.write_text(LEGACY, encoding="utf-8")
    community.seed_once(clean_db, legacy, LOG, signer)
    with Session(clean_db) as db:
        assert [(t.slug, t.type, t.description) for t in bookables.list_active(db)] == [
            ("theatre", "space", ""),
            ("workshop", "space", "Tools and benches"),
        ]


def test_box_seeded_before_bookings_gets_only_the_bookings(clean_db, signer, monkeypatch):
    from app import bookables

    with monkeypatch.context() as m:  # what a box seeded before G1 looks like
        m.setattr(community, "seed_bookings", lambda *a: None)
        community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        assert not community.bookings_seeded(db)
        before = len(dl.load_entries(db))

    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        assert [c.slug for c in categories.list_all(db)] == ["water", "firewood"]
        assert len(bookables.list_active(db)) == 4
        added = dl.load_entries(db)[before:]
        assert [e["payload"]["key"] for e in added] == ["bookable", "bookings"]
        assert dl.verify_db(db).ok

    community.seed_once(clean_db, EXAMPLE, LOG, signer)  # and only once
    with Session(clean_db) as db:
        assert len(dl.load_entries(db)) == before + 2


def test_box_seeded_before_the_guide_gets_only_the_guide(clean_db, signer, monkeypatch):
    with monkeypatch.context() as m:  # what a box seeded before the guide looks like
        m.setattr(community, "seed_guide", lambda *a: None)
        community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        assert not community.guide_seeded(db)
        assert community.guide_for_prompt(db) == ""
        before = len(dl.load_entries(db))

    community.seed_once(clean_db, EXAMPLE, LOG, signer)
    with Session(clean_db) as db:
        added = dl.load_entries(db)[before:]
        assert [e["payload"]["key"] for e in added] == [community.GUIDE_KEY]
        assert "## Sauna\n" in community.guide_for_prompt(db)
        assert len(bookables.list_active(db)) == 4  # bookings not seeded twice
        assert dl.verify_db(db).ok

    community.seed_once(clean_db, EXAMPLE, LOG, signer)  # and only once
    with Session(clean_db) as db:
        assert len(dl.load_entries(db)) == before + 1
