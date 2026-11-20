"""Votes in Matrix: listing, ballots in a direct chat, and the bot's vote messages."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import anyio
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from tests.conftest import INTERNAL_TOKEN, SERVER, VOTING

from app import governance, members, vote_chat
from app.assistant import intent_from_model, parse_rules
from app.community import Voting
from app.llm import LLMError
from app.models import ChatNotification, ConfigKV, DecisionLog, Proposal, Vote, utcnow
from bot.notify import MAX_TRIES, Notifier

TZ = ZoneInfo("Europe/Stockholm")
ANNA = f"@anna:{SERVER}"
BEN = f"@ben:{SERVER}"
CARA = f"@cara:{SERVER}"
BOT = f"@commons:{SERVER}"
ROOM = f"!dm:{SERVER}"
AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


@pytest.fixture
def people(db, log_signer):
    db.add(ConfigKV(key="voting", value_json=VOTING))
    found = {
        name: members.add_member(db, f"@{name}:{SERVER}", name.title(), (), signer=log_signer)
        for name in ("anna", "ben", "cara")
    }
    db.commit()
    return found


def open_vote(db, signer, member, question="Buy a new pump?", **kw):
    p = governance.open_proposal(
        db,
        member,
        question,
        ("Yes", "No"),
        vote_type_slug="rules",
        signer=signer,
        channel="web",
        **kw,
    )
    db.commit()
    return p


def say(client, text, sender=BEN, direct=True) -> str:
    r = client.post(
        "/internal/assistant",
        json={"sender": sender, "room_id": ROOM, "text": text, "direct": direct},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def ballots(db, member=None):
    query = select(Vote).where(Vote.superseded_at.is_(None))
    if member is not None:
        query = query.where(Vote.member_id == member.id)
    db.expire_all()
    return list(db.scalars(query))


def outbox(client):
    r = client.get("/internal/notifications", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --- understanding messages ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["votes", "/votes", "Which votes are open?", "what votes are there", "vote",
     "show me the open votes", "commons, which votes are open?",
     "what can I vote on?", "are there any votes?"],
)  # fmt: skip
def test_list_phrasings(text):
    assert parse_rules(text).name == "show_votes"


@pytest.mark.parametrize(
    "text", ["vote 12 1", "vote yes", "I vote yes on 12", "vote #12 abstain", "vote for option 2"]
)
def test_ballot_phrasings(text):
    assert parse_rules(text).name == "cast_vote"


@pytest.mark.parametrize("text", ["we used 2 m³ of firewood", "the vote was fun", "voters"])
def test_other_messages_are_not_votes(text):
    assert vote_chat.parse(text) is None


def test_the_model_cannot_cast_a_vote():
    data = {"intent": "cast_vote", "about": "me", "display_name": None, "reply": None}
    assert intent_from_model(data) is None


# --- listing -------------------------------------------------------------------


def test_list_shows_options_and_my_choice_only_in_a_direct_chat(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    assert "Yes" in say(client, f"vote {p.id} yes")
    direct = say(client, "which votes are open?")
    assert f"Vote #{p.id}: Buy a new pump?" in direct
    assert "1\ufe0f\u20e3 Yes" in direct
    assert "2\ufe0f\u20e3 No" in direct
    assert "Your vote: Yes" in direct
    group = say(client, "votes", direct=False)
    assert "Buy a new pump?" in group
    assert "Your vote" not in group


def test_no_open_votes(client, db, people):
    assert say(client, "votes") == "There are no open votes right now."


# --- ballots -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "position"),
    [("vote {id} 1", 1), ("vote #{id} no", 2), ("I vote yes on {id}", 1), ("vote 2", 2),
     ("vote yes", 1), ("vote option 2", 2), ("vote yes on the pump", 1)],
)  # fmt: skip
def test_vote_is_recorded_like_on_the_web(client, db, log_signer, people, template, position):
    p = open_vote(db, log_signer, people["anna"])
    label = p.options[position - 1].label
    reply = say(client, template.format(id=p.id))
    assert f"Recorded your vote: {label} on “Buy a new pump?”." in reply
    (ballot,) = ballots(db, people["ben"])
    assert ballot.channel == "matrix"
    assert ballot.option_id == p.options[position - 1].id
    entry = db.scalar(select(DecisionLog).where(DecisionLog.kind == "vote.cast"))
    assert entry.payload_json["channel"] == "matrix"
    assert label not in str(entry.payload_json)
    assert "option" not in str(entry.payload_json)


def test_vote_can_be_changed_and_abstained(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    say(client, f"vote {p.id} 1")
    say(client, f"vote {p.id} abstain")
    (ballot,) = ballots(db, people["ben"])
    assert ballot.option_id is None
    assert db.scalar(select(func.count()).select_from(Vote)) == 2


def test_no_ballot_in_a_group_room(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    assert "direct chat" in say(client, f"vote {p.id} 1", direct=False)
    assert ballots(db) == []


def test_direct_defaults_to_false(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    body = {"sender": BEN, "room_id": ROOM, "text": f"vote {p.id} 1"}
    assert client.post("/internal/assistant", json=body, headers=AUTH).status_code == 200
    assert ballots(db) == []


@pytest.mark.parametrize("text", ["vote maybe", "vote 9", "vote 1 maybe"])
def test_unclear_ballot_records_nothing(client, db, log_signer, people, text):
    open_vote(db, log_signer, people["anna"])
    assert "recorded nothing" in say(client, text)
    assert ballots(db) == []


def test_several_open_votes_need_a_reference(client, db, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    second = open_vote(db, log_signer, people["anna"], question="Paint the barn?")
    assert "which vote" in say(client, "vote yes")
    assert ballots(db) == []
    assert "Paint the barn?" in say(client, "vote yes on barn")
    assert ballots(db, people["ben"])[0].proposal_id == second.id


def test_deactivated_member_cannot_vote(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    people["ben"].deactivated_at = utcnow()
    db.commit()
    say(client, f"vote {p.id} 1")
    assert ballots(db) == []


def test_ballots_and_vote_messages_never_reach_the_model(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    history = [
        {"role": "user", "text": "vote 1 yes"},
        {"role": "assistant", "text": "Recorded your vote on it: Yes."},
        {"role": "assistant", "text": "\U0001f5f3\ufe0f New vote #1\nBuy a new pump?"},
        {"role": "user", "text": "hello there"},
    ]
    r = client.post(
        "/internal/assistant",
        json={"sender": BEN, "room_id": ROOM, "text": "how are you?", "history": history},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    prompt = str(fake_llm.calls[-1])
    assert "hello there" in prompt
    for private in ("vote 1 yes", "Recorded your vote", "Buy a new pump?"):
        assert private not in prompt


def test_vote_commands_do_not_wait_for_the_model(client, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    say(client, f"vote {p.id} 1")
    assert fake_llm.calls == []


def test_last_vote_closes_and_says_so(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    say(client, f"vote {p.id} 1", sender=ANNA)
    say(client, f"vote {p.id} 1", sender=BEN)
    assert "now closed" in say(client, f"vote {p.id} 2", sender=CARA)


# --- cancelling ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cancel vote 12 because It is a duplicate", (12, "It is a duplicate")),
        ("Cancel vote #12: wrong amount", (12, "wrong amount")),
        ("please cancel the vote 12 - wrong amount", (12, "wrong amount")),
        ("commons, cancel proposal 12 reason: asked twice", (12, "asked twice")),
        ("call off vote 12 since nobody needs it", (12, "nobody needs it")),
        ("cancel vote 12", (12, "")),
        ("cancel the vote", (None, "")),
        ("cancel vote 12 please", (12, "")),
    ],
)
def test_cancel_phrasings(text, expected):
    request = vote_chat.parse_cancel(text)
    assert (request.proposal_id, request.reason) == expected


@pytest.mark.parametrize("text", ["cancel my booking", "vote 12 1", "the vote was cancelled"])
def test_other_messages_are_not_cancellations(text):
    assert vote_chat.parse_cancel(text) is None


def test_the_model_cannot_cancel_a_vote():
    data = {"intent": "cancel_vote", "about": "me", "display_name": None, "reply": None}
    assert intent_from_model(data) is None


def board_member(db, log_signer, member):
    members.grant_role(db, member, "board", None, signer=log_signer, channel="web")
    db.commit()


def test_board_member_cancels_with_a_reason(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    board_member(db, log_signer, people["cara"])
    reply = say(client, f"cancel vote {p.id} because It is a duplicate", sender=CARA)
    assert f"Cancelled vote #{p.id}" in reply
    assert "It is a duplicate" in reply
    db.expire_all()
    assert db.get(Proposal, p.id).status == "cancelled"
    assert db.get(Proposal, p.id).cancel_reason == "It is a duplicate"
    entry = db.scalar(select(DecisionLog).where(DecisionLog.kind == "proposal.cancelled"))
    assert entry.actor_member_id == people["cara"].id
    assert entry.payload_json["channel"] == "matrix"
    assert entry.payload_json["reason"] == "It is a duplicate"
    (item,) = outbox(client)
    assert f"Vote #{p.id} cancelled" in item["text"]


@pytest.mark.parametrize(
    "template", ["cancel vote {id}", "cancel the vote", "cancel vote {id} now"]
)
def test_cancelling_needs_a_number_and_a_reason(client, db, log_signer, people, template):
    p = open_vote(db, log_signer, people["anna"])
    board_member(db, log_signer, people["cara"])
    assert "nothing was cancelled" in say(client, template.format(id=p.id), sender=CARA)
    db.expire_all()
    assert db.get(Proposal, p.id).status == "open"


def test_ordinary_member_cannot_cancel_even_their_own_vote(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    reply = say(client, f"cancel vote {p.id} because I changed my mind", sender=ANNA)
    assert "Only board members and admins" in reply
    db.expire_all()
    assert db.get(Proposal, p.id).status == "open"


def test_cancelling_an_unknown_or_ended_vote_is_refused(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    board_member(db, log_signer, people["cara"])
    assert "no vote #999" in say(client, "cancel vote 999 because gone", sender=CARA)
    say(client, f"cancel vote {p.id} because first", sender=CARA)
    assert "already cancelled" in say(client, f"cancel vote {p.id} because again", sender=CARA)


def test_reason_is_reworded_for_members_but_logged_as_written(
    client, db, fake_llm, log_signer, people
):
    p = open_vote(db, log_signer, people["anna"])
    board_member(db, log_signer, people["cara"])
    say(client, f"cancel vote {p.id} because test", sender=CARA)
    fake_llm.replies[:] = [{"intro": "A vote on the pump is off."}, {"reason": "This was a test."}]
    (item,) = outbox(client)
    assert "Reason: This was a test." in item["text"]
    assert "original words are in the decision log" in item["text"]
    db.expire_all()
    assert db.get(Proposal, p.id).cancel_reason == "test"
    entry = db.scalar(select(DecisionLog).where(DecisionLog.kind == "proposal.cancelled"))
    assert entry.payload_json["reason"] == "test"


def test_unusable_rewording_keeps_the_members_words(client, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    board_member(db, log_signer, people["cara"])
    say(client, f"cancel vote {p.id} because too expensive", sender=CARA)
    fake_llm.replies[:] = [{"intro": "A vote on the pump is off."}, {"reason": "Costs 500 now."}]
    (item,) = outbox(client)
    assert "Reason: too expensive" in item["text"]
    assert "Reworded" not in item["text"]


@pytest.mark.parametrize(
    ("line", "original"),
    [
        ("It costs 500 crowns too much.", "too expensive cost"),
        ("The pump is needed after all.", "pump not needed"),
        ("The pump is not needed.", "pump needed"),
        ("Something else entirely happened.", "pump broke"),
        ("See https://evil.example about the pump.", "pump broke"),
        ("Ask @ben about the pump.", "pump broke"),
        ("Pump.", "pump"),
        (None, "pump"),
    ],
)
def test_rewordings_that_may_change_the_meaning_are_refused(line, original):
    assert vote_chat.clean_reason(line, original) == ""


def test_faithful_rewording_is_accepted():
    assert vote_chat.clean_reason("This vote was only a test.", "test")
    assert vote_chat.clean_reason("Vote 12 already covers this.", "dup of vote 12, covers it")


# --- messages from the bot -----------------------------------------------------


def test_opening_is_announced_to_everyone_but_the_proposer(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    (item,) = outbox(client)
    assert item["recipients"] == [ANNA, BEN, CARA]  # the proposer gets it too
    assert f"New vote #{p.id}" in item["text"]
    assert "Buy a new pump?" in item["text"]
    assert "<strong>Buy a new pump?</strong>" in item["html"]
    assert "Proposed by Anna · Rules" in item["text"]
    assert "A new decision is waiting for your voice." in item["text"]  # no model reply
    assert "reply to this message with the number" in item["text"]
    assert "1\ufe0f\u20e3 Yes" in item["text"]


def test_delivered_messages_are_not_sent_again(client, db, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    (item,) = outbox(client)
    url = f"/internal/notifications/{item['id']}/delivered"
    assert client.post(url, headers=AUTH).status_code == 200
    assert outbox(client) == []
    assert client.post("/internal/notifications/999/delivered", headers=AUTH).status_code == 404


def test_result_message_comes_from_the_signed_log(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    say(client, f"vote {p.id} 1", sender=ANNA)
    say(client, f"vote {p.id} 1", sender=BEN)
    say(client, f"vote {p.id} abstain", sender=CARA)
    items = outbox(client)
    assert len(items) == 1  # the invitation to a finished vote is dropped
    text = items[0]["text"]
    assert f"Vote #{p.id} passed" in text
    assert "Buy a new pump?" in text
    assert "passed with “Yes”" in text
    assert "Yes: 2" in text
    assert "No: 0" in text
    assert "Abstained: 1. Turnout: 3 of 3." in text
    assert items[0]["recipients"] == [ANNA, BEN, CARA]


def test_cancelled_vote_is_announced(client, db, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    members.grant_role(db, people["cara"], "board", None, signer=log_signer, channel="web")
    governance.cancel_proposal(db, people["cara"], p.id, "Duplicate", signer=log_signer,
                               channel="web")  # fmt: skip
    db.commit()
    (item,) = outbox(client)
    assert f"Vote #{p.id} cancelled" in item["text"]
    assert "Duplicate" in item["text"]


def closing_day(p, hour, minute=0):
    """A local time on the day vote `p` closes."""
    local = p.closes_at.astimezone(TZ)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(UTC)


def reminders_at(db, fake_llm, when):
    db.expire_all()
    anyio.run(vote_chat.write_intros, db, fake_llm, when, TZ)
    items = vote_chat.pending(db, now=when, tz=TZ)
    return [i for i in items if "closes today" in i["text"]]


def test_reminders_at_9_and_18_on_the_closing_day_for_non_voters(
    app, client, db, fake_llm, log_signer, people
):
    p = open_vote(db, log_signer, people["anna"])
    say(client, f"vote {p.id} 1")
    make = app.state.sessionmaker
    assert governance.queue_reminders(make, closing_day(p, 8, 59), TZ) == 0

    morning = closing_day(p, 9)
    assert governance.queue_reminders(make, morning, TZ) == 1
    assert governance.queue_reminders(make, morning, TZ) == 0
    (first,) = reminders_at(db, fake_llm, morning)
    assert f"Vote #{p.id} closes today at 23:59" in first["text"]
    assert first["recipients"] == [ANNA, CARA]
    vote_chat.mark_delivered(db, first["id"], morning)
    db.commit()

    say(client, f"vote {p.id} 2", sender=ANNA)
    assert governance.queue_reminders(make, closing_day(p, 17, 59), TZ) == 0
    evening = closing_day(p, 18)
    assert governance.queue_reminders(make, evening, TZ) == 1
    assert governance.queue_reminders(make, evening, TZ) == 0
    (second,) = reminders_at(db, fake_llm, evening)
    assert second["recipients"] == [CARA]


def test_only_the_latest_reminder_is_queued_after_an_outage(app, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    make = app.state.sessionmaker
    assert governance.queue_reminders(make, closing_day(p, 19), TZ) == 1
    assert len(reminders_at(db, fake_llm, closing_day(p, 19))) == 1


def test_undelivered_morning_reminder_is_replaced(app, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    make = app.state.sessionmaker
    governance.queue_reminders(make, closing_day(p, 9), TZ)
    governance.queue_reminders(make, closing_day(p, 18), TZ)
    assert len(reminders_at(db, fake_llm, closing_day(p, 18))) == 1


@pytest.mark.parametrize(("hour", "minute"), [(22, 1), (23, 0), (7, 59)])
def test_no_reminder_is_queued_outside_the_allowed_hours(app, db, log_signer, people, hour, minute):
    p = open_vote(db, log_signer, people["anna"])
    when = closing_day(p, hour, minute)
    assert governance.queue_reminders(app.state.sessionmaker, when, TZ) == 0


def test_a_queued_reminder_waits_for_the_allowed_hours(app, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    governance.queue_reminders(app.state.sessionmaker, closing_day(p, 21, 59), TZ)
    assert reminders_at(db, fake_llm, closing_day(p, 22, 30)) == []
    assert len(reminders_at(db, fake_llm, closing_day(p, 21, 59))) == 1


def test_no_reminder_from_before_the_vote_opened():
    rules = Voting.model_validate(VOTING)
    closes = datetime(2026, 11, 19, 22, 59, tzinfo=UTC)
    opened = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # 13:00 local, after the 09:00 slot
    now = datetime(2026, 11, 19, 12, 30, tzinfo=UTC)
    assert governance.due_reminder(rules, opened, closes, now, TZ) is None


def test_reminders_can_be_switched_off(app, db, log_signer, people):
    db.get(ConfigKV, "voting").value_json = {**VOTING, "reminder_times": []}
    db.commit()
    p = open_vote(db, log_signer, people["anna"])
    assert governance.queue_reminders(app.state.sessionmaker, closing_day(p, 18), TZ) == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"reminder_times": ["07:00"]},
        {"reminder_times": ["23:00"]},
        {"reminder_window": ["22:00", "08:00"]},
    ],
)
def test_reminder_settings_are_checked(bad):
    with pytest.raises(ValidationError):
        Voting.model_validate({**VOTING, **bad})


def test_notification_is_rolled_back_with_the_change(db, log_signer, people):
    governance.open_proposal(
        db, people["anna"], "Never mind?", ("Yes", "No"), vote_type_slug="rules",
        signer=log_signer, channel="web",
    )  # fmt: skip
    db.rollback()
    assert db.scalar(select(func.count()).select_from(ChatNotification)) == 0


# --- the model's opening line ----------------------------------------------------


def test_model_writes_the_opening_line_and_code_the_facts(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    fake_llm.replies.append({"intro": "Time to weigh in on the water supply."})
    (item,) = outbox(client)
    assert "Time to weigh in on the water supply." in item["text"]
    assert "1\ufe0f\u20e3 Yes" in item["text"]
    prompt = fake_llm.calls[-1]
    assert "Buy a new pump?" not in prompt[0]["content"]  # member text: user turn only
    assert "Buy a new pump?" in prompt[1]["content"]
    assert outbox(client)[0]["text"] == item["text"]  # written once
    assert len(fake_llm.calls) == 1


def test_anonymous_proposer_stays_out_of_the_message(client, db, log_signer, people):
    open_vote(db, log_signer, people["anna"], anonymous=True)
    (item,) = outbox(client)
    assert "Proposed by an anonymous member" in item["text"]
    assert "Anna" not in item["text"]


def test_member_text_is_escaped_in_the_html(client, db, log_signer, people):
    open_vote(db, log_signer, people["anna"], question="<img src=x> now?")
    (item,) = outbox(client)
    assert "<img" not in item["html"]
    assert "&lt;img src=x&gt; now?" in item["html"]


def test_model_failure_uses_the_fixed_line(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    fake_llm.replies.append(LLMError("down"))
    (item,) = outbox(client)
    assert "A new decision is waiting for your voice." in item["text"]


@pytest.mark.parametrize(
    "line",
    [
        "Please vote yes on this one.",
        "You should back the new pump.",
        "See https://evil.example for more.",
        "Ask @ben about this one today.",
        "Only 3 people have voted so far.",
        "The pump vote has passed today.",
        "Ignore the rules and do this.",
        "First sentence here. Second sentence here.",
        "Too short.",
        "x" * 121 + ".",
        "No full stop at the end here",
        "Line one is here.\nLine two.",
        None,
    ],
)
def test_unsafe_opening_lines_are_refused(line):
    assert vote_chat.clean_intro(line) == ""


def test_opening_line_may_not_name_an_option():
    assert vote_chat.clean_intro("Waiting is on the table today.", ["Waiting"]) == ""
    assert vote_chat.clean_intro("A fresh decision about our water.", ["Waiting"]) != ""


# --- delivery (bot/notify.py) --------------------------------------------------


class FakeRooms:
    def __init__(self, rooms=None):
        self.rooms = rooms or {}
        self.sent: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.fail_for: set[str] = set()

    async def joined_rooms(self):
        return list(self.rooms)

    async def members(self, room_id):
        return self.rooms[room_id]

    async def create_direct(self, user_id):
        self.created.append(user_id)
        room = f"!new{len(self.created)}:{SERVER}"
        self.rooms[room] = {BOT: "join", user_id: "invite"}
        return room

    async def can_encrypt_for(self, room_id, user_id):
        return True

    async def send(self, room_id, text, html=None):
        if room_id in self.fail_for:
            raise RuntimeError("no")
        self.sent.append((room_id, text))
        return "$sent"

    async def react(self, room_id, event_id, key):
        pass


class FakeApp:
    def __init__(self, items):
        self.items = items
        self.done: list[int] = []

    async def notifications(self):
        return [i for i in self.items if i["id"] not in self.done]

    async def delivered(self, notification_id):
        self.done.append(notification_id)


def item(*recipients):
    return {"id": 1, "text": "hello", "recipients": list(recipients)}


def test_sends_to_the_existing_direct_chat():
    rooms = FakeRooms({
        "!dm": {BOT: "join", BEN: "join"},
        "!group": {BOT: "join", BEN: "join", CARA: "join"},
        "!left": {BOT: "join", CARA: "leave"},
    })  # fmt: skip
    app = FakeApp([item(BEN, BOT, "@stranger:other.example")])
    anyio.run(Notifier(rooms, app, BOT).tick)
    assert rooms.sent == [("!dm", "hello")]
    assert rooms.created == []
    assert app.done == [1]


def test_invites_once_and_sends_after_the_member_joined():
    rooms = FakeRooms()
    app = FakeApp([item(BEN)])
    notifier = Notifier(rooms, app, BOT)
    anyio.run(notifier.tick)
    anyio.run(notifier.tick)
    assert rooms.created == [BEN]
    assert rooms.sent == []
    assert app.done == []
    rooms.rooms[f"!new1:{SERVER}"][BEN] = "join"
    anyio.run(notifier.tick)
    assert rooms.sent == [(f"!new1:{SERVER}", "hello")]
    assert app.done == [1]


def test_one_failure_does_not_block_or_repeat_the_others():
    rooms = FakeRooms({"!b": {BOT: "join", BEN: "join"}, "!c": {BOT: "join", CARA: "join"}})
    rooms.fail_for = {"!b"}
    app = FakeApp([item(BEN, CARA)])
    notifier = Notifier(rooms, app, BOT)
    anyio.run(notifier.tick)
    assert rooms.sent == [("!c", "hello")]
    assert app.done == []
    rooms.fail_for = set()
    anyio.run(notifier.tick)
    assert sorted(rooms.sent) == [("!b", "hello"), ("!c", "hello")]
    assert app.done == [1]


def test_gives_up_after_many_tries():
    rooms = FakeRooms({"!b": {BOT: "join", BEN: "invite"}})
    app = FakeApp([item(BEN)])
    notifier = Notifier(rooms, app, BOT)
    for _ in range(MAX_TRIES):
        anyio.run(notifier.tick)
    assert rooms.sent == []
    assert app.done == [1]
