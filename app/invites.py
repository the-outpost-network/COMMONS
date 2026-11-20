"""Sign-up invites: an admin creates one, a new person uses it to make an account.

An invite is a MAS registration token: single use, valid for
``settings.invite_hours``. MAS checks it on its own sign-up page, so the new
person's password is typed into MAS only and never passes through COMMONS.

Every account on this homeserver is a COMMONS member (`members.ensure_member`),
so it does not matter whether the person signs up through the COMMONS link or
directly in Element with the code: they are a member from their first sign-in
or chat message. Roles beyond ``member`` are granted afterwards by an admin.

COMMONS keeps the SHA-256 of the code (to recognise its /join/<code> link) and
MAS' token id; the code itself is shown to the admin once.

Creating an invite appends ``member.invited`` to the decision log in the same
transaction. The entry holds the invite id, its expiry and the inviting admin
(the envelope's actor), never the code, its hash or the admin's note: the log
is permanent and readable by every member.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import decision_log
from app.matrix_admin import MatrixAdmin
from app.models import Invite, Member, utcnow

# No 0/O, 1/I/L: easy to read aloud and to type on a phone. 20 characters from
# 31 symbols is about 99 bits, far beyond guessing.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-Z0-9]{4}(-[A-Z0-9]{4}){4}$")
NOTE_MAX = 100

# The /join link hands the code to MAS' sign-up page in this cookie (app/join.py).
COOKIE = "commons_invite"
COOKIE_SECONDS = 3600


def cookie_domain(public_url: str, auth_public_url: str) -> str | None:
    """COMMONS' host, if MAS lives on a subdomain of it (else None)."""
    own = urlsplit(public_url).hostname or ""
    auth = urlsplit(auth_public_url).hostname or ""
    return own if own and auth.endswith("." + own) else None


def clear_cookie(request: Request, response: Response) -> None:
    s = request.app.state.settings
    domain = cookie_domain(s.public_url, s.matrix_auth_public_url)
    if domain and COOKIE in request.cookies:
        response.delete_cookie(COOKIE, path="/", domain=domain, secure=s.cookie_secure)


class InviteError(ValueError):
    pass


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(20))
    return "-".join(raw[i : i + 4] for i in range(0, 20, 4))


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def clean_note(raw: str) -> str | None:
    note = " ".join(raw.split())
    if len(note) > NOTE_MAX:
        raise InviteError("note too long")
    return note or None


async def create(
    db: Session,
    mas: MatrixAdmin,
    creator: Member,
    hours: int,
    note: str | None,
    *,
    signer: decision_log.Signer,
) -> tuple[Invite, str]:
    """Create the MAS token, the invite row and its ``member.invited`` entry.

    Returns the invite and the code. The caller commits. If MAS refuses,
    nothing is stored.
    """
    code = new_code()
    expires_at = utcnow() + timedelta(hours=hours)
    token_id = await mas.create_registration_token(code, expires_at)
    invite = Invite(
        code_hash=hash_code(code),
        mas_token_id=token_id,
        note=note,
        created_by=creator.id,
        expires_at=expires_at,
    )
    db.add(invite)
    db.flush()
    decision_log.append(
        db,
        signer,
        "member.invited",
        {
            "invite_id": invite.id,
            "expires_at": decision_log.format_ts(expires_at),
            "channel": "web",
        },
        creator.id,
    )
    return invite, code


def find_open(db: Session, code: str, now: datetime | None = None) -> Invite | None:
    """The open invite for a code from a /join link, or None."""
    code = code.strip().upper()
    if not CODE_RE.match(code):
        return None
    invite = db.scalar(select(Invite).where(Invite.code_hash == hash_code(code)))
    return invite if invite and invite.is_open(now or utcnow()) else None


def list_open(db: Session) -> list[Invite]:
    now = utcnow()
    return list(
        db.scalars(
            select(Invite)
            .where(Invite.revoked_at.is_(None), Invite.expires_at > now)
            .order_by(Invite.created_at.desc())
        )
    )


def get(db: Session, invite_id: int) -> Invite | None:
    return db.get(Invite, invite_id)


async def revoke(db: Session, mas: MatrixAdmin, invite: Invite) -> None:
    if invite.revoked_at is None:
        await mas.revoke_registration_token(invite.mas_token_id)
        invite.revoked_at = utcnow()
        db.flush()
