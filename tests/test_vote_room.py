"""Votes in the COMMONS room and taps in direct chats.

The room gets the announcement and the result (as a reply); ballots are taken
only by tapping on the vote message in a member's own direct chat.
"""

from __future__ import annotations

import anyio
import pytest
from sqlalchemy import func, select
from tests.conftest import INTERNAL_TOKEN, SERVER, VOTING

from app import community_room, governance, members
from app.assistant import SlidingWindowLimit
from app.models import (
    ChatNotification,
    ConfigKV,
    DecisionLog,
    EventFeed,
    ProposalMessage,
    Vote,
    utcnow,
)
from bot.feed import RoomFeed
from bot.notify import Notifier

AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
ANNA, BEN, CARA = (f"@{n}:{SERVER}" for n in ("anna", "ben", "cara"))
BOT = f"@commons:{SERVER}"
ONE, TWO, SHRUG = "1️⃣", "2️⃣", "\U0001f937"


@pytest.fixture
def people(db, log_signer):
    db.add(ConfigKV(key="voting", value_json=VOTING))
    found = {
        n: members.add_member(db, f"@{n}:{SERVER}", n.title(), (), signer=log_signer)
        for n in ("anna", "ben", "cara")
    }
    db.commit()
    return found


def open_vote(db, signer, member, **kw):
    p = governance.open_proposal(
        db, member, "Buy a new pump?", ("Yes", "No"), vote_type_slug="rules",
        signer=signer, channel="web", **kw,
    )  # fmt: skip
    db.commit()
    return p


def feed(db, kind):
    db.expire_all()
    return db.scalars(select(EventFeed).where(EventFeed.kind == kind)).all()


def remember(client, pid, kind="room", event="$room", room=None, user=None):
    room = room or ("!dm:x" if kind == "direct" else "!room:x")
    body = {"proposal_id": pid, "kind": kind, "room_id": room, "event_id": event}
    if user:
        body["user_id"] = user
    return client.post("/internal/vote-messages", json=body, headers=AUTH)


