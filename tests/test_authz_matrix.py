"""Authorization matrix: who gets what on every route.

MATRIX has one row per route the app serves and one column per kind of caller.
Each (route, caller) pair is a separate test that really calls the route:

- a number is the exact status the caller must get (401 not signed in,
  403 wrong role), and the request must change nothing;
- OK means the caller gets past the permission check: anything except 401 and
  403 (a POST may still answer 400 or 404 for its data; that is not tested here).

POSTs are sent with a valid CSRF token, so a refusal comes from the permission
check, not from a missing token. The bot only calls /internal/*; everywhere else
its token counts for nothing (it is treated as not signed in).

**Adding a route? Add its row here.** `test_matrix_covers_every_route` fails
until every route the app serves has one, so every new feature has to state who
may use it.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tests.conftest import INTERNAL_TOKEN, SERVER, VOTING, sign_in
from tests.test_route_guards import routes
from tests.test_security_headers import EXPECTED_CSP

from app import bookables, calendar, categories, governance, members
from app.models import (
    Booking,
    ConfigKV,
    DecisionLog,
    Invite,
    Proposal,
    ResourceEntry,
    Vote,
    utcnow,
)

OK = "OK"
CALLERS = ("anonymous", "member", "board", "admin", "bot")

#                                             anonymous member board admin bot
MATRIX: dict[tuple[str, str], tuple] = {
    # Public pages
    ("GET", "/"):                                 (OK,  OK,  OK,  OK,  OK),
    ("GET", "/health"):                           (OK,  OK,  OK,  OK,  OK),
    ("GET", "/auth/login"):                       (OK,  OK,  OK,  OK,  OK),
    ("GET", "/auth/callback"):                    (OK,  OK,  OK,  OK,  OK),
    ("GET", "/join/{code}"):                      (OK,  OK,  OK,  OK,  OK),
    # The token in the address is the permission; a wrong one is 404 for everyone.
    ("GET", "/calendar/ics/{token}.ics"):         (OK,  OK,  OK,  OK,  OK),
    # Signed-in members
    ("POST", "/auth/logout"):                     (401, OK,  OK,  OK,  401),
    ("POST", "/auth/logout-everywhere"):          (401, OK,  OK,  OK,  401),
    ("POST", "/ledger/entries"):                  (401, OK,  OK,  OK,  401),
    ("GET", "/log"):                              (401, OK,  OK,  OK,  401),
    ("GET", "/api/log/verify"):                   (401, OK,  OK,  OK,  401),
    ("GET", "/api/log/export"):                   (401, OK,  OK,  OK,  401),
    ("GET", "/api/log/keys"):                     (401, OK,  OK,  OK,  401),
    ("GET", "/proposals"):                        (401, OK,  OK,  OK,  401),
    ("GET", "/proposals/{proposal_id}"):          (401, OK,  OK,  OK,  401),
    ("POST", "/proposals/{proposal_id}/vote"):    (401, OK,  OK,  OK,  401),
    # Any member proposes a vote (Votes PRD).
    ("GET", "/proposals/new"):                    (401, OK,  OK,  OK,  401),
    ("POST", "/proposals"):                       (401, OK,  OK,  OK,  401),
    # Board members and admins
    ("POST", "/proposals/{proposal_id}/cancel"):  (401, 403, OK,  OK,  401),
    ("GET", "/calendar"):                         (401, OK,  OK,  OK,  401),
    ("GET", "/calendar/new"):                     (401, OK,  OK,  OK,  401),
    ("POST", "/calendar/bookings"):               (401, OK,  OK,  OK,  401),
    ("GET", "/calendar/bookings/{booking_id}"):   (401, OK,  OK,  OK,  401),
    ("POST", "/calendar/bookings/{booking_id}"):  (401, 403, 403, OK,  401),
    ("POST", "/calendar/bookings/{booking_id}/return"): (401, 403, 403, 403, 401),
    ("POST", "/calendar/bookings/{booking_id}/cancel"): (401, 403, 403, OK,  401),
    ("GET", "/calendar/phone"):                   (401, OK,  OK,  OK,  401),
    ("POST", "/calendar/phone"):                  (401, OK,  OK,  OK,  401),
    ("POST", "/calendar/phone/revoke"):           (401, OK,  OK,  OK,  401),
    # My own contributions and data: no member id in the address, so only ever one's own.
    ("GET", "/me"):                               (401, OK,  OK,  OK,  401),
    ("GET", "/me/export.json"):                   (401, OK,  OK,  OK,  401),
    # Admins
    ("GET", "/admin/members"):                    (401, 403, 403, OK,  401),
    ("POST", "/admin/invites"):                   (401, 403, 403, OK,  401),
    ("POST", "/admin/invites/{invite_id}/revoke"): (401, 403, 403, OK,  401),
    ("POST", "/admin/members/{member_id}/grant"):  (401, 403, 403, OK,  401),
    ("POST", "/admin/members/{member_id}/revoke"): (401, 403, 403, OK,  401),
    ("POST", "/admin/members/{member_id}/deactivate"): (401, 403, 403, OK, 401),
    ("POST", "/admin/members/{member_id}/reactivate"): (401, 403, 403, OK, 401),
    ("GET", "/admin/members/{member_id}/delete"):  (401, 403, 403, OK,  401),
    ("POST", "/admin/members/{member_id}/delete"): (401, 403, 403, OK,  401),
    ("GET", "/admin/resources"):                   (401, 403, 403, OK,  401),
    ("GET", "/admin/resources/new"):               (401, 403, 403, OK,  401),
    ("POST", "/admin/resources"):                  (401, 403, 403, OK,  401),
    ("GET", "/admin/resources/{category_id}/edit"): (401, 403, 403, OK, 401),
    ("POST", "/admin/resources/{category_id}"):    (401, 403, 403, OK,  401),
    ("POST", "/admin/resources/{category_id}/archive"): (401, 403, 403, OK, 401),
    ("POST", "/admin/resources/{category_id}/restore"): (401, 403, 403, OK, 401),
    ("POST", "/admin/resources/{category_id}/move"): (401, 403, 403, OK, 401),
    ("GET", "/admin/resources/{category_id}/delete"): (401, 403, 403, OK, 401),
    ("POST", "/admin/resources/{category_id}/delete"): (401, 403, 403, OK, 401),
    # Usage history: admins only, also the CSV with member names.
    ("GET", "/admin/resources/{category_id}/history"):     (401, 403, 403, OK, 401),
    ("GET", "/admin/resources/{category_id}/history.csv"): (401, 403, 403, OK, 401),
    ("GET", "/admin/bookable"):                           (401, 403, 403, OK,  401),
    ("GET", "/admin/bookable/new"):                       (401, 403, 403, OK,  401),
    ("POST", "/admin/bookable"):                          (401, 403, 403, OK,  401),
    ("GET", "/admin/bookable/{thing_id}/edit"):           (401, 403, 403, OK,  401),
    ("POST", "/admin/bookable/{thing_id}"):               (401, 403, 403, OK,  401),
    ("POST", "/admin/bookable/{thing_id}/archive"):       (401, 403, 403, OK,  401),
    ("POST", "/admin/bookable/{thing_id}/restore"):       (401, 403, 403, OK,  401),
    ("GET", "/admin/bookable/{thing_id}/delete"):         (401, 403, 403, OK,  401),
    ("POST", "/admin/bookable/{thing_id}/delete"):        (401, 403, 403, OK,  401),
    # The bot (internal service token); Caddy also blocks /internal from outside
    ("POST", "/internal/assistant"):              (401, 401, 401, 401, OK),
    ("GET", "/internal/notifications"):           (401, 401, 401, 401, OK),
    ("POST", "/internal/vote-messages"):          (401, 401, 401, 401, OK),
    ("POST", "/internal/votes"):                  (401, 401, 401, 401, OK),
    ("POST", "/internal/notifications/{notification_id}/delivered"): (401, 401, 401, 401, OK),
    ("GET", "/internal/events"):                  (401, 401, 401, 401, OK),
    ("POST", "/internal/backup-failed"):          (401, 401, 401, 401, OK),
}  # fmt: skip

# Extra request data per route; the CSRF token is added to every form POST.
FORMS: dict[str, dict] = {
    "/admin/invites": {"note": "matrix test"},
    "/admin/members/{member_id}/grant": {"role": "board"},
    "/admin/members/{member_id}/revoke": {"role": "board"},
    "/admin/resources": {
        "slug": "gas", "name": "Gas", "unit": "bottles", "kind": "stock",
        "shortage_threshold": "", "capacity": "", "forecast_horizon_days": "30", "aliases": "",
    },
    "/admin/resources/{category_id}": {
        "name": "Changed", "unit": "l", "kind": "stock", "shortage_threshold": "1",
        "capacity": "", "forecast_horizon_days": "30", "aliases": "",
    },
    "/admin/resources/{category_id}/move": {"direction": "down"},
    "/admin/bookable": {"name": "Van", "type": "vehicle", "location": "", "description": ""},
    "/admin/bookable/{thing_id}": {"name": "Changed", "type": "tool", "location": "",
                                 "description": ""},
    "/ledger/entries": {"entry_type": "reading", "amount": "800"},
    "/proposals": {
        "vote_type": "rules", "question": "Yes or no?", "option_1": "Yes", "option_2": "No",
    },
    "/proposals/{proposal_id}/cancel": {"reason": "matrix test"},
    "/calendar/bookings": {"thing": "thing", "note": "matrix test"},
    "/calendar/bookings/{booking_id}": {"end": "", "note": "changed"},
    "/calendar/bookings/{booking_id}/cancel": {"reason": "matrix test"},
}  # fmt: skip

ROLES = {"member": (), "board": ("board",), "admin": ("admin",)}
TARGET = f"@target:{SERVER}"


def _cases():
    for (method, path), expected in MATRIX.items():
        for caller, status in zip(CALLERS, expected, strict=True):
            yield pytest.param(method, path, caller, status, id=f"{method} {path} as {caller}")


def test_every_row_has_a_status_for_every_caller():
    for route, expected in MATRIX.items():
        assert len(expected) == len(CALLERS), route
        assert all(s == OK or s in (401, 403) for s in expected), route


def test_matrix_covers_every_route(app):
    served = {(m, r.path) for r in routes(app) for m in r.methods}
    assert served - MATRIX.keys() == set(), "add these routes to MATRIX"
    assert MATRIX.keys() - served == set(), "these MATRIX rows have no route any more"


def _sign_in_as(client: TestClient, fake_matrix, db, signer, caller: str) -> str:
    """Sign `client` in as a member with the caller's roles; returns the CSRF token."""
    user_id = f"@{caller}:{SERVER}"
    members.add_member(db, user_id, caller.title(), ROLES[caller], signer=signer)
    db.commit()
    fake_matrix.user_id = user_id
    assert sign_in(client, fake_matrix).status_code == 303
    page = client.get("/").text
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', page).group(1)


