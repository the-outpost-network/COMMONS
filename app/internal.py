"""Internal API for the Matrix bot: /internal/*.

Reachable only on the internal Docker network (Caddy answers 404 for /internal
on every public host). Every route also requires the service token from the
`internal_api_token` secret. The bot passes the Matrix id of the member who
wrote the message; the app looks the member up and acts with *their* rights.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import backup_alert, members, stream, vote_chat
from app.auth import DB
from app.i18n import en
from app.matrix_oauth import USER_ID_RE
from app.models import utcnow

router = APIRouter(prefix="/internal")


def require_service_token(request: Request) -> None:
    """Guard: the caller must present the internal service token."""
    expected: str | None = request.app.state.internal_token
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if (
        not expected
        or scheme.lower() != "bearer"
        or not token
        or not hmac.compare_digest(token.encode(), expected.encode())
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


ServiceToken = Annotated[None, Depends(require_service_token)]


class ChatLine(BaseModel):
    """One earlier message of the chat: from the member ("user") or from COMMONS."""

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=4_000)


class AssistantRequest(BaseModel):
    sender: str = Field(max_length=255, pattern=USER_ID_RE.pattern)
    room_id: str = Field(min_length=2, max_length=255, pattern=r"^![^\s]+$")
    text: str = Field(min_length=1, max_length=10_000)
    # True only for a 1:1 chat with the bot; ballots are refused anywhere else.
    direct: bool = False
    # The chat before this message, oldest first, as the bot read it from the room.
    history: list[ChatLine] = Field(default_factory=list, max_length=20)
    # The message this one replies to, if any: a reply to a vote message is a ballot.
    reply_to: str | None = Field(default=None, max_length=255, pattern=r"^\$[^\s]+$")


class AssistantReply(BaseModel):
    reply: str


class VoteMessage(BaseModel):
    proposal_id: int = Field(ge=1)
    kind: Literal["room", "direct"]
    room_id: str = Field(min_length=2, max_length=255, pattern=r"^![^\s]+$")
    event_id: str = Field(min_length=2, max_length=255, pattern=r"^\$[^\s]+$")
    # direct: the member the message was sent to (and the only one whose taps count)
    user_id: str | None = Field(default=None, max_length=255, pattern=USER_ID_RE.pattern)


class TapVote(BaseModel):
    sender: str = Field(max_length=255, pattern=USER_ID_RE.pattern)
    room_id: str = Field(min_length=2, max_length=255, pattern=r"^![^\s]+$")
    event_id: str = Field(min_length=2, max_length=255, pattern=r"^\$[^\s]+$")
    key: str = Field(min_length=1, max_length=16)


class Notification(BaseModel):
    id: int
    text: str
    html: str
    recipients: list[str]
    proposal_id: int | None = None
    keys: list[str] = []
    # welcome: also invite the member to the COMMONS room, and keep trying for longer.
    invite_to: str | None = None
    max_tries: int | None = None


class Notifications(BaseModel):
    items: list[Notification]


@router.post("/assistant")
async def assistant(
    body: AssistantRequest, request: Request, db: DB, _: ServiceToken
) -> AssistantReply:
    """Internal (service token): answer one chat message as the sending member."""
    history = [{"role": h.role, "content": h.text} for h in body.history]
    reply = await request.app.state.assistant.handle(
        db,
        body.sender,
        body.room_id,
        body.text,
        history,
        # The COMMONS room is shared, however few are in it: never a private chat.
        direct=body.direct and not vote_chat.is_shared_room(db, body.room_id),
        reply_to=body.reply_to,
    )
    return AssistantReply(reply=reply)


@router.post("/vote-messages")
def vote_message(body: VoteMessage, db: DB, _: ServiceToken) -> dict[str, bool]:
    """Internal (service token): which Matrix message shows which vote."""
    if (body.kind == "direct") != (body.user_id is not None):
        raise HTTPException(status_code=400, detail="user_id is for direct messages only")
    if body.kind == "direct" and vote_chat.is_shared_room(db, body.room_id):
        raise HTTPException(status_code=400, detail="the community room is not a private chat")
    if not vote_chat.remember_message(
        db, body.proposal_id, body.kind, body.room_id, body.event_id, body.user_id, utcnow()
    ):
        raise HTTPException(status_code=404, detail="unknown vote or member")
    db.commit()
    return {"ok": True}


@router.post("/votes")
def tap_vote(body: TapVote, request: Request, db: DB, _: ServiceToken) -> dict[str, str | None]:
    """Internal (service token): a tap on a vote message, as the member who tapped.

    Counts only on the vote message in that member's own direct chat, through the
    normal cast_vote (channel matrix). A tap on any other message is not a ballot
    (reply null); a refusal comes back as the reply to post.
    """
    state = request.app.state
    if not vote_chat.is_vote_message(db, body.event_id):
        return {"reply": None}
    member = members.ensure_member(db, body.sender, state.settings.matrix_server_name)
    if member is None or not member.is_active:
        return {"reply": en.VOTE_TAP_NOT_A_MEMBER}
    db.commit()  # keep a membership that was just created
    if not state.tap_limit.allow(body.sender):
        return {"reply": en.VOTE_TAP_RATE_LIMITED}
    reply = vote_chat.tap(
        db,
        member,
        body.event_id,
        body.key,
        room_id=body.room_id,
        signer=state.log_signer,
        now=utcnow(),
        tz=ZoneInfo(state.settings.timezone),
    )
    return {"reply": reply}


@router.get("/notifications")
async def notifications(request: Request, db: DB, _: ServiceToken) -> Notifications:
    """Internal (service token): vote messages the bot still has to send."""
    tz = ZoneInfo(request.app.state.settings.timezone)
    now = utcnow()
    await vote_chat.write_intros(db, request.app.state.llm, now, tz)
    items = vote_chat.pending(db, now=now, tz=tz)
    return Notifications(items=[Notification(**item) for item in items])


@router.post("/notifications/{notification_id}/delivered")
def notification_delivered(
    notification_id: Annotated[int, Path(ge=1)], db: DB, _: ServiceToken
) -> dict[str, bool]:
    """Internal (service token): the bot has finished sending this message."""
    if not vote_chat.mark_delivered(db, notification_id, utcnow()):
        raise HTTPException(status_code=404, detail="unknown notification")
    db.commit()
    return {"ok": True}


@router.get("/events")
async def events_stream(
    request: Request,
    _: ServiceToken,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    feed_scope: Annotated[str | None, Header(alias="X-Commons-Feed-Scope", max_length=64)] = None,
) -> StreamingResponse:
    """Internal (service token): the COMMONS room feed as Server-Sent Events.

    `Last-Event-ID` replays what the bot missed while it was away, if it was
    reading this database's feed (`X-Commons-Feed-Scope`).
    """
    state = request.app.state
    body = stream.stream(
        state.event_hub,
        state.sessionmaker,
        stream.parse_last_id(last_event_id),
        state.settings.events_stream_seconds,
        feed_scope,
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class BackupFailed(BaseModel):
    reason: Literal[backup_alert.REASONS]  # type: ignore[valid-type]


@router.post("/backup-failed")
async def backup_failed(body: BackupFailed, db: DB, _: ServiceToken) -> dict[str, int]:
    """Internal (service token): the host's nightly backup failed; tell every admin."""
    admins = backup_alert.notify_failed(db, body.reason)
    db.commit()
    return {"admins": admins}