def tap(client, sender, event, key, room="!dm:x"):
    body = {"sender": sender, "room_id": room, "event_id": event, "key": key}
    r = client.post("/internal/votes", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def ballots(db):
    db.expire_all()
    return db.scalars(select(Vote).where(Vote.superseded_at.is_(None))).all()


# --- the room announcement ----------------------------------------------------------


def test_opening_announces_the_vote_in_the_room(db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    (row,) = feed(db, "proposal.opened")
    text = row.data["text"]
    assert f"@room New vote #{p.id}" in text
    assert "Buy a new pump?" in text
    assert "Options: Yes · No" in text
    assert ONE not in text  # nothing to react to: ballots aren't taken in the room
    assert "votes aren't taken here" in text
    assert "The decision will be posted here" in text
    assert "Closes " in text
    assert "Ballots are secret" in text
    assert "<strong>Buy a new pump?</strong>" in row.data["html"]
    assert row.data["proposal_id"] == p.id


def test_anonymous_proposer_stays_anonymous_in_the_room(db, log_signer, people):
    open_vote(db, log_signer, people["anna"], anonymous=True)
    (row,) = feed(db, "proposal.opened")
    assert "Anna" not in row.data["text"]


def test_the_announcement_is_remembered(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    assert remember(client, p.id).status_code == 200
    assert remember(client, p.id).status_code == 200  # a replay changes nothing
    (m,) = db.scalars(select(ProposalMessage)).all()
    assert (m.kind, m.room_id, m.event_id, m.member_id) == ("room", "!room:x", "$room", None)


@pytest.mark.parametrize(
    ("kind", "user", "status"),
    [("direct", None, 400), ("room", BEN, 400), ("direct", "@nobody:" + SERVER, 404)],
)
def test_vote_messages_are_checked(client, db, log_signer, people, kind, user, status):
    p = open_vote(db, log_signer, people["anna"])
    assert remember(client, p.id, kind=kind, user=user).status_code == status


def test_unknown_vote_is_refused(client, db, people):
    assert remember(client, 999).status_code == 404


def test_the_result_replies_to_the_announcement(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id)
    for who in ("anna", "ben", "cara"):
        governance.cast_vote(db, people[who], p.id, p.options[0].id, signer=log_signer,
                             channel="web")  # fmt: skip
        db.commit()
    (row,) = feed(db, "proposal.closed")
    assert (row.data["reply_room"], row.data["reply_to"]) == ("!room:x", "$room")
    assert "passed with “Yes”" in row.data["text"]
    assert "Yes: 3" in row.data["text"]


def test_a_cancelled_vote_is_announced_with_its_reason(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id)
    members.grant_role(db, people["cara"], "board", None, signer=log_signer, channel="web")
    governance.cancel_proposal(db, people["cara"], p.id, "Duplicate", signer=log_signer,
                               channel="web")  # fmt: skip
    db.commit()
    (row,) = feed(db, "proposal.cancelled")
    assert row.data["reply_to"] == "$room"
    assert f"Vote #{p.id} cancelled" in row.data["text"]
    assert "Reason: Duplicate" in row.data["text"]


# --- taps in direct chats -----------------------------------------------------------


def test_direct_messages_carry_the_buttons(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    (item,) = client.get("/internal/notifications", headers=AUTH).json()["items"]
    assert item["proposal_id"] == p.id
    assert item["keys"] == [ONE, TWO, SHRUG]
    assert f"tap {ONE} to {TWO} below" in item["text"]
    assert "reply to this message with the number" in item["text"]
    assert f"{ONE} Yes" in item["text"]


def test_a_tap_in_your_own_direct_chat_is_your_ballot(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert "Recorded your vote: Yes on “Buy a new pump?”. You can change it until" in tap(
        client, BEN, "$ben", ONE
    )
    assert "No" in tap(client, BEN, "$ben", "2⃣")  # without the variation selector
    assert "Abstain" in tap(client, BEN, "$ben", SHRUG)
    (ballot,) = ballots(db)
    assert (ballot.member_id, ballot.option_id, ballot.channel) == (people["ben"].id, None,
                                                                   "matrix")  # fmt: skip


@pytest.mark.parametrize(
    ("sender", "event", "key"),
    [
        (CARA, "$ben", ONE),  # someone else's direct message
        (BEN, "$room", ONE),  # the room announcement: public, never a ballot
        (BEN, "$ben", "👍"),  # not an option
        (BEN, "$ben", "6️⃣"),  # this vote has two options
        (BEN, "$unknown", ONE),
    ],
)
def test_other_taps_are_not_ballots(client, db, log_signer, people, sender, event, key):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id)
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert tap(client, sender, event, key) is None
    assert ballots(db) == []


def test_no_tap_after_the_vote_ended(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    members.grant_role(db, people["cara"], "board", None, signer=log_signer, channel="web")
    governance.cancel_proposal(db, people["cara"], p.id, "Off", signer=log_signer, channel="web")
    db.commit()
    assert "couldn't record" in tap(client, BEN, "$ben", ONE)
    assert ballots(db) == []


def test_non_members_and_deactivated_members_get_a_polite_refusal(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert "Only active COMMONS members can vote" in tap(client, "@eve:other.example", "$ben", ONE)
    people["ben"].deactivated_at = utcnow()
    db.commit()
    assert "Only active COMMONS members can vote" in tap(client, BEN, "$ben", ONE)
    assert ballots(db) == []


def test_taps_after_the_close_are_refused(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    governance.close_if_due(db, p.id, signer=log_signer, now=p.closes_at)
    db.commit()
    assert "votes are no longer taken" in tap(client, BEN, "$ben", ONE)
    assert ballots(db) == []


def test_a_changed_tap_supersedes_and_is_logged_without_the_choice(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    tap(client, BEN, "$ben", ONE)
    tap(client, BEN, "$ben", TWO)
    db.expire_all()
    rows = db.scalars(select(Vote).order_by(Vote.id)).all()
    assert [r.superseded_at is None for r in rows] == [False, True]
    assert rows[-1].option_id == p.options[1].id
    kinds = db.scalars(
        select(DecisionLog.kind).where(DecisionLog.kind.in_(("vote.cast", "vote.changed")))
    ).all()
    assert kinds == ["vote.cast", "vote.changed"]
    entry = db.scalar(select(DecisionLog).where(DecisionLog.kind == "vote.changed"))
    assert entry.payload_json["channel"] == "matrix"
    assert "No" not in str(entry.payload_json)


def test_taps_are_rate_limited_per_sender(app, client, db, log_signer, people):
    app.state.tap_limit = SlidingWindowLimit(2)
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    tap(client, BEN, "$ben", ONE)
    tap(client, BEN, "$ben", TWO)
    assert "a lot of taps" in tap(client, BEN, "$ben", ONE)


# --- replies to the vote message in a direct chat ---------------------------------


def reply(client, text, reply_to="$ben", sender=BEN, direct=True, history=None):
    body = {
        "sender": sender,
        "room_id": "!dm:x",
        "text": text,
        "direct": direct,
        "history": history or [],
    }
    if reply_to:
        body["reply_to"] = reply_to
    r = client.post("/internal/assistant", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["reply"]


@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("1", 1),
        ("2", 2),
        ("No", 2),
        ("vote 1", 1),
        ("option 2", 2),
        ("> <@commons:x> New vote #1\n> Buy a new pump?\n\n1", 1),
        ("abstain", None),
    ],
)
def test_replying_to_the_vote_message_is_a_ballot(
    client, db, fake_llm, log_signer, people, text, position
):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert "Recorded your vote:" in reply(client, text)
    (ballot,) = ballots(db)
    expected = None if position is None else p.options[position - 1].id
    assert (ballot.option_id, ballot.channel) == (expected, "matrix")
    assert fake_llm.calls == []  # a ballot never reaches the model


def test_a_reply_can_change_the_vote_until_the_close(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    reply(client, "1")
    assert "Recorded your vote: No" in reply(client, "2")
    (ballot,) = ballots(db)
    assert ballot.option_id == p.options[1].id
    governance.close_if_due(db, p.id, signer=log_signer, now=p.closes_at)
    db.commit()
    assert "votes are no longer taken" in reply(client, "1")


def test_an_unclear_reply_records_nothing(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert "couldn't tell which option" in reply(client, "maybe later")
    assert ballots(db) == []


@pytest.mark.parametrize(
    ("sender", "reply_to", "direct"),
    [(CARA, "$ben", True), (BEN, "$room", True), (BEN, "$ben", False)],
)
def test_other_replies_are_not_ballots(client, db, log_signer, people, sender, reply_to, direct):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id)
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    reply(client, "1", reply_to=reply_to, sender=sender, direct=direct)
    assert ballots(db) == []


def test_a_reply_ballot_stays_out_of_the_models_history(client, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    history = [
        {"role": "user", "text": "2"},
        {"role": "assistant", "text": "Recorded your vote: No on “Buy a new pump?”."},
        {"role": "user", "text": "thanks"},
    ]
    reply(client, "how are you?", reply_to=None, history=history)
    prompt = str(fake_llm.calls[-1])
    assert "thanks" in prompt
    assert "Recorded your vote" not in prompt
    assert "'2'" not in prompt


# --- the COMMONS room is never a private chat -------------------------------------


@pytest.fixture
def community(db):
    db.add(ConfigKV(key=community_room.ROOM_KEY, value_json="!room:x"))
    db.commit()


def test_no_private_vote_message_in_the_community_room(client, db, log_signer, people, community):
    p = open_vote(db, log_signer, people["anna"])
    r = remember(client, p.id, kind="direct", event="$ben", user=BEN, room="!room:x")
    assert r.status_code == 400


def test_no_ballot_from_the_community_room_however_few_are_in_it(
    client, db, log_signer, people, community
):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert tap(client, BEN, "$ben", ONE, room="!room:x") is None
    body = {"sender": BEN, "room_id": "!room:x", "text": f"vote {p.id} 1", "direct": True}
    r = client.post("/internal/assistant", json=body, headers=AUTH)
    assert "direct chat" in r.json()["reply"]
    body = {**body, "text": "1", "reply_to": "$ben"}
    client.post("/internal/assistant", json=body, headers=AUTH)
    assert ballots(db) == []


def test_a_tap_counts_only_in_the_room_the_message_was_sent_to(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    remember(client, p.id, kind="direct", event="$ben", user=BEN)
    assert tap(client, BEN, "$ben", ONE, room="!elsewhere:x") is None
    assert ballots(db) == []


def test_the_bot_never_uses_the_community_room_as_a_private_chat():
    rooms = FakeRooms()
    rooms.rooms = {"!room:x": {BOT: "join", BEN: "join"}}
    created = []

    async def create_direct(user_id):
        created.append(user_id)
        rooms.rooms["!new:x"] = {BOT: "join", user_id: "invite"}
        return "!new:x"

    rooms.create_direct = create_direct
    app = FakeApp([{"id": 1, "text": "t", "recipients": [BEN], "proposal_id": 7, "keys": []}])
    anyio.run(Notifier(rooms, app, BOT, community_room=lambda: "!room:x").tick)
    assert rooms.sent == []
    assert created == [BEN]


# --- welcome -------------------------------------------------------------------------


def test_a_new_member_is_welcomed_once_in_a_private_chat(client, db, people, community):
    newbie = members.ensure_member(db, f"@dora:{SERVER}", SERVER)
    db.commit()
    members.ensure_member(db, f"@dora:{SERVER}", SERVER)  # already there: no second welcome
    db.commit()
    items = client.get("/internal/notifications", headers=AUTH).json()["items"]
    (item,) = [i for i in items if i["recipients"] == [newbie.matrix_user_id]]
    assert "Welcome to COMMONS, dora!" in item["text"]
    assert "Votes come to you here" in item["text"]
    assert item["invite_to"] == "!room:x"
    assert item["max_tries"] > 1000
    assert item["keys"] == []


def test_members_added_by_admins_are_not_welcomed_twice(db, people):
    members.queue_welcome(db, people["ben"])
    members.queue_welcome(db, people["ben"])
    db.commit()
    count = db.scalar(
        select(func.count()).select_from(ChatNotification).where(ChatNotification.kind == "welcome")
    )
    assert count == 1


def test_the_bot_invites_to_the_community_room_and_waits_longer():
    rooms = FakeRooms()
    invited = []

    async def invite(room_id, user_id):
        invited.append((room_id, user_id))

    rooms.invite = invite
    rooms.rooms["!dm"][BEN] = "invite"  # not accepted yet
    item = {"id": 1, "text": "hi", "recipients": [BEN], "keys": [], "invite_to": "!room:x",
            "max_tries": 500}  # fmt: skip
    app = FakeApp([item])
    notifier = Notifier(rooms, app, BOT)
    for _ in range(300):
        anyio.run(notifier.tick)
    assert invited == [("!room:x", BEN)]  # once
    assert app.done == []  # still waiting for the private chat to be accepted


# --- the bot: room feed ---------------------------------------------------------------


class FakeRoom:
    def __init__(self):
        self.posts: list[tuple] = []

    async def send_notice(self, room_id, text):
        self.posts.append(("notice", room_id, text))
        return "$n"

    async def send_alert(self, room_id, text):
        self.posts.append(("alert", room_id, text))
        return "$a"

    async def edit_notice(self, room_id, event_id, text):
        pass

    async def set_topic(self, room_id, text):
        return True

    async def send_announcement(self, room_id, text, html):
        self.posts.append(("announce", room_id, text, html))
        return "$ann"

    async def send_reply(self, room_id, reply_to, text, html):
        self.posts.append(("reply", room_id, reply_to, text))
        return "$rep"


def run_feed(items, room_id="!room:x"):
    room = FakeRoom()
    told: list[tuple] = []

    async def announced(pid, room, event):
        told.append((pid, room, event))

    async def go():
        f = RoomFeed(room, on_announced=announced)
        if room_id:
            await f.set_room(room_id)
        for item in items:
            await f.on_feed(item)
        await f.close()

    anyio.run(go)
    return room.posts, told


def test_bot_announces_and_reports_the_event():
    item = {"id": 1, "kind": "proposal.opened", "text": "@room vote", "html": "<p>v</p>"}
    posts, told = run_feed([{**item, "proposal_id": 7}])
    assert posts == [("announce", "!room:x", "@room vote", "<p>v</p>")]
    assert told == [(7, "!room:x", "$ann")]


def test_bot_replies_with_the_result_in_the_same_room():
    item = {"id": 2, "kind": "proposal.closed", "text": "passed", "reply_room": "!room:x",
            "reply_to": "$ann"}  # fmt: skip
    posts, _ = run_feed([item])
    assert posts == [("reply", "!room:x", "$ann", "passed")]
    posts, _ = run_feed([{**item, "reply_room": "!old:x"}])  # room changed since
    assert posts == [("reply", "!room:x", None, "passed")]


def test_bot_posts_nothing_without_a_community_room():
    posts, told = run_feed([{"id": 1, "kind": "proposal.opened", "text": "v", "proposal_id": 7}],
                           room_id=None)  # fmt: skip
    assert posts == []
    assert told == []


# --- the bot: direct messages with buttons ------------------------------------------


class FakeRooms:
    def __init__(self):
        self.rooms = {"!dm": {BOT: "join", BEN: "join"}}
        self.sent, self.reactions = [], []

    async def joined_rooms(self):
        return list(self.rooms)

    async def members(self, room_id):
        return self.rooms[room_id]

    async def create_direct(self, user_id):
        raise AssertionError

    async def can_encrypt_for(self, room_id, user_id):
        return True

    async def send(self, room_id, text, html=None):
        self.sent.append(room_id)
        return "$msg"

    async def react(self, room_id, event_id, key):
        self.reactions.append((room_id, event_id, key))


class FakeApp:
    def __init__(self, items):
        self.items, self.done, self.mapped = items, [], []

    async def notifications(self):
        return [i for i in self.items if i["id"] not in self.done]

    async def delivered(self, nid):
        self.done.append(nid)

    async def vote_message(self, pid, kind, room, event, user):
        self.mapped.append((pid, kind, room, event, user))


def test_bot_adds_buttons_and_reports_the_message():
    rooms = FakeRooms()
    app = FakeApp([{"id": 1, "text": "t", "recipients": [BEN], "proposal_id": 7,
                    "keys": [ONE, TWO, SHRUG]}])  # fmt: skip
    anyio.run(Notifier(rooms, app, BOT).tick)
    assert app.mapped == [(7, "direct", "!dm", "$msg", BEN)]
    assert rooms.reactions == [("!dm", "$msg", k) for k in (ONE, TWO, SHRUG)]
    assert app.done == [1]


def test_results_get_no_buttons():
    rooms = FakeRooms()
    app = FakeApp([{"id": 1, "text": "t", "recipients": [BEN], "proposal_id": 7, "keys": []}])
    anyio.run(Notifier(rooms, app, BOT).tick)
    assert app.mapped == []
    assert rooms.reactions == []
