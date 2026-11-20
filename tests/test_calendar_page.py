"""The calendar page, the booking form and booking page, and the phone-calendar (ICS) link.

Who may call which route is in tests/test_authz_matrix.py; the booking rules
themselves are in tests/test_calendar.py. Here: what the pages show, that the
web uses the same rules (and the same refusals) as chat, that members only touch
their own bookings and admins' changes are logged, and that the ICS link shows
only the member's own bookings and stops working when revoked.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import SERVER, sign_in

from app import bookables, calendar_view, members, phone_calendar
from app import calendar as cal
from app.i18n import en
from app.models import Booking, ConfigKV, DecisionLog, utcnow

STOCKHOLM = ZoneInfo("Europe/Stockholm")
H = timedelta(hours=1)


def csrf(client) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text).group(1)


@pytest.fixture
def things(db):
    for name, type_, where in (
        ("Theatre", "space", None),
        ("Car", "vehicle", "the barn"),
        ("Saw", "tool", "the shed"),
    ):
        bookables.create(db, bookables.ThingSpec(name=name, type=type_, location=where))
    db.add(ConfigKV(key="community.timezone", value_json="Europe/Stockholm"))
    db.commit()
    return {t.slug: t for t in bookables.list_active(db)}


def _member(app, db, fake_matrix, log_signer, name: str, roles=()) -> TestClient:
    client = TestClient(app, base_url="https://commons.example.test")
    client.member = members.add_member(
        db, f"@{name.lower()}:{SERVER}", name, roles, signer=log_signer
    )
    db.commit()
    fake_matrix.user_id = f"@{name.lower()}:{SERVER}"
    assert sign_in(client, fake_matrix).status_code == 303
    client.token = csrf(client)
    return client


@pytest.fixture
def ana(app, client, db, fake_matrix, log_signer):
    return _member(app, db, fake_matrix, log_signer, "Ana")


@pytest.fixture
def bob(app, client, db, fake_matrix, log_signer):
    return _member(app, db, fake_matrix, log_signer, "Bob")


@pytest.fixture
def ada(app, client, db, fake_matrix, log_signer):
    return _member(app, db, fake_matrix, log_signer, "Ada", ("admin",))


def post(client, url, data=None):
    return client.post(
        url, data={**(data or {}), "csrf_token": client.token}, follow_redirects=False
    )


def local(t: datetime) -> str:
    return t.astimezone(STOCKHOLM).strftime("%Y-%m-%dT%H:%M")


def book(db, who, thing, start, end=None, **kw) -> Booking:
    b = cal.book(db, who, thing, start, end, channel="web", **kw)
    db.commit()
    return b


def tomorrow_at(hour: int) -> datetime:
    d = (utcnow().astimezone(STOCKHOLM) + timedelta(days=1)).date()
    return datetime(d.year, d.month, d.day, hour, tzinfo=STOCKHOLM)


# --- the view (pure) ---------------------------------------------------------------


def test_a_booking_shows_on_every_day_it_touches(db, things, ana):
    start = tomorrow_at(18)
    book(db, ana.member, things["theatre"], start, start + timedelta(days=2, hours=1))
    v = calendar_view.build(db, "month", start.date(), calendar_view.Filter(), STOCKHOLM)
    on = [d.day for w in v.weeks for d in w if d.items]
    assert on == [start.date() + timedelta(days=n) for n in range(3)]
    first, *_, last = [d for w in v.weeks for d in w if d.items]
    assert first.items[0].starts_today
    assert not first.items[0].ends_today
    assert last.items[0].ends_today
    assert not last.items[0].starts_today


def test_an_end_at_midnight_does_not_spill_into_the_next_day(db, things, ana):
    start = tomorrow_at(20)
    midnight = datetime.combine(start.date() + timedelta(days=1), datetime.min.time(), STOCKHOLM)
    book(db, ana.member, things["theatre"], start, midnight)
    v = calendar_view.build(db, "week", start.date(), calendar_view.Filter(), STOCKHOLM)
    assert [d.day for w in v.weeks for d in w if d.items] == [start.date()]


def test_month_grid_starts_on_monday_and_covers_the_month(db, things):
    v = calendar_view.build(db, "month", date(2026, 11, 13), calendar_view.Filter(), STOCKHOLM)
    days = [d for w in v.weeks for d in w]
    assert days[0].day == date(2026, 10, 26)
    assert days[0].day.weekday() == 0
    assert days[-1].day == date(2026, 12, 6)
    assert days[-1].day.weekday() == 6
    assert all(len(w) == 7 for w in v.weeks)
    assert [d.day.day for d in days if d.in_month][-1] == 30
    assert (v.prev, v.next) == (date(2026, 10, 1), date(2026, 12, 1))


def test_days_follow_vienna_time_across_daylight_saving(db, things, ana):
    """25 Oct 2026 has 25 hours in Stockholm; a booking late that day stays on that day."""
    when = datetime(2026, 10, 25, 22, 30, tzinfo=STOCKHOLM)
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    cal.book(db, ana.member, things["saw"], when, when + H, channel="web", now=now)
    db.commit()
    v = calendar_view.build(db, "week", when.date(), calendar_view.Filter(), STOCKHOLM, now=now)
    (day,) = [d for d in v.weeks[0] if d.items]
    assert day.day == date(2026, 10, 25)
    assert day.items[0].start.strftime("%H:%M") == "22:30"


def test_cancelled_bookings_do_not_show(db, things, ana):
    b = book(db, ana.member, things["saw"], tomorrow_at(10), tomorrow_at(12))
    cal.cancel(db, ana.member, b)
    db.commit()
    v = calendar_view.build(db, "week", tomorrow_at(10).date(), calendar_view.Filter(), STOCKHOLM)
    assert not any(d.items for d in v.weeks[0])


def test_filter_by_type_and_thing(db, things, ana):
    start = tomorrow_at(9)
    for slug in ("car", "saw", "theatre"):
        book(db, ana.member, things[slug], start, start + H)

    def shown(flt):
        v = calendar_view.build(db, "week", start.date(), flt, STOCKHOLM)
        return sorted(i.booking.bookable.slug for d in v.weeks[0] for i in d.items)

    assert shown(calendar_view.Filter()) == ["car", "saw", "theatre"]
    assert shown(calendar_view.make_filter(db, "tool", None)) == ["saw"]
    assert shown(calendar_view.make_filter(db, None, "car")) == ["car"]
    assert shown(calendar_view.make_filter(db, "boat", "nope")) == ["car", "saw", "theatre"]


def test_open_booking_shows_who_has_it_until_now(db, things, ana):
    """The saw someone took this morning: still shown with them, hours after the window."""
    now = utcnow()
    took = now - 5 * H
    b = Booking(
        bookable=things["saw"], member_id=ana.member.id, starts_at=took, ends_at=None,
        blocks_until=took + 2 * H, channel="matrix", created_at=took,
    )  # fmt: skip
    db.add(b)
    db.commit()
    v = calendar_view.build(db, "week", now.astimezone(STOCKHOLM).date(), calendar_view.Filter(),
                            STOCKHOLM, now=now)  # fmt: skip
    assert [h.id for h in v.holders] == [b.id]
    today = next(d for d in v.weeks[0] if d.is_today)
    assert today.items[0].holding
    assert calendar_view.shown_end(b, now) == now


# --- the calendar page ---------------------------------------------------------------


def test_calendar_page_shows_bookings_to_every_member(db, things, ana, bob):
    start = tomorrow_at(15)
    book(db, ana.member, things["car"], start, start + 2 * H, note="shopping run",
         destination="town")  # fmt: skip
    page = bob.get(f"/calendar?view=week&day={start.date().isoformat()}").text
    for part in ("Car", "ana", "→ town", "shopping run", "15:00", "17:00"):
        assert part in page, part
    assert 'aria-current="page">Calendar' in page


def test_month_view_and_filters_render(ana, things):
    page = ana.get("/calendar?view=month&day=2026-11-13&type=vehicle").text
    assert "November 2026" in page
    assert re.search(
        r'class="chip on" aria-current="true"><span class="dot vehicle"[^>]*><\/span>Vehicles', page
    )
    assert ana.get("/calendar?view=nonsense&day=bad&thing=<x>").status_code == 200


def test_who_has_what_now_is_shown(db, things, ana, bob):
    book(db, ana.member, things["saw"], utcnow())
    page = bob.get("/calendar").text
    assert "Saw is with ana since" in page


def test_notes_are_escaped(db, things, ana):
    start = tomorrow_at(10)
    book(db, ana.member, things["saw"], start, start + H, note="<script>x</script>")
    page = ana.get(f"/calendar?view=week&day={start.date().isoformat()}").text
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page


# --- booking on the web ------------------------------------------------------------


def test_book_from_the_form(db, things, ana):
    form = ana.get("/calendar/new?thing=car").text
    assert '<option value="car" selected' in form
    start = tomorrow_at(10)
    data = {"thing": "car", "start": local(start), "end": local(start + 2 * H),
            "note": "to the dump", "destination": "town"}  # fmt: skip
    r = post(ana, "/calendar/bookings", data)
    assert r.status_code == 303
    booking_id = int(re.search(r"/calendar/bookings/(\d+)\?done=booked", r.headers["location"])[1])
    b = db.get(Booking, booking_id)
    assert (b.member_id, b.channel, b.note, b.destination) == (
        ana.member.id, "web", "to the dump", "town",
    )  # fmt: skip
    assert b.starts_at == start
    assert "Booked." in ana.get(r.headers["location"]).text


def test_empty_start_means_now_and_no_end_holds_it(db, things, ana):
    r = post(ana, "/calendar/bookings", {"thing": "saw"})
    assert r.status_code == 303
    (b,) = db.scalars(select(Booking)).all()
    assert abs((b.starts_at - utcnow()).total_seconds()) < 60
    assert b.ends_at is None


def test_a_clash_says_who_has_it_and_keeps_the_form(db, things, ana, bob):
    book(db, ana.member, things["saw"], utcnow())
    r = post(bob, "/calendar/bookings", {"thing": "saw", "note": "fence"})
    assert r.status_code == 409
    assert "Saw is with ana since" in r.text
    assert re.search(r'<a href="/calendar\?view=week&amp;day=[0-9-]+&amp;thing=saw">See ', r.text)
    assert 'value="fence"' in r.text
    assert len(db.scalars(select(Booking)).all()) == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"thing": ""}, "Pick what you want to book."),
        ({"thing": "saw", "destination": "town"}, "Only a vehicle booking says where"),
        ({"thing": "saw", "start": "tomorrow"}, "The start isn&#39;t a valid time."),
        ({"thing": "saw", "end": "2020-01-01T10:00"}, "The end has to be after the start."),
    ],
)
def test_form_errors_book_nothing(db, things, ana, data, message):
    r = post(ana, "/calendar/bookings", data)
    assert r.status_code == 400
    assert message in r.text
    assert db.scalars(select(Booking)).all() == []


def test_change_extend_and_cancel_your_own(db, things, ana):
    start = tomorrow_at(10)
    b = book(db, ana.member, things["car"], start, start + H)
    page = ana.get(f"/calendar/bookings/{b.id}").text
    assert "Cancel booking" in page
    assert "Give it back" not in page
    r = post(ana, f"/calendar/bookings/{b.id}", {"start": local(start),
             "end": local(start + 3 * H), "note": "longer", "destination": "the lake"})  # fmt: skip
    assert r.headers["location"] == f"/calendar/bookings/{b.id}?done=changed"
    db.expire_all()
    assert (b.ends_at, b.note, b.destination) == (start + 3 * H, "longer", "the lake")
    post(ana, f"/calendar/bookings/{b.id}/cancel")
    db.expire_all()
    assert b.cancelled_at is not None
    assert b.cancelled_by == ana.member.id


def test_give_back_a_started_booking(db, things, ana):
    b = book(db, ana.member, things["saw"], utcnow())
    assert "Give it back" in ana.get(f"/calendar/bookings/{b.id}").text
    assert post(ana, f"/calendar/bookings/{b.id}/return").status_code == 303
    db.expire_all()
    assert b.returned_at is not None


def test_members_cannot_touch_others_bookings(db, things, ana, bob):
    start = tomorrow_at(10)
    b = book(db, ana.member, things["car"], start, start + H, note="mine")
    page = bob.get(f"/calendar/bookings/{b.id}").text
    assert "mine" in page  # everyone sees every booking
    assert "Cancel booking" not in page
    assert 'action="/calendar/bookings/' not in page
    assert post(bob, f"/calendar/bookings/{b.id}", {"end": "", "note": "x"}).status_code == 403
    assert post(bob, f"/calendar/bookings/{b.id}/cancel", {"reason": "x"}).status_code == 403
    assert post(bob, f"/calendar/bookings/{b.id}/return").status_code == 403
    db.expire_all()
    assert (b.note, b.ends_at, b.cancelled_at, b.returned_at) == ("mine", start + H, None, None)


def test_admin_changes_and_cancels_are_logged(db, things, ana, ada):
    start = tomorrow_at(10)
    b = book(db, ana.member, things["car"], start, start + H)
    page = ada.get(f"/calendar/bookings/{b.id}").text
    assert "as admin, written to the decision log" in page
    post(ada, f"/calendar/bookings/{b.id}", {"start": local(start), "end": local(start + 2 * H),
                                             "note": "", "destination": ""})  # fmt: skip
    r = post(ada, f"/calendar/bookings/{b.id}/cancel", {"reason": ""})
    assert r.status_code == 400
    assert "Please give a reason" in r.text
    post(ada, f"/calendar/bookings/{b.id}/cancel", {"reason": "the car is in the garage"})
    db.expire_all()
    assert b.cancel_reason == "the car is in the garage"
    kinds = list(db.scalars(select(DecisionLog.kind).order_by(DecisionLog.seq)))
    assert kinds[-2:] == ["booking.changed_by_admin", "booking.cancelled_by_admin"]


def test_unknown_booking_is_404(ana):
    assert ana.get("/calendar/bookings/999").status_code == 404
    assert post(ana, "/calendar/bookings/999/cancel").status_code == 404


# --- phone calendar -------------------------------------------------------------------


def _make_link(client) -> str:
    assert post(client, "/calendar/phone").headers["location"] == "/calendar/phone?done=created"
    page = client.get("/calendar/phone").text
    return re.search(r'id="f-link"[^>]*>https://[^<]+(/calendar/ics/[^<]+\.ics)</code>', page)[1]


def test_link_is_shown_once_and_feeds_only_your_bookings(db, things, ana, bob):
    start = tomorrow_at(10)
    book(db, ana.member, things["car"], start, start + H, note="vet; then, home",
         destination="town")  # fmt: skip
    book(db, bob.member, things["saw"], start, start + H, note="bob's")
    path = _make_link(ana)
    page = ana.get("/calendar/phone").text
    assert "/calendar/ics/" not in page  # shown once
    assert "Your current link was made" in page
    r = TestClient(ana.app, base_url="https://commons.example.test").get(path)  # no session
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert r.headers["cache-control"] == "no-store"
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:Car → town" in body
    assert "DESCRIPTION:vet\\; then\\, home\\nBooked in Commons." in body
    assert "LOCATION:the barn" in body
    assert f"DTSTART:{start.astimezone(UTC):%Y%m%dT%H%M%SZ}" in body
    assert "bob's" not in body
    assert "Saw" not in body


def test_revoke_and_remake_stop_the_old_link(db, things, ana):
    first = _make_link(ana)
    anon = TestClient(ana.app, base_url="https://commons.example.test")
    assert anon.get(first).status_code == 200
    second = _make_link(ana)
    assert anon.get(first).status_code == 404
    assert anon.get(second).status_code == 200
    post(ana, "/calendar/phone/revoke")
    assert anon.get(second).status_code == 404
    assert "Your link no longer works." in ana.get("/calendar/phone?done=revoked").text


def test_deactivated_members_link_stops(db, things, ana, log_signer):
    path = _make_link(ana)
    members.deactivate(db, ana.member, signer=log_signer, channel="web")
    db.commit()
    assert TestClient(ana.app, base_url="https://commons.example.test").get(path).status_code == 404


@pytest.mark.parametrize("bad", ["x", "A" * 43, "../../etc/passwd", "%00" * 30])
def test_unknown_tokens_are_404(client, bad):
    assert client.get(f"/calendar/ics/{bad}.ics").status_code == 404


def test_only_the_hash_is_stored(db, ana):
    path = _make_link(ana)
    raw = path.removeprefix("/calendar/ics/").removesuffix(".ics")
    token = phone_calendar.active(db, ana.member)
    assert token.token_hash != raw
    assert len(token.token_hash) == 64
    assert raw not in str(db.execute(select(phone_calendar.CalendarToken.token_hash)).all())


def test_the_feed_is_rate_limited(app, client):
    app.state.ics_limiter.per_minute = 2
    codes = [client.get("/calendar/ics/" + "A" * 43 + ".ics").status_code for _ in range(4)]
    assert codes == [404, 404, 429, 429]


def test_tokens_never_reach_the_access_log():
    phone_calendar.redact_access_log()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.2:5000", "GET", "/calendar/ics/s3cretT0ken-abcdefghijklmnop.ics", "1.1", 200),
        None,
    )  # fmt: skip
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    assert "s3cret" not in record.getMessage()
    assert "/calendar/ics/[token].ics" in record.getMessage()


def test_long_lines_are_folded_and_text_escaped():
    line = "SUMMARY:" + "ä" * 60
    folded = phone_calendar._fold(line)
    parts = folded.split("\r\n ")
    assert all(len(p.encode()) <= 75 for p in parts)
    assert "".join(parts) == line
    assert phone_calendar._escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


def test_nothing_set_up_yet_says_what_to_do_next(ada, ana):
    """A fresh install: admins get the way to set things up, members whom to ask."""
    for page in ("/calendar", "/calendar/new"):
        member, admin = ana.get(page).text, ada.get(page).text
        assert en.CAL_NOTHING_TO_BOOK in member
        assert en.ASK_ADMIN.format(names="ada") in member  # no display name: from the id
        assert 'href="/admin/bookable/new"' not in member
        assert 'href="/admin/bookable/new"' in admin
    assert 'name="thing"' not in ana.get("/calendar/new").text  # no form with nothing to pick
    assert en.ASK_ADMIN.format(names="ada") in ana.get("/").text
    assert 'href="/admin/resources/new"' in ada.get("/").text


def test_with_things_to_book_there_is_no_empty_note(ana, things):
    assert en.CAL_NOTHING_TO_BOOK not in ana.get("/calendar").text
    assert 'name="thing"' in ana.get("/calendar/new").text


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"thing": "theatre", "start": "garbage"}, "start"),
        ({"thing": "theatre", "end": "2020-01-01T10:00"}, "end"),
        ({"thing": "theatre", "destination": "town"}, "destination"),
        ({"thing": ""}, "thing"),
    ],
)
def test_a_booking_error_marks_its_field(ana, things, data, field):
    form = {"start": "", "end": "", "note": "", "destination": "", **data}
    r = ana.post("/calendar/bookings", data={"csrf_token": ana.token, **form})
    assert r.status_code == 400
    assert re.search(
        rf'id="f-{field}"[^>]*aria-invalid="true" aria-describedby="e-{field}"', r.text
    )
    assert f'id="e-{field}" role="alert"' in r.text
    assert 'class="alert-box"' not in r.text  # the message is at the field, not on top


def test_a_clash_is_about_the_whole_booking(ana, bob, things):
    day = (utcnow() + timedelta(days=2)).astimezone(ZoneInfo("Europe/Stockholm")).date()
    form = {"thing": "theatre", "start": f"{day}T10:00", "end": f"{day}T12:00"}
    form |= {"note": "", "destination": ""}
    assert post(ana, "/calendar/bookings", form).status_code == 303
    r = bob.post("/calendar/bookings", data={"csrf_token": bob.token, **form})
    assert r.status_code == 409
    assert 'class="alert-box"' in r.text
    assert 'aria-invalid="true"' not in r.text
    # Who has it and when, and the way to that day, filtered to the thing.
    assert "Theatre is already booked then, by ana: " in r.text
    assert f"day={day.isoformat()}&amp;thing=theatre" in r.text
