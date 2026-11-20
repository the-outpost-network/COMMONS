"""The invite link: /join/<code>.

A valid link goes straight to MAS' "create account" page. The code travels in a
short-lived cookie for the parent domain (COMMONS at commons.<domain>, MAS at
auth.commons.<domain>); MAS' registration-token step (our template,
deploy/mas/registration_token.html) fills it in and submits it, deletes the
cookie, and the person only types a username and password. MAS then brings them
back signed in, and they become a member (`members.ensure_member`).

If MAS is not on a subdomain of COMMONS the cookie can't reach it; the page then
shows the code to paste instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app import invites
from app.auth import DB, message_page
from app.i18n import en
from app.matrix_admin import MatrixAdminError
from app.ratelimit import client_ip

router = APIRouter()


@router.get("/join/{code}")
async def join(request: Request, db: DB, code: str) -> Response:
    """Public: invite link; sends a valid invite on to the "create account" page."""
    templates = request.app.state.templates
    settings = request.app.state.settings
    if not request.app.state.login_limiter.allow(client_ip(request)):
        return message_page(request, en.TOO_MANY_REQUESTS, 429)
    invite = invites.find_open(db, code[:64])
    usable = invite is not None
    mas = request.app.state.matrix_admin
    if invite is not None and mas is not None:
        try:
            usable = await mas.registration_token_usable(invite.mas_token_id)
        except MatrixAdminError as e:
            # MAS checks the code again at sign-up; going on is harmless.
            request.app.state.log.warning("could not check an invite: %s", e)
    if not usable:
        return message_page(request, en.JOIN_INVALID, 410, en.JOIN_INVALID_TITLE)
    code = code.strip().upper()
    no_store = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
    domain = invites.cookie_domain(settings.public_url, settings.matrix_auth_public_url)
    if domain is None:
        return templates.TemplateResponse(
            request,
            "join.html",
            {"title": en.JOIN_TITLE, "en": en, "code": code},
            headers=no_store,
        )
    response = RedirectResponse("/auth/login?signup=1", status_code=303, headers=no_store)
    # Not HttpOnly: MAS' page script reads it. It holds only this single-use code,
    # which is in the link anyway, and lives for an hour at most.
    response.set_cookie(
        invites.COOKIE,
        code,
        max_age=invites.COOKIE_SECONDS,
        path="/",
        domain=domain,
        secure=settings.cookie_secure,
        httponly=False,
        samesite="lax",
    )
    return response
