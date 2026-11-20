"""Deliver the app's vote messages to members, in their direct chat with the bot.

The app decides what is sent and to whom; this only finds each member's direct
chat and posts the text. A member without one is invited to an encrypted chat
and gets the message once they have joined, because an encrypted message can
only be read by those who were in the room when it was sent. Message text is
never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from bot.app_client import AppError

log = logging.getLogger("commons.bot.notify")

POLL_SECONDS = 15.0
# About an hour of waiting for someone to accept the invite or for Matrix to recover.
MAX_TRIES = 240


class Rooms(Protocol):
    async def joined_rooms(self) -> list[str]: ...
    async def members(self, room_id: str) -> dict[str, str]: ...  # user id -> membership
    async def create_direct(self, user_id: str) -> str: ...
    async def can_encrypt_for(self, room_id: str, user_id: str) -> bool: ...
    async def send(self, room_id: str, text: str, html: str | None = None) -> str: ...
    async def react(self, room_id: str, event_id: str, key: str) -> None: ...
    async def invite(self, room_id: str, user_id: str) -> None: ...


class Outbox(Protocol):
    async def notifications(self) -> list[dict[str, Any]]: ...
    async def delivered(self, notification_id: int) -> None: ...
    async def vote_message(
        self, proposal_id: int, kind: str, room_id: str, event_id: str, user_id: str | None
    ) -> None: ...


@dataclass
class DirectChat:
    room_id: str
    joined: bool


class Notifier:
    def __init__(
        self,
        rooms: Rooms,
        app: Outbox,
        bot_user_id: str,
        community_room: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.rooms = rooms
        self.community_room = community_room
        self.app = app
        self.bot = bot_user_id
        self.server = bot_user_id.split(":", 1)[1]
        self._sent: dict[int, set[str]] = {}
        self._tries: dict[int, int] = {}
        self._invited: set[tuple[int, str]] = set()

    async def direct_chats(self) -> dict[str, DirectChat]:
        """Rooms holding just the bot and one other person, by that person."""
        chats: dict[str, DirectChat] = {}
        shared = self.community_room()
        for room_id in await self.rooms.joined_rooms():
            if room_id == shared:
                continue  # the COMMONS room is never anyone's private chat
            members = await self.rooms.members(room_id)
            present = {u: m for u, m in members.items() if m in ("join", "invite")}
            others = [u for u in present if u != self.bot]
            if self.bot not in present or len(others) != 1:
                continue
            user = others[0]
            joined = present[user] == "join"
            if user not in chats or (joined and not chats[user].joined):
                chats[user] = DirectChat(room_id, joined)
        return chats

    async def _deliver_to(self, user: str, chats: dict[str, DirectChat], item: dict) -> bool:
        """True once the message was sent; False means try again later."""
        chat = chats.get(user)
        if chat is None:
            chats[user] = DirectChat(await self.rooms.create_direct(user), False)
            return False
        if not chat.joined or not await self.rooms.can_encrypt_for(chat.room_id, user):
            return False
        event_id = await self.rooms.send(chat.room_id, item["text"], item.get("html"))
        keys = item.get("keys") or []
        if keys and isinstance(item.get("proposal_id"), int):
            # Remember the message first, so a quick tap already counts.
            await self.app.vote_message(item["proposal_id"], "direct", chat.room_id, event_id, user)
            for key in keys:
                try:
                    await self.rooms.react(chat.room_id, event_id, key)
                except Exception as e:  # the typed "vote 12 1" still works
                    log.warning("could not add a vote button: %s", type(e).__name__)
        return True

    async def tick(self) -> None:
        items = await self.app.notifications()
        if not items:
            return
        chats = await self.direct_chats()
        for item in items:
            nid = item["id"]
            sent = self._sent.setdefault(nid, set())
            waiting = False
            for user in item["recipients"]:
                if user == self.bot or user in sent or not user.endswith(":" + self.server):
                    continue
                room = item.get("invite_to")
                if room and (nid, user) not in self._invited:
                    self._invited.add((nid, user))
                    try:  # into the COMMONS room too; fine if they are there already
                        await self.rooms.invite(room, user)
                    except Exception as e:
                        log.info("no invite to the community room: %s", type(e).__name__)
                try:
                    if await self._deliver_to(user, chats, item):
                        sent.add(user)
                    else:
                        waiting = True
                except Exception as e:  # one member's problem must not block the others
                    log.warning("could not message a member: %s", type(e).__name__)
                    waiting = True
            self._tries[nid] = self._tries.get(nid, 0) + 1
            limit = item.get("max_tries") or MAX_TRIES
            if not waiting or self._tries[nid] >= limit:
                await self.app.delivered(nid)
                self._sent.pop(nid, None)
                self._tries.pop(nid, None)

    async def run(self, interval: float = POLL_SECONDS) -> None:
        while True:
            try:
                await self.tick()
            except AppError as e:
                log.warning("could not fetch vote messages: %s", e)
            except Exception:
                log.exception("sending vote messages failed")
            await asyncio.sleep(interval)
