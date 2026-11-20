"""CSRF middleware, security headers, vendored htmx and the no-inline-code rule."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import INTERNAL_TOKEN, sign_in

from app import security
from app.auth import SESSION_COOKIE, csrf_token_for
from app.models import Session

APP = Path(__file__).parent.parent / "app"
VENDOR = APP / "static" / "vendor"


@pytest.fixture
def signed_in(client, fake_matrix, nora):
    assert sign_in(client, fake_matrix).status_code == 303
    return client


def token(client) -> str:
    return csrf_token_for(client.cookies.get(SESSION_COOKIE))


def live_sessions(db) -> int:
    db.expire_all()
    return len(db.scalars(select(Session).where(Session.revoked_at.is_(None))).all())


# --- CSRF -------------------------------------------------------------------


def test_post_without_token_is_403_and_changes_nothing(signed_in, db):
    r = signed_in.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 403
    assert "expired" in r.text
    assert live_sessions(db) == 1


@pytest.mark.parametrize(
    ("data", "headers"),
    [
        ({"csrf_token": "0" * 64}, {}),
        ({}, {"X-CSRF-Token": "0" * 64}),
        ({"csrf_token": ""}, {}),
    ],
)
def test_post_with_wrong_token_is_403(signed_in, db, data, headers):
    r = signed_in.post("/auth/logout", data=data, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert live_sessions(db) == 1


def test_json_body_without_header_is_403(signed_in, db):
    """Only a urlencoded form or the header can carry the token."""
    r = signed_in.post("/auth/logout", json={"csrf_token": token(signed_in)})
    assert r.status_code == 403


def test_form_token_is_accepted(signed_in, db):
    r = signed_in.post("/auth/logout", data={"csrf_token": token(signed_in)})
    assert r.status_code == 200
    assert live_sessions(db) == 0


def test_htmx_header_token_is_accepted(signed_in, db):
    r = signed_in.post("/auth/logout", headers={"X-CSRF-Token": token(signed_in)})
    assert r.status_code == 200
    assert live_sessions(db) == 0


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_state_changing_methods_need_the_token(signed_in, method):
    assert signed_in.request(method, "/auth/logout").status_code == 403


def test_not_signed_in_goes_to_the_route_guard(client):
    assert client.post("/auth/logout").status_code == 401


def test_oversized_form_is_refused(signed_in):
    body = "csrf_token=x&pad=" + "a" * security.MAX_FORM_BYTES
    r = signed_in.post(
        "/auth/logout",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413


def test_too_many_fields_is_403_not_500(signed_in):
    body = "&".join(f"f{i}=1" for i in range(300)) + f"&csrf_token={token(signed_in)}"
    r = signed_in.post(
        "/auth/logout",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 403


def test_bot_api_is_exempt_even_with_a_cookie(signed_in):
    """The bot authenticates with a bearer token; a stray cookie doesn't trigger CSRF."""
    r = signed_in.post(
        "/internal/assistant",
        json={"sender": "@nora:example.test", "body": "hi"},
        headers={"authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    assert r.status_code != 403


def test_page_carries_the_token_for_htmx(signed_in):
    page = signed_in.get("/").text
    t = token(signed_in)
    assert f'<meta name="csrf-token" content="{t}">' in page
    assert f"""hx-headers='{{"X-CSRF-Token": "{t}"}}'""" in page


def test_signed_out_page_has_no_token(client):
    page = client.get("/").text
    assert "csrf-token" not in page
    assert "hx-headers" not in page


# --- headers ----------------------------------------------------------------

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@pytest.mark.parametrize(
    "path", ["/", "/health", "/static/app.css", "/static/snackbar.js", "/does-not-exist"]
)
def test_security_headers_on_every_response(client, path):
    r = client.get(path)
    assert r.headers["content-security-policy"] == EXPECTED_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    policy = r.headers["permissions-policy"]
    for feature in ("camera=()", "microphone=()", "geolocation=()"):
        assert feature in policy


def test_csrf_refusal_has_the_headers_too(signed_in):
    r = signed_in.post("/auth/logout")
    assert r.status_code == 403
    assert r.headers["content-security-policy"] == EXPECTED_CSP


def test_route_can_set_a_stricter_referrer_policy():
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.testclient import TestClient

    mini = FastAPI()
    mini.add_middleware(security.SecurityHeadersMiddleware)

    @mini.get("/")
    def page():
        return PlainTextResponse("x", headers={"Referrer-Policy": "no-referrer"})

    r = TestClient(mini).get("/")
    assert r.headers.get_list("referrer-policy") == ["no-referrer"]
    assert r.headers["content-security-policy"] == EXPECTED_CSP


# --- vendored htmx and no inline code ---------------------------------------


def test_vendored_files_match_their_checksums():
    lines = (VENDOR / "SHA256SUMS").read_text().splitlines()
    pinned = {}
    for line in lines:
        digest, name = line.split()
        pinned[name] = digest
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name
    # Every script and font served from vendor/ is pinned; nothing slips in unlisted.
    served = {
        str(p.relative_to(VENDOR))
        for p in VENDOR.rglob("*")
        if p.suffix in {".js", ".woff2", ".woff", ".ttf", ".otf"}
    }
    assert served == set(pinned)


def test_vendored_fonts_are_served_locally_with_their_licences(client):
    css = client.get("/static/app.css").text
    fonts = sorted((VENDOR / "fonts").glob("*.woff2"))
    assert fonts
    for font in fonts:
        assert f"url(/static/vendor/fonts/{font.name})" in css
        r = client.get(f"/static/vendor/fonts/{font.name}")
        assert r.status_code == 200
        assert r.content == font.read_bytes()
    for licence in ("OFL-public-sans.txt", "OFL-newsreader.txt"):
        assert "SIL Open Font License" in (VENDOR / "fonts" / licence).read_text()
    assert "http://" not in css  # nothing from elsewhere
    assert "https://" not in css


@pytest.mark.parametrize("name", ["htmx.min.js", "htmx-ext-sse.min.js"])
def test_vendored_scripts_are_served_locally(client, name):
    page = client.get("/").text
    assert f'<script src="/static/vendor/{name}" defer></script>' in page
    r = client.get(f"/static/vendor/{name}")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_htmx_is_kept_from_inline_code(client):
    page = client.get("/").text
    for setting in ('"includeIndicatorStyles": false', '"allowEval": false'):
        assert setting in page


@pytest.mark.parametrize("template", sorted((APP / "templates").glob("*.html")), ids=str)
def test_templates_have_no_inline_code(template):
    """The CSP blocks inline styles, scripts and event handlers; none may creep in."""
    html = template.read_text()
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"<style\b", html)
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert "javascript:" not in html
