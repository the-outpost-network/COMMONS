"""The COMMONS room: post the app's feed quietly and keep the topic current (D6).

No Matrix library here (bot/main.py adapts mautrix to `Room`), so the logic is
easy to test:

- Updates are posted as `m.notice`: Matrix' default push rules don't notify
  for notices, so members see an unread count, not a ping.
- Lines arriving within `batch_seconds` go into one message.
- A fix to an entry the bot posted in the last 15 minutes edits that message
  (or the batch still waiting) instead of posting again; later fixes get
  their own line, because then the history matters.
- The room topic follows the levels; it is only changed when the text changes.
  Without the rights to change it, the bot says so once.
- A low-level alert (D7) is the exception: it goes out at once as a normal
  message, so members do get a notification. Lines already waiting are
  posted first, so the entry that caused it comes before it.
- After a long outage, replayed lines older than `replay_max_age` are skipped
  rather than flooding the room.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.i18n import en

log = logging.getLogger("commons.bot.feed")

EDIT_SECONDS = 15 * 60
LEDGER_FIXES = ("ledger.entry_edited", "ledger.entry_corrected")
ALERTS = ("ledger.level_low", "forecast.shortage")
VOTE_OPENED = "proposal.opened"
VOTE_ENDED = ("proposal.closed", "proposal.cancelled")


# --- Server-Sent Events -------------------------------------------------------------


@dataclass
class Message:
    event: str = "message"
    data: str = ""
    id: str | None = None


class SSEParser:
    """Feed it lines; it returns a Message at each blank line (comments ignored)."""

    def __init__(self) -> None:
        self._cur = Message()
        self._data: list[str] = []

    def feed(self, line: str) -> Message | None:
        line = line.rstrip("\r\n")
        if not line:
            if not self._data and self._cur.id is None:
                self._cur = Message()
                return None
            msg = self._cur
            msg.data = "\n".join(self._data)
            self._cur, self._data = Message(), []
            return msg
        if line.startswith(":"):
            return None  # ping
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._cur.event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            self._cur.id = value
        return None


# --- where the stream left off -----------------------------------------------------


class LastId:
    """The newest feed id handled and whose feed it is, kept across restarts.

    Feed ids only mean something within one database. When the app comes back on
    another one (the demo database, say), the bot starts from that feed's newest
    line: nothing is skipped because of a higher old id, and nothing is reposted.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.value: int | None = None
        self.scope: str | None = None
        if path is not None and path.exists():
            # "<id> <scope>"; a file from before scopes holds only the id.
            number, _, scope = path.read_text().strip().partition(" ")
            self.value = int(number) if number.isdigit() else None
            self.scope = scope or None

    def begin(self, scope: Any, latest: Any) -> None:
        """Line up with the feed the app announced in its `hello`."""
        if not isinstance(latest, int):
            return
        scope = scope if isinstance(scope, str) and scope else None
        moved = self.scope is not None and scope is not None and scope != self.scope
        if self.value is None or moved or self.value > latest:
            self.value = latest
        self.scope = scope or self.scope
        self._save()

    def set(self, value: int) -> None:
        if self.value is not None and value <= self.value:
            return
        self.value = value
        self._save()

    def _save(self) -> None:
        if self.path is not None and self.value is not None:
            tmp = self.path.with_suffix(".tmp")
            tail = f" {self.scope}" if self.scope else ""
            tmp.write_text(f"{self.value}{tail}\n")
            os.replace(tmp, self.path)


# --- posting ------------------------------------------------------------------------


class Room(Protocol):
    async def send_notice(self, room_id: str, text: str) -> str: ...
    async def send_alert(self, room_id: str, text: str) -> str: ...
    async def edit_notice(self, room_id: str, event_id: str, text: str) -> None: ...
    async def set_topic(self, room_id: str, text: str) -> bool: ...
    async def send_announcement(self, room_id: str, text: str, html: str | None) -> str: ...
    async def send_reply(
        self, room_id: str, reply_to: str | None, text: str, html: str | None
    ) -> str: ...


# Told which room message announces which vote: (proposal id, room id, event id).
Announced = Callable[[int, str, str], Awaitable[None]]


@dataclass
class Post:
    room_id: str
    event_id: str
    posted_at: float
    lines: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(self.lines.values())


