"""Voting in your own words: the model reads the message, code finds vote and option,
and nothing is recorded until the member confirms."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.conftest import INTERNAL_TOKEN, SERVER, VOTING

from app import governance, members, vote_chat
from app.models import ConfigKV, Vote

AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
BEN = f"@ben:{SERVER}"


@pytest.fixture
def people(db, log_signer):
    db.add(ConfigKV(key="voting", value_json=VOTING))
    found = {
        n: members.add_member(db, f"@{n}:{SERVER}", n.title(), (), signer=log_signer)
        for n in ("anna", "ben", "cara")
    }
    db.commit()
    return found


def open_vote(db, signer, member, question="Buy a new pump?", options=("Yes", "No")):
    p = governance.open_proposal(
        db, member, question, options, vote_type_slug="rules", signer=signer, channel="web"
    )
    db.commit()
    return p


def say(client, text, direct=True, history=None):
    body = {"sender": BEN, "room_id": "!dm:x", "text": text, "direct": direct,
            "history": history or []}  # fmt: skip
    r = client.post("/internal/assistant", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def model_reads(fake_llm, choice, topic=None, ref=None, about="me"):
    fake_llm.replies.append(
        {
            "intent": "vote",
            "about": about,
            "vote_choice": choice,
            "vote_topic": topic,
            "vote_ref": ref,
            "reply": None,
            "display_name": None,
        }
    )


def ballots(db):
    db.expire_all()
    return db.scalars(select(Vote).where(Vote.superseded_at.is_(None))).all()


@pytest.mark.parametrize(
    ("choice", "label"),
    [("in favour", "Yes"), ("yes", "Yes"), ("against", "No"), ("neutral", "Abstain"),
     ("abstain", "Abstain"), ("2", "No")],
)  # fmt: skip
def test_own_words_are_confirmed_before_recording(client, db, fake_llm, log_signer, people,
                                                   choice, label):  # fmt: skip
    p = open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, choice, topic="pump")
    asked = say(client, "something in my own words")
    assert f"Did you mean: {label} on vote #{p.id}, “Buy a new pump?”?" in asked
    assert ballots(db) == []
    assert f"Recorded your vote: {label}" in say(client, "yes")
    (ballot,) = ballots(db)
    assert ballot.channel == "matrix"


def test_option_names_are_understood(client, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"], options=("Yes", "No", "Wait until Monday"))
    model_reads(fake_llm, "waiting until monday")
    assert "Wait until Monday on vote" in say(client, "let's wait until monday")
    say(client, "yes")
    assert ballots(db)[0].option_id == p.options[2].id


def test_saying_no_records_nothing(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, "yes")
    say(client, "count me in")
    assert say(client, "no") == "OK, I didn't record anything."
    assert ballots(db) == []


def test_moving_on_lets_the_question_lapse(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, "yes")
    say(client, "count me in")
    say(client, "what's the weather like?")
    assert say(client, "yes") is not None
    assert ballots(db) == []


def test_unclear_choice_records_nothing(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, "maybe later")
    assert "couldn't tell which option" in say(client, "hmm, maybe later")
    say(client, "yes")
    assert ballots(db) == []


def test_several_open_votes_need_to_be_named(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    barn = open_vote(db, log_signer, people["anna"], question="Paint the barn red?")
    model_reads(fake_llm, "yes")
    assert "Which vote do you mean?" in say(client, "yes from me")
    model_reads(fake_llm, "yes", topic="barn")
    assert f"vote #{barn.id}" in say(client, "yes to the barn")


def test_only_in_a_private_chat_and_only_for_yourself(client, db, fake_llm, log_signer, people):
    open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, "yes")
    assert "direct chat" in say(client, "I'm for it", direct=False)
    model_reads(fake_llm, "yes", about="someone_else")
    assert "only record your own vote" in say(client, "please put Frida down as a yes")
    say(client, "yes")
    assert ballots(db) == []


def test_a_vote_that_closed_meanwhile_is_refused(client, db, fake_llm, log_signer, people):
    p = open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, "yes")
    say(client, "I'm for it")
    governance.close_if_due(db, p.id, signer=log_signer, now=p.closes_at)
    db.commit()
    assert "couldn't record" in say(client, "yes")
    assert ballots(db) == []


def test_the_model_never_sees_the_votes_or_the_ballot_history(
    client, db, fake_llm, log_signer, people
):
    open_vote(db, log_signer, people["anna"])
    model_reads(fake_llm, "yes")
    history = [
        {"role": "user", "text": "I'm for the pump"},
        {"role": "assistant", "text": "Did you mean: Yes on vote #1, “Buy a new pump?”? ..."},
    ]
    say(client, "count me in", history=history)
    prompt = fake_llm.calls[-1]
    assert "Buy a new pump?" not in prompt[0]["content"]  # no member text in the system turn
    assert "I'm for the pump" not in str(prompt)
    assert "Did you mean" not in str(prompt)


def test_choice_mapping_without_the_model():
    class P:
        options = (
            type("O", (), {"id": 1, "label": "Yes"}),
            type("O", (), {"id": 2, "label": "No"}),
        )

    assert vote_chat._nl_choice(P, "In favour!") == 1
    assert vote_chat._nl_choice(P, "against") == 2
    assert vote_chat._nl_choice(P, "pass") == governance.ABSTAIN
    assert vote_chat._nl_choice(P, "perhaps") is None


@pytest.mark.parametrize(
    ("text", "model", "ref", "topic"),
    [
        # Seen with the real model: the number missed and a topic copied from the examples.
        ("put me down as against on vote 3", {"vote_topic": "barn painting"}, 3, None),
        ("I'm in favour of buying the new pump", {"vote_topic": "new pump"}, None, "new pump"),
        ("yes on #12 please", {"vote_ref": 7}, 12, None),
        ("yes to that one", {"vote_ref": 7}, None, None),
    ],
)
def test_only_what_the_message_says_is_kept(text, model, ref, topic):
    from app.assistant import ballot_request_from_model

    req = ballot_request_from_model({"vote_choice": "yes", **model}, text)
    assert (req.ref, req.topic) == (ref, topic)
