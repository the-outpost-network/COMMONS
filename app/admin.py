"""Admin pages: /admin/*. Every route requires the admin role.

Members: create sign-up invites, grant and revoke roles, deactivate and
reactivate. Every account on the homeserver is a member (it joins on its first
sign-in or chat message), so there is no "add member" step: an admin creates an
invite, the person signs up with it, an admin grants extra roles if needed.

The writes go through `app.members` and `app.invites`, which enforce the
invariants (everyone keeps `member`, there is always an active admin) and
append invites, role changes and deactivations to the decision log.
Deactivating a member also locks their Matrix account in MAS.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app import invites, members
from app.auth import DB, Admin, AdminCsrfChecked, CurrentSession
from app.calendar import member_name
from app.i18n import en
from app.matrix_admin import MatrixAdminError
from app.models import ROLES, Member

router = APIRouter(prefix="/admin")

Role = Annotated[str, Form(max_length=20)]


def _page(
    request: Request,
    db: DB,
    current: CurrentSession,
    *,
    done: str | None = None,
    error: str | None = None,
    status: int = 200,
    new_invite: dict | None = None,
    who: Member | None = None,
) -> Response:
    done_text = en.ADMIN_DONE.get(done or "")
    if done_text and "{name}" in done_text:
        # Names the member it was about; without one, the plain line.
        done_text = done_text.format(name=member_name(who)) if who else en.ADMIN_DONE["updated"]
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_members.html",
        {
            "title": en.ADMIN_MEMBERS_TITLE,
            "nav": "settings",
            "section": "members",
            "en": en,
            "current": current,
            "members": members.list_members(db),
            "roles": ROLES,
            "done": done_text,
            "error": error,
            "invites": invites.list_open(db),
            "invite_hours": request.app.state.settings.invite_hours,
            "new_invite": new_invite,
            "tz": request.app.state.settings.timezone,
        },
        status_code=status,
    )


def _error_text(request: Request, e: members.MemberError, member: Member | None) -> str:
    who = member.matrix_user_id if member else ""
    match e:
        case members.LastAdminError():
            return en.ADMIN_LAST_ADMIN.format(user=who)
        case members.MemberRoleFixed():
            return en.ADMIN_MEMBER_ROLE_FIXED
        case members.UnknownRole():
            return en.ADMIN_UNKNOWN_ROLE
        case members.HasHistory():
            return en.ADMIN_DELETE_HAS_HISTORY.format(user=who)
        case members.CannotDeleteSelf():
            return en.ADMIN_DELETE_SELF
    return en.BAD_REQUEST


def _member(db: DB, member_id: int) -> Member:
    m = members.get(db, member_id)
    if m is None:
        raise HTTPException(status_code=404)
    return m


def _updated() -> Response:
    return RedirectResponse("/admin/members?done=updated", status_code=303)


# A new invite link is shown once, on the page the POST redirects to
# (POST/redirect/GET, so reloading never creates another invite). Until then it
# waits here, in memory only, for the admin session that created it; the app runs
# with one worker. After a restart or 10 minutes it is gone: withdraw and recreate.
FRESH_INVITE_SECONDS = 600


def _remember_fresh_invite(request: Request, current: CurrentSession, invite_id: int, code: str):
    fresh = request.app.state.fresh_invites
    now = time.monotonic()
    for key in [k for k, (_, _, deadline) in fresh.items() if deadline < now]:
        del fresh[key]
    fresh[current.session.id_hash] = (invite_id, code, now + FRESH_INVITE_SECONDS)


def _take_fresh_invite(request: Request, db: DB, current: CurrentSession) -> dict | None:
    entry = request.app.state.fresh_invites.pop(current.session.id_hash, None)
    if entry is None or entry[2] < time.monotonic():
        return None
    invite = invites.get(db, entry[0])
    if invite is None:
        return None
    link = f"{request.app.state.settings.public_url}/join/{entry[1]}"
    return {"link": link, "code": entry[1], "invite": invite}


@router.get("/members")
def members_page(
    request: Request,
    db: DB,
    current: Admin,
    done: str | None = None,
    who: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """Admin: list members with their roles and status."""
    return _page(
        request,
        db,
        current,
        done=done,
        new_invite=_take_fresh_invite(request, db, current),
        who=members.get(db, who) if who else None,
    )


def _mas(request: Request):
    return request.app.state.matrix_admin


def _signer(request: Request):
    return request.app.state.log_signer


@router.post("/invites")
async def create_invite(
    request: Request,
    db: DB,
    current: AdminCsrfChecked,
    note: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    """Admin: create a single-use sign-up invite and show its link once."""
    mas = _mas(request)
    if mas is None:
        return _page(request, db, current, error=en.ADMIN_NO_MAS, status=503)
    try:
        invite, code = await invites.create(
            db,
            mas,
            current.member,
            request.app.state.settings.invite_hours,
            invites.clean_note(note),
            signer=_signer(request),
        )
    except invites.InviteError:
        return _page(request, db, current, error=en.BAD_REQUEST, status=400)
    except MatrixAdminError as e:
        db.rollback()
        request.app.state.log.warning("could not create an invite: %s", e)
        return _page(request, db, current, error=en.ADMIN_MAS_FAILED, status=502)
    db.commit()
    _remember_fresh_invite(request, current, invite.id, code)
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(
    request: Request, db: DB, current: AdminCsrfChecked, invite_id: int
) -> Response:
    """Admin: withdraw an invite that has not been used yet."""
    invite = invites.get(db, invite_id)
    if invite is None:
        raise HTTPException(status_code=404)
    mas = _mas(request)
    if mas is None:
        return _page(request, db, current, error=en.ADMIN_NO_MAS, status=503)
    try:
        await invites.revoke(db, mas, invite)
    except MatrixAdminError as e:
        db.rollback()
        request.app.state.log.warning("could not revoke an invite: %s", e)
        return _page(request, db, current, error=en.ADMIN_MAS_FAILED, status=502)
    db.commit()
    return RedirectResponse("/admin/members?done=invite_revoked", status_code=303)


def _apply(request: Request, db: DB, current: CurrentSession, m: Member, action) -> Response:
    try:
        action()
    except members.MemberError as e:
        db.rollback()
        status = 409 if isinstance(e, members.LastAdminError) else 400
        return _page(request, db, current, error=_error_text(request, e, m), status=status)
    db.commit()
    return _updated()


@router.post("/members/{member_id}/grant")
def grant_role(
    request: Request, db: DB, current: AdminCsrfChecked, member_id: int, role: Role
) -> Response:
    """Admin: give a member a role."""
    m = _member(db, member_id)
    return _apply(
        request,
        db,
        current,
        m,
        lambda: members.grant_role(
            db, m, role, current.member.id, signer=_signer(request), channel="web"
        ),
    )


@router.post("/members/{member_id}/revoke")
def revoke_role(
    request: Request, db: DB, current: AdminCsrfChecked, member_id: int, role: Role
) -> Response:
    """Admin: take a role away (never `member`, never from the last admin)."""
    m = _member(db, member_id)
    return _apply(
        request,
        db,
        current,
        m,
        lambda: members.revoke_role(
            db, m, role, current.member.id, signer=_signer(request), channel="web"
        ),
    )


async def _set_active(
    request: Request, db: DB, current: CurrentSession, m: Member, active: bool
) -> Response:
    """Change the membership in COMMONS and lock/unlock the Matrix account with it.

    The COMMONS change is checked first (last-admin rule), then MAS is called,
    then COMMONS commits. If MAS fails, nothing changes in COMMONS either.
    """
    try:
        if active:
            members.reactivate(db, m)
        else:
            members.deactivate(db, m, current.member.id, signer=_signer(request), channel="web")
    except members.MemberError as e:
        db.rollback()
        status = 409 if isinstance(e, members.LastAdminError) else 400
        return _page(request, db, current, error=_error_text(request, e, m), status=status)
    mas = _mas(request)
    if mas is not None:
        try:
            await mas.set_locked(m.matrix_user_id, locked=not active)
        except MatrixAdminError as e:
            db.rollback()
            request.app.state.log.warning("could not change the Matrix account lock: %s", e)
            return _page(request, db, current, error=en.ADMIN_MAS_FAILED, status=502)
    db.commit()
    # Say the chat account is locked only when the chat server locked it.
    what = ("reactivated" if active else "deactivated") + ("" if mas is not None else "_here")
    return RedirectResponse(f"/admin/members?done={what}&who={m.id}", status_code=303)


@router.post("/members/{member_id}/deactivate")
async def deactivate(
    request: Request, db: DB, current: AdminCsrfChecked, member_id: int
) -> Response:
    """Admin: end a membership, its sessions and its chat account (never the last admin)."""
    return await _set_active(request, db, current, _member(db, member_id), active=False)


@router.post("/members/{member_id}/reactivate")
async def reactivate(
    request: Request, db: DB, current: AdminCsrfChecked, member_id: int
) -> Response:
    """Admin: undo a deactivation and unlock the chat account."""
    return await _set_active(request, db, current, _member(db, member_id), active=True)


@router.get("/members/{member_id}/delete")
def delete_confirm(request: Request, db: DB, current: Admin, member_id: int) -> Response:
    """Admin: ask before deleting a member for good."""
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_member_delete.html",
        {
            "title": en.ADMIN_DELETE_TITLE,
            "current": current,
            "m": _member(db, member_id),
            "nav": "settings",
            "section": "members",
        },
    )


@router.post("/members/{member_id}/delete")
async def delete_member(
    request: Request, db: DB, current: AdminCsrfChecked, member_id: int
) -> Response:
    """Admin: delete a member without history and erase their Matrix account.

    COMMONS checks first (history, last admin, not yourself); then MAS
    deactivates and erases the account; then COMMONS commits. If MAS fails,
    nothing is deleted.
    """
    m = _member(db, member_id)
    mas = _mas(request)
    if mas is None:
        return _page(request, db, current, error=en.ADMIN_NO_MAS, status=503)
    user_id = m.matrix_user_id
    try:
        members.delete_member(db, m, current.member)
    except members.MemberError as e:
        db.rollback()
        status = 400 if isinstance(e, members.CannotDeleteSelf) else 409
        return _page(request, db, current, error=_error_text(request, e, m), status=status)
    try:
        await mas.deactivate_and_erase(user_id)
    except MatrixAdminError as e:
        db.rollback()
        request.app.state.log.warning("could not erase the Matrix account: %s", e)
        return _page(request, db, current, error=en.ADMIN_MAS_FAILED, status=502)
    db.commit()
    return RedirectResponse("/admin/members?done=deleted", status_code=303)
