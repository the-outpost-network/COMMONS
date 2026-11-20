"""Resource categories: service rules, community.yaml seeding, admin pages, board."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.conftest import SERVER, sign_in

from app import categories, community, decision_log, members
from app.i18n import en
from app.main import create_app
from app.models import ConfigKV, ResourceAlias, ResourceCategory

EXAMPLE = Path(__file__).parent.parent / "community.yaml.example"
ADMIN = f"@ada:{SERVER}"
Spec = categories.CategorySpec


def water(**kw) -> Spec:
    return Spec(**{"slug": "water", "name": "Water", "unit": "l", "kind": "stock", **kw})


def wood(**kw) -> Spec:
    return Spec(**{"slug": "firewood", "name": "Firewood", "unit": "m³", "kind": "stock", **kw})


# -- the spec (shared by community.yaml and the admin form) -------------------


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"slug": "Water"}, "slug"),
        ({"slug": "w"}, "slug"),
        ({"slug": "9lives"}, "slug"),
        ({"kind": "liquid"}, "kind"),
        ({"unit": ""}, "unit"),
        ({"name": "x" * 61}, "name"),
        ({"shortage_threshold": -1}, "shortage_threshold"),
        ({"capacity": 0}, "capacity"),
        ({"forecast_horizon_days": 0}, "forecast_horizon_days"),
        ({"aliases": ["a/b"]}, "aliases"),
        ({"aliases": [f"w{i}" for i in range(11)]}, "aliases"),
        ({"shortage_threshold": 500, "capacity": 500}, ""),
        ({"colour": "blue"}, "colour"),
    ],
)
def test_spec_rejects(change, field):
    data = {"slug": "water", "name": "Water", "unit": "l", "kind": "stock", **change}
    with pytest.raises(categories.ValidationError) as e:
        Spec(**data)
    assert field in categories.spec_errors(e.value)


def test_spec_normalizes_aliases():
    s = water(aliases=["  Tank ", "tank", "WATER", "drinking   water", ""])
    assert s.aliases == ["tank", "drinking water"]  # own name dropped, duplicates merged


# -- service --------------------------------------------------------------------


def test_create_list_and_resolve(db):
    w = categories.create(db, water(aliases=["tank"]))
    f = categories.create(db, wood(aliases=["wood", "logs"]))
    db.commit()
    assert [c.slug for c in categories.list_active(db)] == ["water", "firewood"]
    assert categories.resolve(db, "  WOOD ") == f
    assert categories.resolve(db, "Firewood") == f
    assert categories.resolve(db, "tank") == w
    assert categories.resolve(db, "petrol") is None
    assert categories.by_slug(db, "water") == w


@pytest.mark.parametrize(
    "clash",
    [
        wood(slug="water"),
        wood(name="WATER"),
        wood(aliases=["Tank"]),
        wood(aliases=["water"]),
        Spec(slug="tank", name="Tank", unit="l", kind="stock"),
    ],
)
def test_every_word_means_one_category(db, clash):
    categories.create(db, water(aliases=["tank"]))
    with pytest.raises(categories.CategoryError):
        categories.create(db, clash)


def test_alias_unique_in_the_database_too(db):
    """Even if the app checks were bypassed, PostgreSQL refuses a duplicate alias."""
    w = categories.create(db, water())
    f = categories.create(db, wood())
    db.add(ResourceAlias(alias="tank", category_id=w.id))
    db.flush()
    db.add(ResourceAlias(alias="tank", category_id=f.id))
    with pytest.raises(IntegrityError):
        db.flush()


def test_kind_checked_in_the_database(db):
    db.add(ResourceCategory(slug="x1", name="X", unit="l", kind="liquid"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_update_keeps_the_slug(db):
    w = categories.create(db, water(aliases=["tank"]))
    categories.update(db, w, water(name="Water tank", unit="L", aliases=["cistern"]))
    db.commit()
    db.refresh(w)
    assert (w.slug, w.name, w.unit, w.alias_names) == ("water", "Water tank", "L", ["cistern"])
    with pytest.raises(categories.CategoryError):
        categories.update(db, w, water(slug="h2o"))


def test_archive_hides_and_restore_checks_clashes(db):
    w = categories.create(db, water(aliases=["tank"]))
    categories.archive(db, w)
    assert categories.list_active(db) == []
    assert categories.resolve(db, "tank") is None
    assert categories.list_all(db) == [w]  # still there, history stays
    # Archived words still count: nobody else can take them meanwhile.
    with pytest.raises(categories.CategoryError):
        categories.create(db, wood(aliases=["tank"]))
    categories.restore(db, w)
    assert categories.list_active(db) == [w]


def test_move_changes_the_order(db):
    w = categories.create(db, water())
    f = categories.create(db, wood())
    categories.move(db, f, -1)
    assert categories.list_active(db) == [f, w]
    categories.move(db, f, -1)  # already first: nothing happens
    assert categories.list_active(db) == [f, w]


def test_delete_without_history(db):
    w = categories.create(db, water(aliases=["tank"]))
    categories.delete(db, w)
    db.commit()
    assert categories.list_all(db) == []
    assert db.query(ResourceAlias).count() == 0


def test_delete_refused_once_something_refers_to_it(db):
    """Stand-in for ledger entries (D2): any row pointing at the category."""
    w = categories.create(db, water())
    # A real table (PostgreSQL won't let a temp table reference this one); the
    # session is never committed, so the table goes away with the rollback.
    db.execute(text("CREATE TABLE entry_stub (category_id int REFERENCES resource_category(id))"))
    db.execute(text("INSERT INTO entry_stub VALUES (:id)"), {"id": w.id})
    with pytest.raises(categories.HasHistory):
        categories.delete(db, w)
    assert categories.get(db, w.id) is not None


# -- community.yaml -------------------------------------------------------------


def log_signer(engine, tmp_path):
    from sqlalchemy.orm import Session

    with Session(engine) as db, db.begin():
        return decision_log.init_signer(db, tmp_path / "keys")


def test_example_file_is_valid():
    config = community.load(EXAMPLE)
    assert [c.slug for c in config.resource_categories] == ["water", "firewood"]


def _yaml(categories_yaml: str, extra: str = "") -> str:
    return f"community:\n  name: Test Village\n{extra}resource_categories:\n{categories_yaml}"


OK_YAML = _yaml(
    "  - {slug: water, name: Water, unit: l, kind: stock, aliases: [tank]}\n"
    "  - {slug: firewood, name: Firewood, unit: m³, kind: stock}\n"
)


@pytest.mark.parametrize(
    ("text_", "message"),
    [
        (
            _yaml(
                "  - {slug: water, name: Water, unit: l, kind: stock}\n"
                "  - {slug: firewood, name: Firewood, unit: m³, kind: pile}\n"
            ),
            "resource_categories[1] (firewood).kind: must be stock or flow",
        ),
        (
            _yaml(
                "  - {slug: water, name: Water, unit: l, kind: stock, aliases: [wood]}\n"
                "  - {slug: firewood, name: Firewood, unit: m³, kind: stock, aliases: [Wood]}\n"
            ),
            "resource_categories[1] (firewood): 'wood' already means water",
        ),
        (
            _yaml("  - {slug: water, name: Water, unit: l, kind: stock, colour: blue}\n"),
            "resource_categories[0] (water).colour",
        ),
        (
            _yaml("  - {slug: water, name: Water, unit: l, kind: stock}\n", "gardens: []\n"),
            "gardens",
        ),
        ("resource_categories: [\n", "not valid YAML"),
        ("community:\n  name: X\n", "resource_categories: Field required"),
        (_yaml(""), "resource_categories"),
    ],
)
def test_invalid_file_says_where(text_, message):
    with pytest.raises(community.CommunityConfigError) as e:
        community.parse(text_, "community.yaml")
    assert message in str(e.value)


def test_seed_once(pg_engine, clean_db, tmp_path):
    f = tmp_path / "community.yaml"
    f.write_text(OK_YAML)
    log = logging.getLogger("test")
    community.seed_once(pg_engine, f, log, log_signer(pg_engine, tmp_path))
    # Afterwards the file is not read at all: a broken or missing file changes nothing.
    f.write_text("resource_categories: [\n")
    community.seed_once(pg_engine, f, log, log_signer(pg_engine, tmp_path))
    f.unlink()
    community.seed_once(pg_engine, f, log, log_signer(pg_engine, tmp_path))
    from sqlalchemy.orm import Session

    with Session(pg_engine) as db:
        assert [c.slug for c in categories.list_all(db)] == ["water", "firewood"]
        assert db.get(ConfigKV, community.SEEDED_KEY) is not None
        assert community.community_name(pg_engine) == "Test Village"


def test_missing_file_before_seeding_is_an_error(pg_engine, clean_db, tmp_path):
    with pytest.raises(community.CommunityConfigError, match="make init"):
        community.seed_once(
            pg_engine,
            tmp_path / "community.yaml",
            logging.getLogger("t"),
            log_signer(pg_engine, tmp_path),
        )


def test_app_refuses_to_start_with_a_broken_file(settings, tmp_path, fake_matrix):
    f = tmp_path / "community.yaml"
    f.write_text(_yaml("  - {slug: water, name: Water, unit: l, kind: pile}\n"))
    app = create_app(dataclasses.replace(settings, community_file=f), require_secrets=False)

    async def start():
        async with app.router.lifespan_context(app):
            pass

    try:
        with pytest.raises(SystemExit, match=r"resource_categories\[0\] \(water\)\.kind"):
            asyncio.run(start())
    finally:
        app.state.engine.dispose()


def test_app_seeds_on_start_and_shows_the_name(settings, tmp_path, fake_matrix, fake_llm):
    import httpx

    f = tmp_path / "community.yaml"
    f.write_text(OK_YAML)
    app = create_app(
        dataclasses.replace(settings, community_file=f),
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake_matrix.handler)),
        require_secrets=False,
        llm=fake_llm,
        internal_token="t",
    )
    with TestClient(app, base_url="https://commons.example.test") as c:
        assert "Test Village" in c.get("/").text
    app.state.engine.dispose()


# -- admin pages ----------------------------------------------------------------


def csrf(client) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text).group(1)


@pytest.fixture
def admin(client, fake_matrix, db, log_signer):
    members.add_member(db, ADMIN, "Ada", ("admin",), signer=log_signer)
    db.commit()
    fake_matrix.user_id = ADMIN
    assert sign_in(client, fake_matrix).status_code == 303
    client.token = csrf(client)
    return client


def form(**kw) -> dict:
    return {
        "slug": "gas",
        "name": "Gas bottles",
        "unit": "bottles",
        "kind": "stock",
        "shortage_threshold": "2",
        "capacity": "6",
        "forecast_horizon_days": "30",
        "aliases": "gas, propane",
        **kw,
    }


def test_list_page(admin, db):
    categories.create(db, water(aliases=["tank"]))
    db.commit()
    page = admin.get("/admin/resources").text
    for part in ("Water", "tank", "/admin/resources/new"):
        assert part in page
    assert 'aria-current="page">Resources' in page


def test_create_from_the_form(admin, db):
    r = admin.post(
        "/admin/resources", data={**form(), "csrf_token": admin.token}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/resources?done=created"
    gas = categories.by_slug(db, "gas")
    assert (gas.name, gas.shortage_threshold, gas.capacity) == ("Gas bottles", 2, 6)
    assert gas.alias_names == ["propane"]  # "gas" is the slug already
    assert "Resource added." in admin.get("/admin/resources?done=created").text


@pytest.mark.parametrize(
    ("change", "field_error"),
    [
        ({"slug": "Gas!"}, "Use 2 to 32 lower-case letters"),
        ({"capacity": "lots"}, "Enter a number"),
        ({"forecast_horizon_days": "2.5"}, "whole days"),
        ({"shortage_threshold": "9", "capacity": "6"}, "lower than the capacity"),
        ({"kind": "pile"}, "Must be stock or flow."),
        ({"name": ""}, "Required."),
        ({"name": "x" * 61}, "At most 60 characters."),
    ],
)
def test_form_errors_are_shown_and_nothing_is_saved(admin, db, change, field_error):
    r = admin.post("/admin/resources", data={**form(**change), "csrf_token": admin.token})
    assert r.status_code == 400
    assert field_error in r.text
    assert categories.list_all(db) == []


def test_form_clash_and_escaping(admin, db):
    categories.create(db, water(aliases=["tank"]))
    db.commit()
    r = admin.post(
        "/admin/resources",
        data={**form(aliases="<b>x</b>, tank"), "csrf_token": admin.token},
    )
    assert r.status_code in (400, 409)
    assert "<b>x</b>" not in r.text


def test_edit_keeps_the_slug_whatever_is_sent(admin, db):
    w = categories.create(db, water())
    db.commit()
    assert 'value="water" readonly' in admin.get(f"/admin/resources/{w.id}/edit").text
    r = admin.post(
        f"/admin/resources/{w.id}",
        data={**form(slug="hacked", name="Water tank", unit="l"), "csrf_token": admin.token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    w = categories.get(db, w.id)
    assert (w.slug, w.name) == ("water", "Water tank")


def test_archive_restore_move_delete(admin, db):
    w = categories.create(db, water())
    f = categories.create(db, wood())
    db.commit()
    w_id = w.id
    tok = {"csrf_token": admin.token}
    admin.post(f"/admin/resources/{f.id}/move", data={**tok, "direction": "up"})
    db.expire_all()
    assert [c.slug for c in categories.list_active(db)] == ["firewood", "water"]
    admin.post(f"/admin/resources/{w.id}/archive", data=tok)
    db.expire_all()
    assert not categories.get(db, w.id).is_active
    assert "archived" in admin.get("/admin/resources").text
    admin.post(f"/admin/resources/{w.id}/restore", data=tok)
    db.expire_all()
    assert categories.get(db, w.id).is_active
    assert "for good" in admin.get(f"/admin/resources/{w.id}/delete").text
    r = admin.post(f"/admin/resources/{w.id}/delete", data=tok, follow_redirects=False)
    assert r.headers["location"] == "/admin/resources?done=deleted"
    db.expire_all()
    assert categories.get(db, w_id) is None


def test_move_rejects_other_directions(admin, db):
    w = categories.create(db, water())
    db.commit()
    r = admin.post(
        f"/admin/resources/{w.id}/move", data={"csrf_token": admin.token, "direction": "sideways"}
    )
    assert r.status_code == 400


def test_unknown_resource_is_404(admin):
    assert admin.get("/admin/resources/999/edit").status_code == 404
    assert (
        admin.post("/admin/resources/999/archive", data={"csrf_token": admin.token}).status_code
        == 404
    )


# -- the board and the stylesheet -----------------------------------------------


def test_board_shows_active_resources_to_members(client, fake_matrix, db, nora):
    categories.create(db, water(shortage_threshold=1000, capacity=3000))
    old = categories.create(db, wood())
    categories.archive(db, old)
    db.commit()
    assert "Water" not in client.get("/").text  # signed out: sign-in page only
    sign_in(client, fake_matrix)
    page = client.get("/").text
    for part in ("Water", en.BOARD_NOTHING_LOGGED, "Low below 1,000 l"):
        assert part in page
    assert "Firewood" not in page
    assert 'href="/admin/resources"' not in page  # the Settings tab is for admins only


def test_stylesheet_is_served_locally(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "--c-brand" in r.text
    assert "http" not in r.text.split("*/", 1)[1]  # no CDN or external fonts
    assert client.get("/static/../app/main.py").status_code == 404
