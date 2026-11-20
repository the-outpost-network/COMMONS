"""Sign in with Matrix, sessions, logout and role guards."""

from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import select
from tests.conftest import PUBLIC, SERVER, sign_in

from app import members
from app.auth import SESSION_COOKIE, hash_token, require_member, require_role
from app.i18n import en
from app.models import AuthEvent, Session, utcnow
from app.ratelimit import RateLimiter


def events(db):
    db.expire_all()
    return [e.kind for e in db.scalars(select(AuthEvent).order_by(AuthEvent.id))]


def sessions(db):
    db.expire_all()
    return list(db.scalars(select(Session)))


# -- the happy path ---------------------------------------------------------


def test_login_redirects_to_matrix_with_pkce_and_state(client, fake_matrix):
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 303
    url = httpx.URL(r.headers["location"])
    assert str(url).startswith(f"{PUBLIC}/_continuwuity/oauth2/grant/authorization_code?")
    q = {k: v[0] for k, v in parse_qs(url.query.decode()).items()}
    assert q["code_challenge_method"] == "S256"
    assert len(q["code_challenge"]) >= 43
    assert q["redirect_uri"] == "https://commons.example.test/auth/callback"
    assert "urn:matrix:client:device:COMMONS_DASHBOARD" in q["scope"]
    assert len(q["state"]) >= 32
    cookie = r.headers["set-cookie"]
    assert "commons_login=" in cookie
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/auth"):
        assert flag in cookie