class RoomFeed:
    def __init__(
        self,
        room: Room,
        *,
        batch_seconds: float = 20.0,
        edit_seconds: float = EDIT_SECONDS,
        replay_max_age: float = 24 * 3600,
        clock: Callable[[], float] = time.time,
        on_announced: Announced | None = None,
    ) -> None:
        self.room = room
        self.on_announced = on_announced
        self.room_id: str | None = None
        self.batch_seconds = batch_seconds
        self.edit_seconds = edit_seconds
        self.replay_max_age = replay_max_age
        self.clock = clock
        self._pending: dict[str, str] = {}
        self._posts: dict[str, Post] = {}  # entry key -> the message showing it
        self._timer: asyncio.Task | None = None
        self._topic: str | None = None  # the newest levels text
        self._topic_set: str | None = None  # what the room topic is now
        self._warned_topic = False

    # stream messages

    async def handle(self, event: str, data: dict[str, Any]) -> None:
        if event == "hello":
            await self.set_room(data.get("room_id"))
            await self._update_topic(data.get("topic"))
        elif event == "room":
            await self.set_room(data.get("room_id"))
        elif event == "feed":
            await self.on_feed(data)

    async def set_room(self, room_id: Any) -> None:
        room_id = room_id if isinstance(room_id, str) and room_id.startswith("!") else None
        if room_id == self.room_id:
            return
        await self.flush()
        self.room_id = room_id
        self._posts.clear()
        self._topic_set = None
        self._warned_topic = False
        log.info("community room %s", "set" if room_id else "not set")
        await self._update_topic(self._topic)

    async def on_feed(self, item: dict[str, Any]) -> None:
        if self._too_old(item.get("at")):
            return
        key = f"entry:{item['entry_id']}" if isinstance(item.get("entry_id"), int) else None
        line = item.get("line") if isinstance(item.get("line"), str) else None
        summary = str(item.get("summary") or "")
        if item.get("kind") == VOTE_OPENED or item.get("kind") in VOTE_ENDED:
            await self._vote(item)
            return
        if item.get("kind") in ALERTS and summary:
            await self._alert(summary)
        elif item.get("kind") in LEDGER_FIXES and key and line:
            if key in self._pending:
                self._pending[key] = line  # not posted yet: just show it fixed
            elif (post := self._posts.get(key)) and self._editable(post):
                post.lines[key] = line
                await self._edit(post)
            else:
                self._pending[f"feed:{item.get('id')}"] = summary
        elif key and line:
            self._pending[key] = line
        elif summary:
            self._pending[f"feed:{item.get('id')}"] = summary
        await self._update_topic(item.get("topic"))
        self._schedule()

    # batching

    def _schedule(self) -> None:
        if self._pending and (self._timer is None or self._timer.done()):
            self._timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.batch_seconds)
        await self.flush()

    async def flush(self) -> None:
        timer = self._timer
        if timer is not None and not timer.done() and timer is not asyncio.current_task():
            timer.cancel()
        pending, self._pending = self._pending, {}
        if not pending or self.room_id is None:
            return
        post = Post(self.room_id, "", self.clock(), dict(pending))
        try:
            post.event_id = await self.room.send_notice(self.room_id, post.text)
        except Exception as e:  # a Matrix error must not stop the stream
            log.warning("could not post to the community room: %s", type(e).__name__)
            return
        self._prune()
        for key in pending:
            if key.startswith("entry:"):
                self._posts[key] = post

    async def _alert(self, text: str) -> None:
        await self.flush()
        if self.room_id is None:
            return
        try:
            await self.room.send_alert(self.room_id, text)
        except Exception as e:  # a Matrix error must not stop the stream
            log.warning("could not post an alert to the community room: %s", type(e).__name__)

    async def _vote(self, item: dict[str, Any]) -> None:
        """A vote: announce it (@room), or reply to the announcement with how it ended."""
        await self.flush()
        text = item.get("text") or item.get("summary")
        html = item.get("html") if isinstance(item.get("html"), str) else None
        if self.room_id is None or not isinstance(text, str) or not text:
            return
        try:
            if item.get("kind") == VOTE_OPENED:
                event_id = await self.room.send_announcement(self.room_id, text, html)
                pid = item.get("proposal_id")
                if self.on_announced is not None and isinstance(pid, int):
                    await self.on_announced(pid, self.room_id, event_id)
                return
            reply_to = item.get("reply_to") if item.get("reply_room") == self.room_id else None
            await self.room.send_reply(self.room_id, reply_to, text, html)
        except Exception as e:  # a Matrix or app error must not stop the stream
            log.warning("could not post a vote to the community room: %s", type(e).__name__)

    async def close(self) -> None:
        await self.flush()
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer

    # edits, topic

    def _editable(self, post: Post) -> bool:
        return post.room_id == self.room_id and self.clock() - post.posted_at < self.edit_seconds

    def _prune(self) -> None:
        for key in [k for k, p in self._posts.items() if not self._editable(p)]:
            del self._posts[key]

    async def _edit(self, post: Post) -> None:
        try:
            await self.room.edit_notice(post.room_id, post.event_id, post.text)
        except Exception as e:
            log.warning("could not edit a community room post: %s", type(e).__name__)

    async def _update_topic(self, text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        self._topic = text
        if self.room_id is None or text == self._topic_set:
            return
        try:
            ok = await self.room.set_topic(self.room_id, text)
        except Exception as e:
            log.warning("could not set the room topic: %s", type(e).__name__)
            return
        if ok:
            self._topic_set = text
        elif not self._warned_topic:
            self._warned_topic = True
            with contextlib.suppress(Exception):
                await self.room.send_notice(self.room_id, en.BOT_TOPIC_NO_RIGHTS)

    def _too_old(self, at: Any) -> bool:
        if not isinstance(at, str):
            return False
        try:
            when = datetime.fromisoformat(at).timestamp()
        except ValueError:
            return False
        return self.clock() - when > self.replay_max_age


def parse_data(msg: Message) -> dict[str, Any] | None:
    try:
        data = json.loads(msg.data) if msg.data else {}
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
