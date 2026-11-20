"""The COMMONS room feed (D6): feed rows, the room setting, the bot's event stream."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta

import anyio
import pytest
from sqlalchemy import select
from tests.conftest import INTERNAL_TOKEN, SERVER

from app import categories, community_room, events, feed, ledger, members, stream
from app.i18n import en
from app.models import ConfigKV, DecisionLog, EventFeed, utcnow

NORA = f"@nora:{SERVER}"
ADA = f"@ada:{SERVER}"
ROOM = f"!village:{SERVER}"
AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
Spec = categories.CategorySpec


@pytest.fixture
def resources(db):
    water = categories.create(
        db,
        Spec(slug="water", name="Water", unit="l", kind="stock", shortage_threshold=1000,
             capacity=3000),
    )  # fmt: skip
    wood = categories.create(
        db, Spec(slug="firewood", name="Firewood", unit="m³", kind="stock", shortage_threshold=2)
    )
    db.commit()
    return water, wood


@pytest.fixture
def ada(db, log_signer):
    m = members.add_member(db, ADA, "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


@pytest.fixture
def heard():
    got: list[events.Event] = []
    stop = events.subscribe(got.append)
    yield got
    stop()


def add(db, member, category, entry_type, amount, minutes_ago=0):
    now = utcnow() - timedelta(minutes=minutes_ago)
    e = ledger.add_entry(db, member, category, entry_type, amount, channel="matrix", now=now)
    db.commit()
    return e


def rows(db, with_alerts: bool = False) -> list[EventFeed]:
    """Feed rows, oldest first; low-level alerts (D7) only when asked for."""
    db.expire_all()
    q = select(EventFeed).order_by(EventFeed.id)
    if not with_alerts:
        q = q.where(EventFeed.kind.not_in((feed.LEVEL_LOW, feed.LEVEL_OK)))
    return list(db.scalars(q))


def say(client, text: str, sender: str = NORA, room: str = ROOM) -> str:
    r = client.post(
        "/internal/assistant",
        json={"sender": sender, "room_id": room, "text": text},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def messages(body: str) -> list[tuple[str, dict, str | None]]:
    """Parse an SSE body: (event, data, id) per message, pings skipped."""
    out = []
    for block in body.strip().split("\n\n"):
        lines = [x for x in block.split("\n") if x and not x.startswith(":")]
        if not lines:
            continue
        f = dict(x.split(": ", 1) for x in lines)
        out.append((f.get("event", "message"), json.loads(f["data"]), f.get("id")))
    return out


# -- feed rows ------------------------------------------------------------------


def test_a_log_writes_a_feed_line_with_the_levels_in_the_same_commit(db, nora, resources, heard):
    water, wood = resources
    add(db, nora, water, "reading", 1850)
    e = add(db, nora, wood, "add", 1)
    (_, row) = rows(db)
    assert row.kind == "ledger.entry_added"
    assert row.member_id == nora.id
    assert row.summary == f"Nora: Firewood added 1 m³ (#{e.id})"
    assert row.data["entry_id"] == e.id
    assert row.data["line"] == row.summary
    assert row.data["topic"] == "COMMONS levels: Water 1,850 l · Firewood 1 m³ (from 0) (low)"
    # Subscribers get the row with the event, after the commit (then the
    # firewood alert, as 1 m³ is below 2).
    entry_event, alert_event = heard[-2:]
    assert entry_event.data["feed"]["id"] == row.id
    assert entry_event.data["feed"]["summary"] == row.summary
    assert alert_event.kind == feed.LEVEL_LOW


def test_no_feed_line_without_a_commit(db, nora, resources, heard):
    water, _ = resources
    ledger.add_entry(db, nora, water, "use", 50, channel="matrix")
    db.rollback()
    assert rows(db) == []
    assert heard == []


def test_edit_correction_and_undo_lines(db, nora, ada, resources):
    water, _ = resources
    add(db, nora, water, "reading", 2000, minutes_ago=60)
    e = add(db, nora, water, "use", 300, minutes_ago=2)
    ledger.fix_entry(db, nora, e, 200, channel="matrix")
    db.commit()
    edited = rows(db)[-1]
    assert edited.summary == f"Nora fixed #{e.id}: Water used 300 l → 200 l"
    assert edited.data["line"] == f"Nora: Water used 200 l (#{e.id}) (edited)"

    ledger.fix_entry(db, ada, e, 150, channel="matrix")
    db.commit()
    corrected = rows(db)[-1]
    assert corrected.member_id == ada.id
    assert corrected.summary == f"Ada corrected #{e.id} (Nora: Water used): 200 l → 150 l"
    assert corrected.data["line"].endswith("(corrected to 150 l)")
    assert corrected.data["entry_id"] == e.id

    ledger.fix_entry(db, ada, e, None, channel="matrix")
    db.commit()
    undone = rows(db)[-1]
    assert undone.summary == f"Ada undid #{e.id} (Nora: Water used 200 l)"
    assert undone.data["line"].endswith("(undone, no longer counts)")
    assert undone.data["topic"] == "COMMONS levels: Water 2,000 l · Firewood no data"

    ledger.fix_entry(db, ada, e, 100, channel="matrix")
    db.commit()
    assert rows(db)[-1].summary.endswith("undone → 100 l")


def test_topic_for_flow_resources(db, nora):
    gas = categories.create(db, Spec(slug="gas", name="Gas", unit="kg", kind="flow"))
    db.commit()
    add(db, nora, gas, "use", 3)
    assert rows(db)[-1].data["topic"] == "COMMONS levels: Gas 3 kg used in 7 days"


def test_since_and_latest_id(db, nora, resources):
    water, _ = resources
    assert feed.latest_id(db) == 0
    add(db, nora, water, "reading", 1800)
    add(db, nora, water, "use", 20)
    first, second = rows(db)
    assert feed.latest_id(db) == second.id
    assert [i["id"] for i in feed.since(db, first.id)] == [second.id]
    assert feed.since(db, 0)[0]["summary"] == first.summary


# -- choosing the community room -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ok"),
    [("COMMONS, use this room for updates", True), ("commons: use this room for updates.", True),
     ("use this room for the COMMONS updates please", True),
     ("@commons:example.test make this the community room", True),
     ("post updates here", True), ("use this room for something else", False),
     ("what room is used for updates?", False)],
)  # fmt: skip
def test_asks_to_set_room(text, ok):
    assert community_room.asks_to_set_room(text) is ok


def test_an_admin_chooses_the_room_and_it_is_logged(client, db, ada, heard, fake_llm):
    reply = say(client, "COMMONS, use this room for updates", sender=ADA)
    assert reply == en.ROOM_SET
    assert fake_llm.calls == []  # rules only, never the model
    db.expire_all()
    row = db.get(ConfigKV, stream.ROOM_KEY)
    assert (row.value_json, row.updated_by) == (ROOM, ada.id)
    entry = db.scalars(select(DecisionLog).order_by(DecisionLog.seq.desc())).first()
    assert (entry.kind, entry.actor_member_id) == ("config.changed", ada.id)
    assert entry.payload_json == {
        "key": stream.ROOM_KEY, "old": None, "new": ROOM, "source": "matrix"
    }  # fmt: skip
    assert [e.kind for e in heard] == [stream.ROOM_SET]
    assert say(client, "COMMONS, use this room for updates", sender=ADA) == en.ROOM_ALREADY


def test_members_cannot_choose_the_room(client, db, nora, ada):
    say(client, "COMMONS, use this room for updates", sender=ADA)
    other = f"!other:{SERVER}"
    assert say(client, "COMMONS, use this room for updates", room=other) == en.ROOM_ADMINS_ONLY
    db.expire_all()
    assert db.get(ConfigKV, stream.ROOM_KEY).value_json == ROOM


# -- the event stream ------------------------------------------------------------


def test_stream_needs_the_service_token(client):
    assert client.get("/internal/events").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/internal/events", headers=bad).status_code == 401


def test_stream_starts_with_hello(client, db, nora, ada, resources):
    water, _ = resources
    say(client, "COMMONS, use this room for updates", sender=ADA)
    add(db, nora, water, "reading", 800)
    r = client.get("/internal/events", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    ((event, data, _),) = messages(r.text)  # no replay without Last-Event-ID
    assert event == "hello"
    assert data == {
        "room_id": ROOM,
        "topic": "COMMONS levels: Water 800 l (low) · Firewood no data",
        "latest_id": rows(db, with_alerts=True)[-1].id,
        "feed_scope": feed.scope(db),
    }
    assert len(data["feed_scope"]) == 16


def test_a_position_from_another_database_replays_nothing(client, db, ada, nora, resources):
    water, _ = resources
    say(client, "COMMONS, use this room for updates", sender=ADA)  # starts the decision log
    add(db, nora, water, "reading", 1800)
    add(db, nora, water, "use", 20)
    headers = {**AUTH, "Last-Event-ID": "0"}
    other = client.get("/internal/events", headers={**headers, "X-Commons-Feed-Scope": "other"})
    assert [e for e, _, _ in messages(other.text)] == ["hello"]
    same = client.get(
        "/internal/events", headers={**headers, "X-Commons-Feed-Scope": feed.scope(db)}
    )
    assert [e for e, _, _ in messages(same.text)] == ["hello", "feed", "feed"]


def test_stream_replays_after_last_event_id(client, db, nora, resources):
    water, _ = resources
    add(db, nora, water, "reading", 1800)
    add(db, nora, water, "use", 20)
    add(db, nora, water, "use", 30)
    first = rows(db)[0]
    r = client.get("/internal/events", headers={**AUTH, "Last-Event-ID": str(first.id)})
    got = messages(r.text)
    assert [e for e, _, _ in got] == ["hello", "feed", "feed"]
    assert [int(i) for _, _, i in got[1:]] == [first.id + 1, first.id + 2]
    assert got[1][1]["summary"].startswith("Nora: Water used 20 l")
    for junk in ("abc", "-1", "9" * 30):
        r = client.get("/internal/events", headers={**AUTH, "Last-Event-ID": junk})
        assert [e for e, _, _ in messages(r.text)] == ["hello"]


def test_live_events_reach_the_stream_from_any_thread(app, client, db, nora, resources):
    """Sync routes commit on a threadpool thread; the stream runs on the event loop."""
    water, _ = resources
    sm = app.state.sessionmaker

    async def run() -> list[str]:
        hub = stream.EventHub()
        hub.start(asyncio.get_running_loop())
        try:
            chunks = stream.stream(hub, sm, None, max_seconds=5)
            got = [await anext(chunks)]  # hello
            assert hub.listeners == 1

            def write():
                with sm() as s:
                    ledger.add_entry(s, s.merge(nora), s.merge(water), "use", 5, channel="web")
                    s.commit()

            threading.Thread(target=write).start()
            got.append(await asyncio.wait_for(anext(chunks), 5))
            await chunks.aclose()
            assert hub.listeners == 0
            return got
        finally:
            hub.stop()

    hello, live = anyio.run(run)
    assert "event: hello" in hello
    assert live.startswith(f"id: {rows(db)[-1].id}\nevent: feed\n")
    assert "Water used 5 l" in live


def test_room_changes_are_streamed_and_slow_streams_are_cut_off(app):
    async def run():
        hub = stream.EventHub()
        hub.start(asyncio.get_running_loop())
        try:
            q = hub.open()
            hub._on_event(events.Event(stream.ROOM_SET, {"room_id": ROOM}))
            hub._on_event(events.Event("ledger.entry_added", {"entry_id": 1}))  # no feed: skipped
            await asyncio.sleep(0)
            assert q.get_nowait() == ("room", {"room_id": ROOM})
            assert q.empty()
            for i in range(stream.QUEUE_SIZE + 1):
                stream.EventHub._put(q, ("feed", {"id": i}))
            assert q.get_nowait() == stream._OVERFLOW
        finally:
            hub.stop()

    anyio.run(run)


def test_sse_format():
    assert stream.sse("feed", {"a": "ü\nx"}, 7) == 'id: 7\nevent: feed\ndata: {"a": "ü\\nx"}\n\n'


# -- low-level alerts (D7) ---------------------------------------------------------


def alerts(db) -> list[tuple[str, str]]:
    kinds = (feed.LEVEL_LOW, feed.LEVEL_OK)
    return [(r.kind, r.summary) for r in rows(db, with_alerts=True) if r.kind in kinds]


def test_dropping_below_the_threshold_alerts_once(db, nora, resources, heard):
    water, _ = resources
    add(db, nora, water, "reading", 1200)
    assert alerts(db) == []
    add(db, nora, water, "use", 300)  # 900 < 1,000
    assert alerts(db) == [(feed.LEVEL_LOW, "⚠️ Water is running low: 900 l left, below 1,000 l.")]
    low = rows(db, with_alerts=True)[-1]
    assert low.data["category_id"] == water.id
    assert low.data["topic"].startswith("COMMONS levels: Water 900 l (low)")
    assert heard[-1].kind == feed.LEVEL_LOW
    assert heard[-1].data["feed"]["id"] == low.id
    # Staying below: no more alerts.
    add(db, nora, water, "use", 100)
    add(db, nora, water, "reading", 700)
    assert len(alerts(db)) == 1


def test_refilling_resets_and_the_next_drop_alerts_again(db, nora, resources):
    water, _ = resources
    add(db, nora, water, "reading", 900)
    add(db, nora, water, "add", 1100)  # 2,000
    add(db, nora, water, "use", 500)  # still above
    add(db, nora, water, "use", 600)  # 900
    assert [k for k, _ in alerts(db)] == [feed.LEVEL_LOW, feed.LEVEL_OK, feed.LEVEL_LOW]
    assert alerts(db)[1][1] == "Water is back above the low mark: 2,000 l (low below 1,000 l)."


def test_exactly_at_the_threshold_is_not_low(db, nora, resources):
    water, _ = resources
    add(db, nora, water, "reading", 1000)
    assert alerts(db) == []


def test_the_alert_state_survives_a_restart(app, db, nora, resources):
    water, _ = resources
    add(db, nora, water, "reading", 800)
    assert len(alerts(db)) == 1
    # A new session (as after a restart) reads the state from the database.
    with app.state.sessionmaker() as s:
        assert feed.alert_state(s, water.id) == feed.LEVEL_LOW
        ledger.add_entry(s, s.get(type(nora), nora.id), s.get(type(water), water.id), "use", 5,
                         channel="web")  # fmt: skip
        s.commit()
    assert len(alerts(db)) == 1


def test_undo_and_corrections_can_cross_the_threshold(db, nora, ada, resources):
    water, _ = resources
    add(db, nora, water, "reading", 1500, minutes_ago=60)
    e = add(db, nora, water, "use", 800, minutes_ago=30)  # 700: low
    ledger.fix_entry(db, ada, e, None, channel="matrix")  # undone: 1,500
    db.commit()
    ledger.fix_entry(db, ada, e, 600, channel="matrix")  # 900: low again
    db.commit()
    assert [k for k, _ in alerts(db)] == [feed.LEVEL_LOW, feed.LEVEL_OK, feed.LEVEL_LOW]


def test_no_alert_without_a_threshold_or_for_flows(db, nora):
    tank = categories.create(db, Spec(slug="tank", name="Tank", unit="l", kind="stock"))
    gas = categories.create(
        db, Spec(slug="gas", name="Gas", unit="kg", kind="flow", shortage_threshold=5)
    )
    db.commit()
    add(db, nora, tank, "reading", 1)
    add(db, nora, gas, "use", 100)
    assert alerts(db) == []


def test_counting_from_zero_alerts_too(db, nora, resources):
    _, wood = resources
    add(db, nora, wood, "add", 1)  # 1 m³ from zero, below 2
    assert alerts(db) == [(feed.LEVEL_LOW, "⚠️ Firewood is running low: 1 m³ left, below 2 m³.")]


def test_no_alert_without_a_commit(db, nora, resources):
    water, _ = resources
    ledger.add_entry(db, nora, water, "reading", 10, channel="matrix")
    db.rollback()
    assert alerts(db) == []