def test_sign_in_creates_session_and_revokes_matrix_tokens(client, fake_matrix, nora, db):
    r = sign_in(client, fake_matrix)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    for flag in ("commons_session=", "HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag in cookie
    # both tokens were revoked; nothing is still live
    assert sorted(fake_matrix.revoked) == ["access-secret-2", "refresh-secret-2"]
    assert fake_matrix.live_tokens == set()
    # only the hash of the session cookie is stored
    raw = client.cookies[SESSION_COOKIE]
    [s] = sessions(db)
    assert s.id_hash == hash_token(raw) != raw
    assert s.member_id == nora.id
    assert events(db) == ["login"]
    page = client.get("/")
    assert "Signed in as" in page.text
    assert "Nora" in page.text


def test_client_is_registered_once_and_stored_privately(settings, fake_matrix, nora, client):
    sign_in(client, fake_matrix)
    sign_in(client, fake_matrix)
    assert fake_matrix.registrations == 1
    path = settings.data_dir / "matrix_oauth_client.json"
    assert path.stat().st_mode & 0o777 == 0o600


def test_new_login_rotates_the_old_session(client, fake_matrix, nora, db):
    sign_in(client, fake_matrix)
    first = client.cookies[SESSION_COOKIE]
    sign_in(client, fake_matrix)
    assert client.cookies[SESSION_COOKIE] != first
    old = db.get(Session, hash_token(first))
    db.refresh(old)
    assert old.revoked_at is not None


# -- failures: never a session, tokens always revoked -----------------------


def test_new_account_on_this_server_becomes_a_member(client, fake_matrix, db):
    """Every account on the homeserver is a member (accounts exist only via invites)."""
    r = sign_in(client, fake_matrix)
    assert r.status_code == 303
    nora = members.get_by_matrix_id(db, fake_matrix.user_id)
    assert nora.role_names == {"member"}
    assert len(sessions(db)) == 1
    assert fake_matrix.live_tokens == set()
    assert events(db) == ["login"]


def test_deactivated_member_gets_no_session(client, fake_matrix, nora, db):
    nora.deactivated_at = utcnow()
    db.commit()
    r = sign_in(client, fake_matrix)
    assert r.status_code == 403
    assert sessions(db) == []


def test_user_from_another_server_is_rejected(client, fake_matrix, db):
    members.add_member(db, "@nora:evil.test")
    db.commit()
    fake_matrix.user_id = "@nora:evil.test"
    r = sign_in(client, fake_matrix)
    assert r.status_code == 502
    assert sessions(db) == []
    assert fake_matrix.live_tokens == set()


def test_tokens_revoked_even_when_whoami_fails(client, fake_matrix, nora, db):
    fake_matrix.fail_whoami = True
    r = sign_in(client, fake_matrix)
    assert r.status_code == 502
    assert fake_matrix.live_tokens == set()
    assert len(fake_matrix.revoked) == 2
    assert sessions(db) == []


def test_no_session_when_revocation_fails(client, fake_matrix, nora, db):
    fake_matrix.fail_revoke = True
    r = sign_in(client, fake_matrix)
    assert r.status_code == 502
    assert sessions(db) == []


def test_no_session_when_token_survives_revocation(client, fake_matrix, nora, db):
    fake_matrix.ignore_revoke = True
    r = sign_in(client, fake_matrix)
    assert r.status_code == 502
    assert sessions(db) == []


def test_failed_token_exchange(client, fake_matrix, nora, db):
    fake_matrix.fail_token = True
    assert sign_in(client, fake_matrix).status_code == 502
    assert sessions(db) == []


def test_tokens_never_logged(client, fake_matrix, nora, caplog):
    fake_matrix.fail_whoami = True
    sign_in(client, fake_matrix)
    assert "secret" not in caplog.text


@pytest.mark.parametrize("case", ["no_cookie", "wrong_state", "replay"])
def test_bad_state_is_rejected_without_exchanging_the_code(client, fake_matrix, nora, db, case):
    r = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(httpx.URL(r.headers["location"]).query.decode())["state"][0]
    if case == "no_cookie":  # e.g. a callback link opened in another browser
        client.cookies.delete("commons_login", path="/auth")
    elif case == "wrong_state":
        state = "x" * len(state)
    elif case == "replay":
        client.get("/auth/callback", params={"code": fake_matrix.grant(), "state": state})
        client.cookies.set("commons_login", state, path="/auth")
        exchanges = fake_matrix.exchanges
    before = fake_matrix.exchanges
    r = client.get("/auth/callback", params={"code": fake_matrix.grant(), "state": state})
    assert r.status_code == 400
    assert fake_matrix.exchanges == before
    if case != "replay":
        assert sessions(db) == []
    else:
        assert fake_matrix.exchanges == exchanges


def test_cancelled_sign_in(client, fake_matrix, db):
    client.get("/auth/login", follow_redirects=False)
    r = client.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400
    assert fake_matrix.exchanges == 0


def test_metadata_pointing_elsewhere_is_refused(client, fake_matrix):
    fake_matrix.endpoint_host = "https://evil.test"
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 502
    assert fake_matrix.registrations == 0


# -- sessions ----------------------------------------------------------------


def test_expired_session_is_rejected(client, fake_matrix, nora, db):
    sign_in(client, fake_matrix)
    [s] = sessions(db)
    s.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert en.SIGN_IN_WITH_MATRIX in client.get("/").text


def test_activity_extends_the_idle_expiry(client, fake_matrix, nora, db):
    sign_in(client, fake_matrix)
    [s] = sessions(db)
    s.last_seen_at = utcnow() - timedelta(days=3)
    s.expires_at = utcnow() + timedelta(days=11)
    db.commit()
    client.get("/")
    [s] = sessions(db)
    assert s.expires_at > utcnow() + timedelta(days=13)


def test_session_ends_when_member_is_deactivated(client, fake_matrix, nora, db):
    sign_in(client, fake_matrix)
    nora.deactivated_at = utcnow()
    db.commit()
    assert en.SIGN_IN_WITH_MATRIX in client.get("/").text


def test_logout_requires_csrf_token(client, fake_matrix, nora, db):
    sign_in(client, fake_matrix)
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", data={"csrf_token": "x" * 64}).status_code == 403
    [s] = sessions(db)
    assert s.revoked_at is None


def csrf(client) -> str:
    page = client.get("/").text
    return page.split('name="csrf_token" value="')[1].split('"')[0]


def test_logout_invalidates_the_session(client, fake_matrix, nora, db):
    sign_in(client, fake_matrix)
    raw = client.cookies[SESSION_COOKIE]
    r = client.post("/auth/logout", data={"csrf_token": csrf(client)})
    assert r.status_code == 200
    # replaying the old cookie no longer works
    client.cookies.set(SESSION_COOKIE, raw)
    assert en.SIGN_IN_WITH_MATRIX in client.get("/").text
    assert events(db) == ["login", "logout"]


def test_logout_everywhere_ends_all_of_my_sessions_only(app, client, fake_matrix, nora, db):
    from fastapi.testclient import TestClient

    sign_in(client, fake_matrix)
    with TestClient(app, base_url="https://commons.example.test") as phone:
        sign_in(phone, fake_matrix)
        other = members.add_member(db, f"@adelia:{SERVER}")
        db.commit()
        fake_matrix.user_id = other.matrix_user_id
        with TestClient(app, base_url="https://commons.example.test") as adelia:
            sign_in(adelia, fake_matrix)
            client.post("/auth/logout-everywhere", data={"csrf_token": csrf(client)})
            assert en.SIGN_IN_WITH_MATRIX in phone.get("/").text
            assert "Signed in as" in adelia.get("/").text


def test_logout_without_session_is_unauthorized(client):
    assert client.post("/auth/logout", data={"csrf_token": "x"}).status_code == 401


# -- role guards ---------------------------------------------------------------


@pytest.fixture
def guarded(app):
    @app.get("/test/member", dependencies=[Depends(require_member)])
    def m():
        return {"ok": True}

    @app.get("/test/board", dependencies=[Depends(require_role("board", "admin"))])
    def b():
        return {"ok": True}

    @app.get("/test/admin", dependencies=[Depends(require_role("admin"))])
    def a():
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (None, {"member": 401, "board": 401, "admin": 401}),
        ((), {"member": 200, "board": 403, "admin": 403}),
        (("board",), {"member": 200, "board": 200, "admin": 403}),
        (("admin",), {"member": 200, "board": 200, "admin": 200}),
    ],
)
def test_role_guards(guarded, client, fake_matrix, db, log_signer, roles, expected):
    if roles is not None:
        members.add_member(db, f"@nora:{SERVER}", roles=roles, signer=log_signer)
        db.commit()
        sign_in(client, fake_matrix)
    for route, status in expected.items():
        assert client.get(f"/test/{route}").status_code == status, route


