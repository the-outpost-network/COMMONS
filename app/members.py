"""Member service: adding members, roles and deactivation.

The web admin page and the command line both go through these functions.
Callers check permissions; these functions enforce the invariants:

- every member holds the ``member`` role (deactivate instead of revoking it);
- there is always at least one active admin, so nobody can lock the community
  out of its own COMMONS (``LastAdminError``);
- a Matrix account on this homeserver *is* a COMMONS membership: the first time
  an account signs in or writes to the bot, it becomes a member with the role
  ``member`` (`ensure_member`). Accounts only exist through admin invites
  (MAS sign-up needs a registration token, federation is off).

Granting and revoking roles and deactivating a member append ``role.granted``,
``role.revoked`` and ``member.deactivated`` to the decision log in the same
transaction, so they need the log ``signer``. The ``member`` role every member
holds is not logged: it comes with the account (its invite is logged instead).
Reactivation is not logged yet (there is no decision-log kind for it).
"""

from __future__ import annotations

import unicodedata
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import decision_log
from app.matrix_oauth import USER_ID_RE
from app.models import ROLES, AuthEvent, Member, MemberRole, utcnow
from app.models import Session as WebSession

# Where an admin action came from; recorded in the decision-log payload.
Channel = Literal["web", "cli"]


class MemberError(ValueError):
    pass


class InvalidUserId(MemberError):
    pass


class AlreadyMember(MemberError):
    pass


class UnknownRole(MemberError):
    pass


class MemberRoleFixed(MemberError):
    pass


class LastAdminError(MemberError):
    pass


class HasHistory(MemberError):
    pass


class CannotDeleteSelf(MemberError):
    pass


def normalize_user_id(raw: str, server_name: str) -> str:
    """Turn "nora" or "@nora:<server>" into a Matrix id on our own homeserver."""
    raw = raw.strip()
    user_id = raw if raw.startswith("@") else f"@{raw.lower()}:{server_name}"
    if not USER_ID_RE.match(user_id):
        raise InvalidUserId(f"not a Matrix user id: {raw!r}")
    if user_id.split(":", 1)[1] != server_name:
        raise InvalidUserId(f"{user_id} is not an account on this homeserver ({server_name})")
    return user_id


def get(db: Session, member_id: int) -> Member | None:
    return db.get(Member, member_id)


def get_by_matrix_id(db: Session, matrix_user_id: str) -> Member | None:
    return db.scalar(select(Member).where(Member.matrix_user_id == matrix_user_id))


def ensure_member(db: Session, matrix_user_id: str, server_name: str) -> Member | None:
    """The member for a Matrix account on this homeserver, created on first contact.

    Returns None for accounts on other servers. A deactivated member is returned
    as it is (still deactivated): the caller refuses it. Two first sign-ins at
    the same moment are safe: the unique Matrix id lets one insert win.
    """
    member = get_by_matrix_id(db, matrix_user_id)
    if member is not None:
        return member
    if not USER_ID_RE.match(matrix_user_id) or matrix_user_id.split(":", 1)[1] != server_name:
        return None
    try:
        with db.begin_nested():
            member = add_member(db, matrix_user_id)
            queue_welcome(db, member)
    except IntegrityError:
        member = get_by_matrix_id(db, matrix_user_id)
    return member


def queue_welcome(db: Session, member: Member) -> None:
    """Ask the bot to open a private chat with a new member and say hello (once)."""
    from app.models import ChatNotification

    exists = db.scalar(
        select(ChatNotification.id).where(
            ChatNotification.kind == "welcome", ChatNotification.member_id == member.id
        )
    )
    if exists is None:
        db.add(ChatNotification(kind="welcome", member_id=member.id, created_at=utcnow()))
        db.flush()


def add_member(
    db: Session,
    matrix_user_id: str,
    display_name: str | None = None,
    roles: tuple[str, ...] = ("member",),
    granted_by: int | None = None,
    *,
    signer: decision_log.Signer | None = None,
    channel: Channel = "cli",
) -> Member:
    """Create a member with the role ``member``, plus ``roles``.

    Roles beyond ``member`` are logged (``role.granted``), so they need ``signer``.
    """
    if not USER_ID_RE.match(matrix_user_id):
        raise InvalidUserId(f"not a Matrix user id: {matrix_user_id!r}")
    if display_name is not None and not (1 <= len(display_name) <= 100):
        raise MemberError("display name must be 1 to 100 characters")
    extra = [r for r in dict.fromkeys(roles) if r != "member"]
    for role in extra:
        _check_role(role)
    if extra and signer is None:
        raise decision_log.DecisionLogError("granting roles needs the decision-log signer")
    if get_by_matrix_id(db, matrix_user_id):
        raise AlreadyMember(f"{matrix_user_id} is already a member")
    member = Member(matrix_user_id=matrix_user_id, display_name=display_name)
    db.add(member)
    db.flush()
    _add_role(db, member, "member", granted_by)
    for role in extra:
        grant_role(db, member, role, granted_by, signer=signer, channel=channel)
    return member


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise UnknownRole(f"unknown role {role!r}; use one of {', '.join(ROLES)}")


def _add_role(db: Session, member: Member, role: str, granted_by: int | None) -> None:
    db.add(MemberRole(member_id=member.id, role=role, granted_at=utcnow(), granted_by=granted_by))
    db.flush()
    db.refresh(member)


def _subject(member: Member, channel: Channel) -> dict[str, object]:
    """Who a membership entry is about: id plus Matrix id, so the log reads on its own."""
    return {"member_id": member.id, "matrix_user_id": member.matrix_user_id, "channel": channel}


