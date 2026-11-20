"""Server-Sent Events for the bot: GET /internal/events (D6).

The hub subscribes once to the in-process bus (app/events.py) and hands each
committed event to every open stream. The bus calls subscribers in whatever
thread committed: sync routes commit on a threadpool thread, the assistant on
the event loop. So events cross into asyncio with `call_soon_threadsafe`.

A stream starts with a `hello` (the community room, the current topic and the
newest feed id), then replays what the client missed (its `Last-Event-ID`),
then sends new `feed` items live and `room` changes. A ping comment every 15
seconds keeps the connection open. Each stream has a bounded queue; a client
that falls that far behind is cut off and catches up by replay on reconnect.
Streams also end after `events_stream_seconds`, and the bot just reconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app import community, events, feed
from app.events import Event

log = logging.getLogger("commons.stream")

ROOM_KEY = "matrix.community_room"
ROOM_SET = "community.room_set"
QUEUE_SIZE = 200
PING_SECONDS = 15.0
_OVERFLOW = ("overflow", {})


def sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    """One SSE message. JSON never contains a raw newline, so one data line is enough."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class EventHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue] = set()
        self._stop = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stop = events.subscribe(self._on_event)

    def stop(self) -> None:
        if self._stop:
            self._stop()
        self._queues.clear()

    def open(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.add(q)
        return q

    def close(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    @property
    def listeners(self) -> int:
        return len(self._queues)

    def _on_event(self, e: Event) -> None:
        if "feed" in e.data:
            msg = ("feed", e.data["feed"])
        elif e.kind == ROOM_SET:
            msg = ("room", {"room_id": e.data.get("room_id")})
        else:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for q in list(self._queues):
            loop.call_soon_threadsafe(self._put, q, msg)

    @staticmethod
    def _put(q: asyncio.Queue, msg: tuple[str, dict]) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # Too far behind: end this stream; the client replays on reconnect.
            while not q.empty():
                q.get_nowait()
            q.put_nowait(_OVERFLOW)


def _start(
    sm: sessionmaker[Session], last_id: int | None, client_scope: str | None
) -> tuple[dict, list[dict], int | None]:
    with sm() as db:
        scope = feed.scope(db)
        hello = {
            "room_id": community.get(db, ROOM_KEY),
            "topic": feed.topic(db),
            "latest_id": feed.latest_id(db),
            "feed_scope": scope,
        }
        if client_scope and client_scope != scope:
            last_id = None  # a position in another database's feed says nothing here
        replay = feed.since(db, last_id) if last_id is not None else []
    return hello, replay, last_id


async def stream(
    hub: EventHub,
    sm: sessionmaker[Session],
    last_id: int | None,
    max_seconds: float,
    client_scope: str | None = None,
) -> AsyncIterator[str]:
    q = hub.open()  # before reading the database, so nothing falls in between
    try:
        hello, replay, last_id = await run_in_threadpool(_start, sm, last_id, client_scope)
        yield sse("hello", hello)
        sent = last_id if last_id is not None else hello["latest_id"]
        for it in replay:
            yield sse("feed", it, it["id"])
            sent = it["id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds
        while (remaining := deadline - loop.time()) > 0:
            try:
                kind, data = await asyncio.wait_for(q.get(), min(PING_SECONDS, remaining))
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if (kind, data) == _OVERFLOW:
                log.warning("event stream fell behind; closing it")
                return
            if kind == "feed":
                if data["id"] <= sent:
                    continue  # already replayed
                sent = data["id"]
                yield sse("feed", data, data["id"])
            else:
                yield sse(kind, data)
    finally:
        hub.close(q)


def parse_last_id(raw: str | None) -> int | None:
    if raw is None or not raw.strip().isdigit() or len(raw.strip()) > 18:
        return None
    return int(raw.strip())
