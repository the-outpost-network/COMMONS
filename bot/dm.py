"""Private messages from COMMONS to one member: "you said you'd give back the saw".

The app decides who gets what and writes the text (app/reminders.py); the
bot only delivers it. A feed item of a kind in `DM_KINDS` names the member in
`to` and is sent to them alone, never to the community room.

The message goes to the member's existing 1:1 room with the bot (where they
book and log), else to a new encrypted direct room the bot invites them to.
Like bot/feed.py, no Matrix library here (bot/main.py adapts mautrix to
`Matrix`), so the logic is easy to test.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger("commons.bot.dm")

DM_KINDS = ("booking.overdue", "backup.failed")


class Matrix(Protocol):
    bot_user_id: str

    async def joined_rooms(self) -> list[str]: ...
    async def members(self, room_id: str) -> set[str]: ...  # joined and invited
    async def create_direct(self, user_id: str) -> str: ...
    async def send_text(self, room_id: str, text: str) -> None: ...


class DirectMessages:
    def __init__(
        self,
        matrix: Matrix,
        *,
        server_name: str,
        community_room: Callable[[], str | None] = lambda: None,
        replay_max_age: float = 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.matrix = matrix
        self.server_name = server_name
        self.community_room = community_room
        self.replay_max_age = replay_max_age
        self.clock = clock
        self._rooms: dict[str, str] = {}  # member -> their room with the bot

    async def on_feed(self, item: dict[str, Any]) -> bool:
        """Deliver `item` if it is a private message; False if it is a room line."""
        if item.get("kind") not in DM_KINDS:
            return False
        to, text = item.get("to"), item.get("summary")
        if not isinstance(to, str) or not to.endswith(":" + self.server_name):
            log.warning("private message without a local member; dropped")
            return True
        if not isinstance(text, str) or not text or self._too_old(item.get("at")):
            return True
        try:
            room_id = await self.room_for(to)
            await self.matrix.send_text(room_id, text)
        except Exception as e:  # a Matrix error must not stop the stream
            self._rooms.pop(to, None)
            log.warning("could not send a private message: %s", type(e).__name__)
        return True

    async def room_for(self, user_id: str) -> str:
        """The 1:1 room of the bot and `user_id` (never the community room), else a new one."""
        if (room := self._rooms.get(user_id)) is not None:
            return room
        pair = {self.matrix.bot_user_id, user_id}
        avoid = self.community_room()
        for room_id in await self.matrix.joined_rooms():
            if room_id != avoid and await self.matrix.members(room_id) == pair:
                self._rooms[user_id] = room_id
                return room_id
        room_id = await self.matrix.create_direct(user_id)
        self._rooms[user_id] = room_id
        return room_id

    def _too_old(self, at: Any) -> bool:
        if not isinstance(at, str):
            return False
        try:
            when = datetime.fromisoformat(at).timestamp()
        except ValueError:
            return False
        return self.clock() - when > self.replay_max_age
