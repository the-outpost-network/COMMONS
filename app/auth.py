"""Dashboard sign-in with Matrix, sessions and permission guards."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import update
from sqlalchemy.orm import Session as DBSession

from app import invites, members
from app.db import get_db
from app.i18n import en
from app.matrix_admin import MatrixAdminError
from app.matrix_oauth import PKCE, MatrixAuthError
from app.models import AuthEvent, Member, Session, utcnow
from app.ratelimit import client_ip

SESSION_COOKIE = "commons_session"
LOGIN_COOKIE = "commons_login"
LOGIN_TTL_SECONDS = 600
MAX_PENDING_LOGINS = 1000
LAST_SEEN_RESOLUTION = timedelta(minutes=5)

router = APIRouter()

DB = Annotated[DBSession, Depends(get_db)]


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def csrf_token_for(raw_session_token: str) -> str:
    """CSRF token derived from the session cookie value, which only the browser holds."""
    return hashlib.sha256(b"csrf:" + raw_session_token.encode()).hexdigest()


# -- pending sign-ins (between /auth/login and /auth/callback) -------------


@dataclass
class PendingLogin:
    verifier: str
    created: float


class PendingLogins:
    def __init__(self) -> None:
        self._items: dict[str, PendingLogin] = {}

    def add(self, state: str, verifier: str) -> None:
        now = time.monotonic()
        self._items = {k: v for k, v in self._items.items() if now - v.created < LOGIN_TTL_SECONDS}
        while len(self._items) >= MAX_PENDING_LOGINS:
            self._items.pop(next(iter(self._items)))
        self._items[state] = PendingLogin(verifier, now)

    def pop(self, state: str) -> PendingLogin | None:
        item = self._items.pop(state, None)
        if item and time.monotonic() - item.created < LOGIN_TTL_SECONDS:
            return item
        return None


# -- sessions ---------------------------------------------------------------


@dataclass
class CurrentSession:
    member: Member
    session: Session
    csrf_token: str


def log_event(db: DBSession, kind: str, member_id: int | None = None) -> None:
    db.add(AuthEvent(kind=kind, member_id=member_id, channel="web", ts=utcnow()))


def create_session(db: DBSession, member: Member, idle_days: int) -> str:
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        Session(
            id_hash=hash_token(raw),
            member_id=member.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=idle_days),
        )
    )
    return raw


def revoke_session(db: DBSession, raw: str) -> Session | None:
    s = db.get(Session, hash_token(raw))
    if s and s.revoked_at is None:
        s.revoked_at = utcnow()
    return s


def load_session(request: Request, db: DBSession) -> CurrentSession | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw or len(raw) > 100:
        return None
    s = db.get(Session, hash_token(raw))
    now = utcnow()
    if s is None or s.revoked_at is not None or s.expires_at <= now or not s.member.is_active:
        return None
    if now - s.last_seen_at >= LAST_SEEN_RESOLUTION:
        s.last_seen_at = now
        s.expires_at = now + timedelta(days=request.app.state.settings.session_idle_days)
        db.commit()
    return CurrentSession(s.member, s, csrf_token_for(raw))


def optional_session(request: Request, db: DB) -> CurrentSession | None:
    return load_session(request, db)


def require_member(request: Request, db: DB) -> CurrentSession:
    current = load_session(request, db)
    if current is None:
        raise HTTPException(status_code=401, detail=en.NOT_SIGNED_IN)
    return current


SignedIn = Annotated[CurrentSession, Depends(require_member)]
MaybeSignedIn = Annotated[CurrentSession | None, Depends(optional_session)]


def require_role(*roles: str):
    """Guard: signed in and holding at least one of ``roles``."""

    def guard(current: SignedIn) -> CurrentSession:
        if not current.member.role_names.intersection(roles):
            raise HTTPException(status_code=403, detail=en.FORBIDDEN)
        return current

    return guard


# The token comes as a form field (plain forms) or a header (htmx, hx-headers).
# app/security.py's middleware checks it first for every state change; these
# route-level checks are the second line of defence.
CsrfField = Annotated[str | None, Form(max_length=100)]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token", max_length=100)]


def check_csrf(token: str | None, current: CurrentSession) -> CurrentSession:
    if token is None or not hmac.compare_digest(token, current.csrf_token):
        raise HTTPException(status_code=403, detail=en.CSRF_FAILED)
    return current


def require_csrf(
    current: SignedIn, csrf_token: CsrfField = None, x_csrf_token: CsrfHeader = None
) -> CurrentSession:
    return check_csrf(x_csrf_token or csrf_token, current)


CsrfChecked = Annotated[CurrentSession, Depends(require_csrf)]

# Admin pages: the role guard runs first, so a non-admin gets 403 whatever they send.
admin_guard = require_role("admin")
Admin = Annotated[CurrentSession, Depends(admin_guard)]


def require_admin_csrf(
    current: Admin, csrf_token: CsrfField = None, x_csrf_token: CsrfHeader = None
) -> CurrentSession:
    return check_csrf(x_csrf_token or csrf_token, current)


AdminCsrfChecked = Annotated[CurrentSession, Depends(require_admin_csrf)]

# Board pages (open and cancel proposals): board members and admins.
board_guard = require_role("board", "admin")
Board = Annotated[CurrentSession, Depends(board_guard)]


def require_board_csrf(
    current: Board, csrf_token: CsrfField = None, x_csrf_token: CsrfHeader = None
) -> CurrentSession:
    return check_csrf(x_csrf_token or csrf_token, current)


BoardCsrfChecked = Annotated[CurrentSession, Depends(require_board_csrf)]


def _set_cookie(
    request: Request, response: Response, name: str, value: str, max_age: int, path: str = "/"
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=path,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="lax",
    )


def message_page(request: Request, text: str, status: int, title: str | None = None) -> Response:
    """An error or notice page. The heading says what happened; a signed-in member
    keeps the normal header (tabs and account menu)."""
    heading = title or en.MESSAGE_TITLES.get(status, en.MESSAGE_TITLE_OTHER)
    with request.app.state.sessionmaker() as db:
        current = load_session(request, db)
        # Rendered inside the session: the header reads the member's roles.
        return request.app.state.templates.TemplateResponse(
            request,
            "message.html",
            {"title": heading, "text": text, "current": current},
            status_code=status,
        )


def _rate_limited(request: Request) -> bool:
    return not request.app.state.login_limiter.allow(client_ip(request))


# -- routes -----------------------------------------------------------------


@router.get("/auth/login")
async def login(request: Request, signup: bool = False) -> Response:
    """Public: start a sign-in by sending the browser to the Matrix login page.

    ``?signup=1`` opens the "create account" page instead (from an invite link).
    """
    if _rate_limited(request):
        return message_page(request, en.TOO_MANY_REQUESTS, 429)
    state = secrets.token_urlsafe(24)
    pkce = PKCE.new()
    try:
        url = await request.app.state.matrix.authorization_url(state, pkce, signup=signup)
    except (MatrixAuthError, ValueError, OSError) as e:
        request.app.state.log.warning("sign-in start failed: %s", e)
        return message_page(request, en.SIGN_IN_FAILED, 502, en.SIGN_IN_FAILED_TITLE)
    request.app.state.pending_logins.add(state, pkce.verifier)
    response = RedirectResponse(url, status_code=303)
    # Ties the callback to this browser (stops login CSRF / forwarded callback links).
    _set_cookie(request, response, LOGIN_COOKIE, state, LOGIN_TTL_SECONDS, path="/auth")
    return response


@router.get("/auth/callback")
async def callback(
    request: Request,
    db: DB,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Public: finish a sign-in. Matrix redirects here with a one-time code."""
    if _rate_limited(request):
        return message_page(request, en.TOO_MANY_REQUESTS, 429)
    cookie_state = request.cookies.get(LOGIN_COOKIE)
    pending = None
    if state and cookie_state and len(state) <= 100 and hmac.compare_digest(state, cookie_state):
        pending = request.app.state.pending_logins.pop(state)

    def fail(text: str, status: int, member_id: int | None = None) -> Response:
        log_event(db, "login_failed", member_id)
        db.commit()
        response = message_page(request, text, status, en.SIGN_IN_FAILED_TITLE)
        response.delete_cookie(LOGIN_COOKIE, path="/auth")
        return response

    if error:
        return fail(en.SIGN_IN_CANCELLED, 400)
    if pending is None:
        return fail(en.SIGN_IN_EXPIRED, 400)
    if not code or len(code) > 512:
        return fail(en.SIGN_IN_FAILED, 400)

    try:
        matrix_user_id = await request.app.state.matrix.identify(code, pending.verifier)
    except (MatrixAuthError, ValueError, OSError) as e:
        request.app.state.log.warning("sign-in failed: %s", e)
        return fail(en.SIGN_IN_FAILED, 502)

    # Every account on this homeserver is a member; new accounts join here.
    member = members.ensure_member(
        db, matrix_user_id, request.app.state.settings.matrix_server_name
    )
    if member is None or not member.is_active:
        return fail(en.NOT_A_MEMBER, 403, member.id if member else None)

    # Matrix is the source of truth for the display name; refresh COMMONS' copy.
    names = request.app.state.matrix_admin
    if names is not None:
        try:
            members.store_display_name(db, member, await names.get_display_name(matrix_user_id))
        except MatrixAdminError as e:
            request.app.state.log.warning("could not read the Matrix display name: %s", e)

    # Rotate: a session cookie that came along with this browser is ended.
    old = request.cookies.get(SESSION_COOKIE)
    if old and len(old) <= 100:
        revoke_session(db, old)
    raw = create_session(db, member, request.app.state.settings.session_idle_days)
    log_event(db, "login", member.id)
    db.commit()

    response = RedirectResponse("/", status_code=303)
    _set_cookie(
        request, response, SESSION_COOKIE, raw, request.app.state.settings.session_idle_days * 86400
    )
    response.delete_cookie(LOGIN_COOKIE, path="/auth")
    invites.clear_cookie(request, response)
    return response


@router.post("/auth/logout")
def logout(
    request: Request,
    current: CsrfChecked,
    db: DB,
) -> Response:
    """Signed-in members: end this session."""
    revoke_session(db, request.cookies[SESSION_COOKIE])
    log_event(db, "logout", current.member.id)
    db.commit()
    response = message_page(request, en.LOGGED_OUT, 200, en.LOGGED_OUT_TITLE)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/auth/logout-everywhere")
def logout_everywhere(
    request: Request,
    current: CsrfChecked,
    db: DB,
) -> Response:
    """Signed-in members: end all of their sessions."""
    db.execute(
        update(Session)
        .where(Session.member_id == current.member.id, Session.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    log_event(db, "logout_everywhere", current.member.id)
    db.commit()
    response = message_page(request, en.LOGGED_OUT, 200, en.LOGGED_OUT_TITLE)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
