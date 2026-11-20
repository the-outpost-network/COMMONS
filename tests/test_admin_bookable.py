"""The Bookable settings page, the service behind it, and config.changed logging.

Covers ids made from names, unique active names, archive/restore, delete only
without bookings, the form, and that every change, resource categories
included, is signed into the decision log with who made it. Who may call which
route is in tests/test_authz_matrix.py.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select
from tests.conftest import SERVER, sign_in

from app import bookables, calendar, categories, decision_log, members
from app.bookables import ThingSpec
from app.models import Bookable, DecisionLog, utcnow

ADMIN = f"@ada:{SERVER}"


def csrf(client) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text).group(1)


@pytest.fixture
def admin(client, fake_matrix, db, log_signer):
    client.member = members.add_member(db, ADMIN, "Ada", ("admin",), signer=log_signer)
    db.commit()
    fake_matrix.user_id = ADMIN
    assert sign_in(client, fake_matrix).status_code == 303
    client.token = csrf(client)
    return client


@pytest.fixture
def saw(db):
    thing = bookables.create(db, ThingSpec(name="Saw", type="tool", location="the shed"))
    db.commit()
    return thing


def changes(db) -> list[DecisionLog]:
    db.expire_all()
    q = select(DecisionLog).where(DecisionLog.kind == "config.changed").order_by(DecisionLog.seq)
    return list(db.scalars(q))


def thing(**kw) -> dict:
    return {"name": "Car", "type": "vehicle", "location": "the barn", "description": "", **kw}


def post(client, url, data=None):
    return client.post(
        url, data={**(data or {}), "csrf_token": client.token}, follow_redirects=False
    )


# -- service -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Big saw", "big-saw"),
        ("Küche", "kuche"),
        ("3D printer", "thing-3d-printer"),
        ("!!", "thing"),
    ],
)
def test_the_id_is_made_from_the_name(db, name, slug):
    assert bookables.create(db, ThingSpec(name=name, type="tool")).slug == slug


def test_ids_stay_unique_and_fixed(db, saw):
    bookables.archive(db, saw)
    second = bookables.create(db, ThingSpec(name="Saw", type="tool"))
    assert second.slug == "saw-2"
    bookables.update(db, second, ThingSpec(name="Hand saw", type="tool"))
    assert second.slug == "saw-2"


def test_active_names_are_unique_any_case(db, saw):
    with pytest.raises(bookables.BookablesError) as e:
        bookables.create(db, ThingSpec(name="SAW", type="tool"))
    assert e.value.field == "name"


def test_restore_checks_the_name(db, saw):
    bookables.archive(db, saw)
    bookables.create(db, ThingSpec(name="saw", type="tool"))
    with pytest.raises(bookables.BookablesError, match="already called"):
        bookables.restore(db, saw)


def test_delete_only_without_bookings(db, saw, nora):
    start = utcnow() + timedelta(days=1)
    calendar.book(db, nora, saw, start, start + timedelta(hours=2), channel="web")
    db.commit()
    with pytest.raises(bookables.HasHistory):
        bookables.delete(db, saw)
    car = bookables.create(db, ThingSpec(name="Car", type="vehicle"))
    car_id = car.id
    bookables.delete(db, car)
    db.commit()
    assert db.get(Bookable, car_id) is None


# -- the page -------------------------------------------------------------------


def test_list_page(admin, saw):
    page = admin.get("/admin/bookable").text
    for part in ("Saw", "Tool", "the shed", "/admin/bookable/new"):
        assert part in page
    assert 'aria-current="page">Things to book' in page
    for gone in ("Booking kinds", "Booking purposes", "Pickup"):
        assert gone not in page


def test_the_form_asks_four_things(admin):
    page = admin.get("/admin/bookable/new").text
    for field in ('name="name"', 'name="type"', 'name="location"', 'name="description"'):
        assert field in page
    for gone in ('name="slug"', 'name="mode"', 'name="kind"', "purpose"):
        assert gone not in page


def test_add_a_car_logs_who_and_what(admin, db):
    r = post(admin, "/admin/bookable", thing(location=" the  barn "))
    assert (r.status_code, r.headers["location"]) == (303, "/admin/bookable?done=created")
    car = bookables.by_slug(db, "car")
    assert (car.name, car.type, car.location) == ("Car", "vehicle", "the barn")
    (entry,) = changes(db)
    assert entry.actor_member_id == admin.member.id
    assert entry.payload_json == {
        "key": "bookable.car",
        "old": None,
        "new": bookables.values(car),
        "source": "web",
    }
    assert "Thing added." in admin.get("/admin/bookable?done=created").text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"type": "boat"}, "Must be space, vehicle, tool"),
        ({"name": ""}, "Required."),
        ({"name": "x" * 61}, "At most 60 characters."),
        ({"name": "saw"}, "already called"),
    ],
)
def test_form_errors_save_and_log_nothing(admin, db, saw, change, message):
    r = post(admin, "/admin/bookable", thing(**change))
    assert r.status_code in (400, 409)
    assert message in r.text
    assert len(bookables.list_all(db)) == 1
    assert changes(db) == []


def test_names_are_escaped(admin, db):
    post(admin, "/admin/bookable", thing(name="<b>x</b>"))
    page = admin.get("/admin/bookable").text
    assert "<b>x</b>" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page


def test_edit_logs_old_and_new_and_no_change_logs_nothing(admin, db, saw):
    before = bookables.values(saw)
    unchanged = {"name": "Saw", "type": "tool", "location": "the shed", "description": ""}
    assert post(admin, f"/admin/bookable/{saw.id}", unchanged).status_code == 303
    assert changes(db) == []
    post(admin, f"/admin/bookable/{saw.id}", {**unchanged, "location": "the barn"})
    (entry,) = changes(db)
    assert entry.payload_json["key"] == "bookable.saw"
    assert (entry.payload_json["old"], entry.payload_json["new"]["location"]) == (
        before,
        "the barn",
    )


def test_archive_restore_delete_are_logged(admin, db, saw):
    saw_id = saw.id
    url = f"/admin/bookable/{saw_id}"
    assert post(admin, f"{url}/archive").headers["location"] == "/admin/bookable?done=archived"
    assert "archived" in admin.get("/admin/bookable").text
    post(admin, f"{url}/restore")
    assert "for good" in admin.get(f"{url}/delete").text
    assert post(admin, f"{url}/delete").headers["location"] == "/admin/bookable?done=deleted"
    db.expire_all()
    assert db.get(Bookable, saw_id) is None
    news = [e.payload_json["new"] for e in changes(db)]
    assert [n and n["archived"] for n in news] == [True, False, None]
    assert decision_log.verify_db(db).ok


def test_deleting_a_booked_thing_is_refused(admin, db, saw, nora):
    start = utcnow() + timedelta(days=1)
    calendar.book(db, nora, saw, start, channel="web")
    db.commit()
    r = post(admin, f"/admin/bookable/{saw.id}/delete")
    assert r.status_code == 409
    assert "archive it instead" in r.text
    assert changes(db) == []


def test_unknown_id_is_404(admin):
    assert admin.get("/admin/bookable/999/edit").status_code == 404
    assert post(admin, "/admin/bookable/999/archive").status_code == 404


# -- resource categories are logged too -------------------------------------------


def test_resource_category_changes_are_logged(admin, db):
    form = {"slug": "gas", "name": "Gas", "unit": "bottles", "kind": "stock",
            "shortage_threshold": "", "capacity": "", "forecast_horizon_days": "30",
            "aliases": ""}  # fmt: skip
    assert post(admin, "/admin/resources", form).status_code == 303
    gas = categories.by_slug(db, "gas")
    post(admin, f"/admin/resources/{gas.id}", {**form, "shortage_threshold": "2"})
    post(admin, f"/admin/resources/{gas.id}/move", {"direction": "down"})  # order: not logged
    post(admin, f"/admin/resources/{gas.id}/archive")
    post(admin, f"/admin/resources/{gas.id}/restore")
    post(admin, f"/admin/resources/{gas.id}/delete")
    entries = changes(db)
    assert {e.payload_json["key"] for e in entries} == {"resource_categories.gas"}
    assert {e.actor_member_id for e in entries} == {admin.member.id}
    payloads = [e.payload_json for e in entries]
    assert payloads[0]["old"] is None
    assert (payloads[1]["old"]["shortage_threshold"], payloads[1]["new"]["shortage_threshold"]) == (
        None,
        2,
    )
    assert [p["new"] and p["new"]["archived"] for p in payloads[2:]] == [True, False, None]
    assert decision_log.verify_db(db).ok