# -- rate limiting -------------------------------------------------------------


def test_login_is_rate_limited(app, client):
    app.state.login_limiter = RateLimiter(per_minute=3)
    codes = [client.get("/auth/login", follow_redirects=False).status_code for _ in range(5)]
    assert codes == [303, 303, 303, 429, 429]


def test_rate_limiter_window_and_keys():
    rl = RateLimiter(per_minute=2)
    assert rl.allow("a", 0)
    assert rl.allow("a", 1)
    assert not rl.allow("a", 2)
    assert rl.allow("b", 2)  # another client is unaffected
    assert rl.allow("a", 61)  # window moved on


def test_rate_limiter_memory_is_bounded():
    rl = RateLimiter(per_minute=1, max_keys=100)
    for i in range(1000):
        rl.allow(f"ip{i}", 0)
    assert len(rl._hits) <= 100


def test_client_ip_trusts_forwarded_header_only_from_the_proxy():
    from starlette.requests import Request

    from app.ratelimit import client_ip

    def req(peer, xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "client": (peer, 1), "headers": headers})

    assert client_ip(req("172.22.0.4", "100.89.38.110")) == "100.89.38.110"
    assert client_ip(req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"
    assert client_ip(req("172.22.0.4", "not-an-ip")) == "172.22.0.4"


# -- Synapse + MAS: identity via OpenID userinfo ------------------------------


@pytest.fixture
def mas_client(settings, fake_matrix):
    from dataclasses import replace

    from fastapi.testclient import TestClient
    from tests.conftest import AUTH_INTERNAL, AUTH_PUBLIC

    from app.main import create_app

    fake_matrix.mas = True
    s = replace(
        settings, matrix_auth_public_url=AUTH_PUBLIC, matrix_auth_internal_url=AUTH_INTERNAL
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_matrix.handler))
    app = create_app(s, http=http, require_secrets=False)
    with TestClient(app, base_url="https://commons.example.test") as c:
        yield c, app


def test_mas_sign_in_asks_only_for_openid(mas_client, fake_matrix):
    client, app = mas_client
    with app.state.sessionmaker() as db:
        members.add_member(db, f"@nora:{SERVER}")
        db.commit()
    r = client.get("/auth/login", follow_redirects=False)
    assert r.headers["location"].startswith("https://auth.example.test/authorize?")
    r = sign_in(client, fake_matrix)
    assert r.status_code == 303, r.text
    assert fake_matrix.last_scope == "openid"  # no Matrix API access, no device
    assert fake_matrix.whoami_calls == 0  # Synapse's cached token check isn't used
    assert fake_matrix.live_tokens == set()
    assert "Signed in as" in client.get("/").text


def test_mas_no_session_when_token_survives(mas_client, fake_matrix):
    client, app = mas_client
    with app.state.sessionmaker() as db:
        members.add_member(db, f"@nora:{SERVER}")
        db.commit()
    fake_matrix.ignore_revoke = True
    assert sign_in(client, fake_matrix).status_code == 502