def grant_role(
    db: Session,
    member: Member,
    role: str,
    granted_by: int | None = None,
    *,
    signer: decision_log.Signer,
    channel: Channel,
) -> None:
    """Give ``member`` a role and log ``role.granted`` (nothing if they already hold it).

    ``granted_by`` is the acting admin (None from the command line).
    """
    _check_role(role)
    if role in member.role_names:
        return
    _add_role(db, member, role, granted_by)
    decision_log.append(
        db, signer, "role.granted", {**_subject(member, channel), "role": role}, granted_by
    )


def _other_active_admins(db: Session, member: Member) -> int:
    """Count active admins other than ``member``, locking the admin rows.

    ``FOR UPDATE`` makes two admins who remove each other at the same moment
    queue up: the second one sees the first change and is refused.
    """
    rows = db.scalars(
        select(MemberRole.member_id)
        .join(Member, Member.id == MemberRole.member_id)
        .where(MemberRole.role == "admin", Member.deactivated_at.is_(None))
        .with_for_update()
    ).all()
    return sum(1 for member_id in rows if member_id != member.id)


def _ensure_not_last_admin(db: Session, member: Member) -> None:
    if "admin" in member.role_names and member.is_active and _other_active_admins(db, member) == 0:
        raise LastAdminError(
            f"{member.matrix_user_id} is the last admin; make someone else admin first"
        )


def revoke_role(
    db: Session,
    member: Member,
    role: str,
    revoked_by: int | None = None,
    *,
    signer: decision_log.Signer,
    channel: Channel,
) -> None:
    """Take a role away and log ``role.revoked`` (nothing if they don't hold it)."""
    _check_role(role)
    if role == "member":
        raise MemberRoleFixed("every member has the role 'member'; deactivate the member instead")
    if role not in member.role_names:
        return
    if role == "admin":
        _ensure_not_last_admin(db, member)
    db.execute(delete(MemberRole).where(MemberRole.member_id == member.id, MemberRole.role == role))
    db.flush()
    db.refresh(member)
    decision_log.append(
        db, signer, "role.revoked", {**_subject(member, channel), "role": role}, revoked_by
    )


def deactivate(
    db: Session,
    member: Member,
    deactivated_by: int | None = None,
    *,
    signer: decision_log.Signer,
    channel: Channel,
) -> None:
    """End membership: no sign-in, no chat commands. History stays attributed.

    Logs ``member.deactivated`` (nothing if the member is already deactivated).
    """
    if not member.is_active:
        return
    _ensure_not_last_admin(db, member)
    now = utcnow()
    member.deactivated_at = now
    db.execute(
        update(WebSession)
        .where(WebSession.member_id == member.id, WebSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.flush()
    decision_log.append(db, signer, "member.deactivated", _subject(member, channel), deactivated_by)


def delete_member(db: Session, member: Member, acting: Member) -> None:
    """Remove a member from COMMONS completely (for test accounts and mistakes).

    Only possible while nothing else in COMMONS points at the member: every table
    that records an action refers to `member.id`, so the database refuses the
    delete (``HasHistory``) once they have done anything, e.g. created an invite
    or granted a role. Members with history are deactivated instead, so the
    record stays attributed. Their own roles, sessions and sign-in history go
    with them. Never the last admin, never yourself. The caller commits.
    """
    if member.id == acting.id:
        raise CannotDeleteSelf("you can't delete your own membership")
    _ensure_not_last_admin(db, member)
    try:
        with db.begin_nested():
            db.execute(delete(WebSession).where(WebSession.member_id == member.id))
            db.execute(delete(AuthEvent).where(AuthEvent.member_id == member.id))
            db.execute(delete(MemberRole).where(MemberRole.member_id == member.id))
            db.execute(delete(Member).where(Member.id == member.id))
    except IntegrityError as e:
        raise HasHistory(f"{member.matrix_user_id} has records in COMMONS") from e
    db.expunge(member)


def reactivate(db: Session, member: Member) -> None:
    member.deactivated_at = None
    db.flush()


def has_active_admin(db: Session) -> bool:
    return (
        db.scalar(
            select(MemberRole.member_id)
            .join(Member, Member.id == MemberRole.member_id)
            .where(MemberRole.role == "admin", Member.deactivated_at.is_(None))
            .limit(1)
        )
        is not None
    )


def admin_names(db: Session) -> list[str]:
    """The active admins' names, for "ask Ada or Frida" on pages with nothing set up yet."""
    from app.calendar import member_name  # app.calendar imports this module

    admins = db.scalars(
        select(Member)
        .join(MemberRole, MemberRole.member_id == Member.id)
        .where(MemberRole.role == "admin", Member.deactivated_at.is_(None))
        .order_by(Member.id)
    )
    return [member_name(m) for m in admins]


def list_members(db: Session) -> list[Member]:
    return list(db.scalars(select(Member).order_by(Member.id)))


DISPLAY_NAME_MAX = 64
_QUOTES = "\"'\u201c\u201d\u2018\u2019\u00ab\u00bb"


def clean_display_name(raw: str) -> str:
    """Normalize a requested display name, or raise MemberError.

    One line, 1 to DISPLAY_NAME_MAX characters, no control or format characters
    (e.g. right-to-left overrides), and not starting with "@" so nobody can look
    like a Matrix user id in a room.
    """
    name = " ".join(raw.split()).strip(_QUOTES + " .!")
    if not name or len(name) > DISPLAY_NAME_MAX or name.startswith("@"):
        raise MemberError("invalid display name")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise MemberError("invalid display name")
    return name


def store_display_name(db: Session, member: Member, name: str | None) -> bool:
    """Update COMMONS' copy of the Matrix display name. Returns True if it changed."""
    if name is not None:
        name = name[:100]
    if member.display_name == name:
        return False
    member.display_name = name
    db.flush()
    return True
