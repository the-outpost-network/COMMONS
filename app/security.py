"""Browser security: CSRF check on every state change, and security headers.

Both are plain ASGI middleware, so they cover every route and mount, including
ones added later, without each route remembering to opt in.

CSRF: the token is derived from the session cookie (auth.csrf_token_for), so
it is per session and needs no database lookup. A state-changing request that
carries a session cookie must also carry the token, either in the
``X-CSRF-Token`` header (htmx sends it via ``hx-headers`` on <body>) or as the
``csrf_token`` field of a urlencoded form. Otherwise it is refused with 403.

Requests without a session cookie aren't acting for anyone, so a forged one
can't do anything; they go on to the route guard (401). ``/internal/*`` is the
bot's API: it authenticates with a bearer token, never with cookies, so
cross-site requests can't use it.
"""

from __future__ import annotations

import hmac
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth import SESSION_COOKIE, csrf_token_for, message_page
from app.i18n import en

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
# Paths that never use cookie authentication.
CSRF_EXEMPT_PREFIXES = ("/internal/",)
# Forms are small; anything bigger isn't a form this app serves.
MAX_FORM_BYTES = 1_000_000

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "permissions-policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "interest-cohort=(), browsing-topics=()"
    ),
}


class SecurityHeadersMiddleware:
    """Adds SECURITY_HEADERS to every HTTP response.

    A header a route set itself wins (e.g. the join page's stricter
    ``Referrer-Policy: no-referrer``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.headers = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.headers if k not in present]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _exempt(path: str) -> bool:
    return path == "/internal" or path.startswith(CSRF_EXEMPT_PREFIXES)


class CSRFMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS or _exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            await self.app(scope, receive, send)  # not signed in: the route guard answers
            return
        expected = csrf_token_for(raw)

        sent = request.headers.get(CSRF_HEADER)
        replay = receive
        if sent is None and _is_urlencoded(request.headers):
            body = await _read_body(receive)
            if body is None:
                await _refuse(scope, send, 413, en.BAD_REQUEST)
                return
            try:
                fields = parse_qs(body.decode("latin-1"), max_num_fields=200)
            except ValueError:  # too many fields
                fields = {}
            values = fields.get(CSRF_FIELD, [])
            sent = values[0] if len(values) == 1 else None
            replay = _replayer(body, receive)
        if sent is None or not hmac.compare_digest(sent.encode(), expected.encode()):
            await _refuse(scope, send, 403, en.CSRF_FAILED)
            return
        await self.app(scope, replay, send)


def _is_urlencoded(headers: Headers) -> bool:
    ctype = headers.get("content-type", "").split(";")[0].strip().lower()
    return ctype == "application/x-www-form-urlencoded"


async def _read_body(receive: Receive) -> bytes | None:
    """The whole request body, or None if it is larger than MAX_FORM_BYTES."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks)
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_FORM_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replayer(body: bytes, receive: Receive) -> Receive:
    """Hand the buffered body to the app once, then pass through (disconnects)."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _refuse(scope: Scope, send: Send, status: int, text: str) -> None:
    async def no_body() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    title = en.CSRF_FAILED_TITLE if text == en.CSRF_FAILED else None
    response = message_page(Request(scope), text, status, title)
    await response(scope, no_body, send)