@pytest.mark.parametrize(("method", "path", "caller", "expected"), list(_cases()))
def test_authorization(client, fake_matrix, db, log_signer, method, path, caller, expected):
    # Something for the routes with an id to act on: a board member and an open invite.
    target = members.add_member(db, TARGET, "Target", ("board",), signer=log_signer)
    db.flush()
    creator = members.add_member(db, f"@creator:{SERVER}", "Creator")
    db.flush()
    invite = Invite(
        code_hash="0" * 64,
        mas_token_id="RT-0",
        created_by=creator.id,
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(invite)
    db.add(ConfigKV(key="voting", value_json=VOTING))
    db.flush()
    proposal = governance.open_proposal(
        db, target, "Buy a tank?", ("Yes", "No"), vote_type_slug="rules", signer=log_signer,
        channel="web",
    )  # fmt: skip
    water = categories.create(
        db, categories.CategorySpec(slug="water", name="Water", unit="l", kind="stock")
    )
    thing = bookables.create(db, bookables.ThingSpec(name="Thing", type="space"))
    # Someone else's booking, tomorrow: only its member (or an admin) may touch it.
    start = utcnow() + timedelta(days=1)
    booking = calendar.book(db, target, thing, start, start + timedelta(hours=1), channel="web")
    db.commit()
    thing_before = bookables.values(thing)
    booking_before = (booking.starts_at, booking.ends_at, booking.note, booking.cancelled_at)

    headers: dict[str, str] = {}
    csrf = ""
    if caller in ROLES:
        csrf = _sign_in_as(client, fake_matrix, db, log_signer, caller)
    elif caller == "bot":
        headers["Authorization"] = f"Bearer {INTERNAL_TOKEN}"
    log_before = db.scalar(select(func.count()).select_from(DecisionLog))

    url = path.format(
        member_id=target.id,
        invite_id=invite.id,
        category_id=water.id,
        proposal_id=proposal.id,
        notification_id=1,
        thing_id=thing.id,
        booking_id=booking.id,
        token="A" * 43,
        code="AAAA-BBBB-CCCC-DDDD-EEEE",
    )
    if method == "GET":
        r = client.get(url, headers=headers, follow_redirects=False)
    elif path.startswith("/internal/"):
        body = {"sender": f"@{caller}:{SERVER}", "room_id": f"!dm:{SERVER}", "text": "help"}
        r = client.post(url, json=body, headers=headers, follow_redirects=False)
    else:
        form = {**FORMS.get(path, {}), "csrf_token": csrf}
        if path == "/ledger/entries":
            form["category_id"] = str(water.id)
        if path == "/proposals/{proposal_id}/vote":
            form["choice"] = str(proposal.options[0].id)
        r = client.post(url, data=form, headers=headers, follow_redirects=False)

    # Every route, for every caller and outcome, carries the strict CSP (no inline scripts).
    assert r.headers["content-security-policy"] == EXPECTED_CSP
    if expected == OK:
        assert r.status_code not in (401, 403), f"{caller} was refused: {r.status_code}"
        return
    assert r.status_code == expected
    # A refused request changes nothing, in COMMONS or in MAS.
    db.expire_all()
    target = members.get_by_matrix_id(db, TARGET)
    assert target is not None
    assert target.role_names == {"member", "board"}
    assert target.is_active
    assert db.get(Invite, invite.id).revoked_at is None
    assert db.scalar(select(func.count()).select_from(Invite)) == 1
    assert fake_matrix.reg_tokens == {}
    assert fake_matrix.locked == set()
    assert fake_matrix.erased == []
    assert db.scalar(select(func.count()).select_from(ResourceEntry)) == 0
    # Refused callers opened nothing (a member's own new vote is allowed above).
    assert db.scalar(select(func.count()).select_from(Proposal)) == 1
    assert db.get(Proposal, proposal.id).status == "open"
    assert db.scalar(select(func.count()).select_from(Vote)) == 0
    (only,) = categories.list_all(db)
    assert (only.slug, only.name, only.is_active) == ("water", "Water", True)
    db.refresh(thing)
    assert bookables.values(thing) == thing_before
    assert len(bookables.list_all(db)) == 1
    db.refresh(booking)
    assert (booking.starts_at, booking.ends_at, booking.note, booking.cancelled_at) == (
        booking_before
    )
    assert db.scalar(select(func.count()).select_from(Booking)) == 1
    assert db.scalar(select(func.count()).select_from(DecisionLog)) == log_before
