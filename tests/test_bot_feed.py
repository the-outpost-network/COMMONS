"""The bot's side of the COMMONS room (D6): SSE, batching, edits, topic, reconnects."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import anyio
import httpx
import pytest

from app.i18n import en
from bot.app_client import follow_events
from bot.feed import LastId, RoomFeed, SSEParser

ROOM = "!village:example.test"
T0 = 1_800_000_000.0


class FakeRoom:
    def __init__(self, topic_ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.topics: list[str] = []
        self.alerts: list[tuple[str, str]] = []
        self.topic_ok = topic_ok

    async def send_notice(self, room_id: str, text: str) -> str:
        self.sent.append((room_id, text))
        return f"$ev{len(self.sent)}"

    async def send_alert(self, room_id: str, text: str) -> str:
        self.alerts.append((room_id, text))
        return f"$alert{len(self.alerts)}"

    async def edit_notice(self, room_id: str, event_id: str, text: str) -> None:
        self.edits.append((room_id, event_id, text))

    async def set_topic(self, room_id: str, text: str) -> bool:
        if self.topic_ok:
            self.topics.append(text)
        return self.topic_ok


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def at(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def item(id_, kind="ledger.entry_added", entry_id=None, line=None, summary=None, topic=None,
         ts=T0):  # fmt: skip
    return {"id": id_, "kind": kind, "at": at(ts), "entry_id": entry_id,
            "line": line, "summary": summary or line, "topic": topic}  # fmt: skip


def make(**kw) -> tuple[RoomFeed, FakeRoom, Clock]:
    room, clock = FakeRoom(kw.pop("topic_ok", True)), Clock()
    return RoomFeed(room, batch_seconds=3600, clock=clock, **kw), room, clock


def run(fn) -> None:
    anyio.run(fn)


# -- SSE -----------------------------------------------------------------------------


def test_sse_parser():
    p = SSEParser()
    lines = [": ping", "", "id: 7", "event: feed", 'data: {"a": 1}', "", "event: hello",
             "data: {}", ""]  # fmt: skip
    got = [m for m in (p.feed(x) for x in lines) if m]
    assert [(m.event, m.data, m.id) for m in got] == [
        ("feed", '{"a": 1}', "7"),
        ("hello", "{}", None),
    ]


def test_last_id_survives_a_restart(tmp_path):
    path = tmp_path / "feed_last_id"
    last = LastId(path)
    assert last.value is None
    last.set(5)
    last.set(3)  # never goes back
    assert LastId(path).value == 5


# -- posting --------------------------------------------------------------------------


def test_lines_are_batched_into_one_silent_post():
    async def go():
        f, room, _ = make()
        await f.handle("hello", {"room_id": ROOM, "topic": "COMMONS levels: Water 800 l"})
        await f.on_feed(item(1, entry_id=10, line="Nora: Water reading 800 l (#10)"))
        await f.on_feed(item(2, entry_id=11, line="Nora: Firewood added 1 m³ (#11)"))
        assert room.sent == []  # waiting for the batch
        await f.flush()
        assert room.sent == [
            (ROOM, "Nora: Water reading 800 l (#10)\nNora: Firewood added 1 m³ (#11)")
        ]
        await f.close()

    run(go)


def test_the_batch_is_posted_by_itself_after_a_few_seconds():
    async def go():
        room = FakeRoom()
        f = RoomFeed(room, batch_seconds=0.01)
        await f.set_room(ROOM)
        await f.on_feed(item(1, entry_id=10, line="Nora: Water used 5 l (#10)"))
        await asyncio.sleep(0.05)
        assert room.sent == [(ROOM, "Nora: Water used 5 l (#10)")]

    run(go)


def test_a_fix_before_posting_just_changes_the_line():
    async def go():
        f, room, _ = make()
        await f.set_room(ROOM)
        await f.on_feed(item(1, entry_id=10, line="Nora: Water used 300 l (#10)"))
        await f.on_feed(item(2, "ledger.entry_edited", 10, "Nora: Water used 200 l (#10) (edited)",
                             "Nora fixed #10: Water used 300 l → 200 l"))  # fmt: skip
        await f.flush()
        assert room.sent == [(ROOM, "Nora: Water used 200 l (#10) (edited)")]

    run(go)


def test_a_fix_within_15_minutes_edits_the_post():
    async def go():
        f, room, clock = make()
        await f.set_room(ROOM)
        await f.on_feed(item(1, entry_id=10, line="Nora: Water used 300 l (#10)"))
        await f.on_feed(item(2, entry_id=11, line="Nora: Wood added 1 m³ (#11)"))
        await f.flush()
        clock.now += 5 * 60
        undone = "Nora: Water used 300 l (#10) (undone, no longer counts)"
        await f.on_feed(item(3, "ledger.entry_corrected", 10, undone, "Nora undid #10 (…)"))
        assert room.edits == [(ROOM, "$ev1", undone + "\nNora: Wood added 1 m³ (#11)")]
        await f.flush()
        assert len(room.sent) == 1  # nothing new posted

    run(go)


def test_a_later_fix_gets_its_own_line():
    async def go():
        f, room, clock = make()
        await f.set_room(ROOM)
        await f.on_feed(item(1, entry_id=10, line="Nora: Water used 300 l (#10)"))
        await f.flush()
        clock.now += 20 * 60
        await f.on_feed(item(2, "ledger.entry_corrected", 10, "… (corrected to 250 l)",
                             "Ada corrected #10 (Nora: Water used): 300 l → 250 l",
                             ts=clock.now))  # fmt: skip
        await f.flush()
        assert room.edits == []
        assert room.sent[-1] == (ROOM, "Ada corrected #10 (Nora: Water used): 300 l → 250 l")

    run(go)


def test_no_room_no_posts_and_old_replays_are_skipped():
    async def go():
        f, room, _ = make()
        await f.on_feed(item(1, entry_id=10, line="Nora: Water used 5 l (#10)"))
        await f.flush()
        assert room.sent == []
        await f.set_room(ROOM)
        await f.on_feed(item(2, entry_id=11, line="old", ts=T0 - 2 * 24 * 3600))
        await f.flush()
        assert room.sent == []

    run(go)


# -- the topic ----------------------------------------------------------------------


def test_topic_only_changes_when_the_levels_change():
    async def go():
        f, room, _ = make()
        await f.handle("hello", {"room_id": ROOM, "topic": "COMMONS levels: Water 800 l"})
        await f.on_feed(item(1, entry_id=1, line="x", topic="COMMONS levels: Water 800 l"))
        await f.on_feed(item(2, entry_id=2, line="y", topic="COMMONS levels: Water 750 l"))
        assert room.topics == ["COMMONS levels: Water 800 l", "COMMONS levels: Water 750 l"]
        # A new room gets the current levels straight away.
        await f.handle("room", {"room_id": "!new:example.test"})
        assert room.topics[-1] == "COMMONS levels: Water 750 l"

    run(go)


def test_without_rights_for_the_topic_the_bot_says_so_once():
    async def go():
        f, room, _ = make(topic_ok=False)
        await f.handle("hello", {"room_id": ROOM, "topic": "COMMONS levels: Water 800 l"})
        await f.on_feed(item(1, entry_id=1, line="x", topic="COMMONS levels: Water 700 l"))
        assert room.sent == [(ROOM, en.BOT_TOPIC_NO_RIGHTS)]

    run(go)


def test_bad_room_ids_are_ignored():
    async def go():
        f, _, _ = make()
        await f.handle("room", {"room_id": "not a room"})
        assert f.room_id is None

    run(go)


# -- following the stream -------------------------------------------------------------


def test_follow_events_resumes_with_last_event_id(tmp_path):
    seen_headers: list[dict] = []
    bodies = [
        b'event: hello\ndata: {"room_id": null, "topic": "t", "latest_id": 4}\n\n'
        b'id: 5\nevent: feed\ndata: {"id": 5}\n\n',
        b'event: hello\ndata: {"room_id": null, "topic": "t", "latest_id": 6}\n\n'
        b'id: 6\nevent: feed\ndata: {"id": 6}\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        assert request.url.path == "/internal/events"
        return httpx.Response(200, content=bodies[min(len(seen_headers), 2) - 1],
                              headers={"content-type": "text/event-stream"})  # fmt: skip

    async def go():
        got: list[tuple[str, dict]] = []
        stop = asyncio.Event()

        async def handle(event, data):
            got.append((event, data))
            if data.get("id") == 6:
                stop.set()

        last = LastId(tmp_path / "feed_last_id")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await asyncio.wait_for(
                follow_events(http, "http://app:8000", "tok", handle, last, stop), 10
            )
        return got, last.value

    got, last = anyio.run(go)
    assert [e for e, _ in got] == ["hello", "feed", "hello", "feed"]
    assert last == 6
    assert seen_headers[0]["authorization"] == "Bearer tok"
    assert "last-event-id" not in seen_headers[0]
    assert seen_headers[1]["last-event-id"] == "5"


def test_position_is_kept_within_one_database(tmp_path):
    path = tmp_path / "feed_last_id"
    last = LastId(path)
    last.begin("real", 12)
    last.set(14)
    last.begin("real", 20)  # reconnecting: the missed lines 15-20 get replayed
    assert last.value == 14
    again = LastId(path)
    assert (again.value, again.scope) == (14, "real")


@pytest.mark.parametrize("latest", [0, 3, 90])
def test_another_database_starts_at_its_newest_line(tmp_path, latest):
    last = LastId(tmp_path / "feed_last_id")
    last.begin("real", 12)
    last.begin("demo", latest)
    assert (last.value, last.scope) == (latest, "demo")
    last.set(latest + 1)
    last.begin("real", 12)  # back again: no old lines, none skipped
    assert (last.value, last.scope) == (12, "real")


def test_a_position_saved_before_scopes_is_carried_over(tmp_path):
    path = tmp_path / "feed_last_id"
    path.write_text("12\n")
    kept = LastId(path)
    kept.begin("real", 15)
    assert (kept.value, kept.scope) == (12, "real")  # lines 13-15 still get replayed
    path.write_text("12\n")
    newer_database = LastId(path)
    newer_database.begin("demo", 0)
    assert (newer_database.value, newer_database.scope) == (0, "demo")


def test_follow_events_sends_the_scope_it_is_reading(tmp_path):
    path = tmp_path / "feed_last_id"
    path.write_text("7 real\n")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        body = b'event: hello\ndata: {"latest_id": 2, "feed_scope": "demo"}\n\n'
        return httpx.Response(200, content=body)

    async def go():
        stop = asyncio.Event()
        last = LastId(path)

        async def handle(event, data):
            stop.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await asyncio.wait_for(
                follow_events(http, "http://app:8000", "t", handle, last, stop), 10
            )
        return last

    last = anyio.run(go)
    assert seen[0]["x-commons-feed-scope"] == "real"
    assert (last.value, last.scope) == (2, "demo")


def test_follow_events_keeps_going_after_errors(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(200, content=b'event: hello\ndata: {"latest_id": 0}\n\n')

    async def go():
        stop = asyncio.Event()

        async def handle(event, data):
            stop.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await asyncio.wait_for(
                follow_events(http, "http://app:8000", "t", handle, LastId(None), stop), 10
            )

    anyio.run(go)
    assert len(calls) == 2


# -- low-level alerts (D7) --------------------------------------------------------------


def test_an_alert_is_posted_at_once_as_a_normal_message_after_waiting_lines():
    async def go():
        f, room, _ = make()
        await f.set_room(ROOM)
        await f.on_feed(item(1, entry_id=10, line="Nora: Water used 300 l (#10)"))
        await f.on_feed(item(2, "ledger.level_low", summary="⚠️ Water is running low",
                             topic="COMMONS levels: Water 900 l (low)"))  # fmt: skip
        assert room.sent == [(ROOM, "Nora: Water used 300 l (#10)")]
        assert room.alerts == [(ROOM, "⚠️ Water is running low")]
        assert room.topics[-1] == "COMMONS levels: Water 900 l (low)"
        await f.close()

    run(go)


def test_back_above_is_a_quiet_line_and_alerts_need_a_room():
    async def go():
        f, room, _ = make()
        await f.on_feed(item(1, "ledger.level_low", summary="⚠️ low"))
        assert room.alerts == []
        await f.set_room(ROOM)
        await f.on_feed(item(2, "ledger.level_ok", summary="Water is back above"))
        await f.flush()
        assert room.sent == [(ROOM, "Water is back above")]
        assert room.alerts == []

    run(go)
