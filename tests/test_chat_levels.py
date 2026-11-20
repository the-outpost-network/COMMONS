"""Levels and history in chat (D4): rules, model intents, replies written by code."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from tests.conftest import INTERNAL_TOKEN, SERVER

from app import categories, ledger
from app.chat_levels import HISTORY_MAX, parse_question_rule
from app.i18n import en
from app.models import ResourceEntry, utcnow

NORA = f"@nora:{SERVER}"
ROOM = f"!dm:{SERVER}"
AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
CASES = yaml.safe_load((Path(__file__).parent / "intent_cases.yaml").read_text())
Spec = categories.CategorySpec


@pytest.fixture
def resources(db):
    water = categories.create(
        db,
        Spec(slug="water", name="Water", unit="l", kind="stock", shortage_threshold=1000,
             capacity=3000, aliases=["tank", "water tank"]),
    )  # fmt: skip
    wood = categories.create(
        db,
        Spec(slug="firewood", name="Firewood", unit="m³", kind="stock", shortage_threshold=2,
             aliases=["wood", "logs"]),
    )  # fmt: skip
    db.commit()
    return water, wood


@pytest.fixture
def ada(db, log_signer):
    from app import members

    m = members.add_member(db, f"@ada:{SERVER}", "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


def say(client, text: str) -> str:
    r = client.post(
        "/internal/assistant",
        json={"sender": NORA, "room_id": ROOM, "text": text},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def model_says(fake_llm, intent: str, **slots):
    keys = ("resource", "entry_type", "amount", "unit", "day", "time", "note", "count")
    fake_llm.replies.append(
        {"intent": intent, "about": "me", "display_name": None,
         "reply": slots.pop("reply", None), **{k: slots.get(k) for k in keys}}
    )  # fmt: skip


def log(db, member, category, entry_type, amount, hours_ago=0.0, note=None):
    now = utcnow()
    e = ledger.add_entry(
        db, member, category, entry_type, amount, channel="matrix",
        occurred_at=now - timedelta(hours=hours_ago), now=now, note=note,
    )  # fmt: skip
    db.commit()
    return e


def no_new_entries(db, before: int) -> bool:
    db.expire_all()
    return db.query(ResourceEntry).count() == before


# -- rules ----------------------------------------------------------------------


QUESTION_CASES = [c for c in CASES if c.get("question")]


@pytest.mark.parametrize("case", QUESTION_CASES, ids=[c["text"] for c in QUESTION_CASES])
def test_rules_understand_questions(case):
    q = parse_question_rule(case["text"])
    assert q is not None
    assert q.kind == case["question"]


@pytest.mark.parametrize(
    ("text", "kind", "resource", "count"),
    [
        ("status?", "levels", None, 5),
        ("how much firewood is left", "levels", "firewood", 5),
        ("water level?", "levels", "water", 5),
        ("recent water entries", "history", "water", 5),
        ("water history", "history", "water", 5),
        ("show me the last 10 water readings", "history", "water", 10),
        ("Hi COMMONS! levels", "levels", None, 5),
    ],
)
def test_rule_details(text, kind, resource, count):
    q = parse_question_rule(text)
    assert (q.kind, q.resource, q.count) == (kind, resource, count)


@pytest.mark.parametrize("text", ["we used 2 m³ firewood", "hello", "change my name to Nora"])
def test_not_a_question(text):
    assert parse_question_rule(text) is None


# -- replies -----------------------------------------------------------------------


def test_level_with_age_and_who(client, db, nora, resources, fake_llm):
    water, _ = resources
    log(db, nora, water, "reading", 1850, hours_ago=3)
    log(db, nora, water, "use", 120, hours_ago=1)
    assert say(client, "how much water do we have?") == (
        "Water: 1,730 l (last reading 3 h ago by Nora)."
    )


def test_greeting_and_chat_word(client, db, nora, resources):
    water, _ = resources
    log(db, nora, water, "reading", 1850)
    assert say(client, "ola, how much water do we have?").startswith("Water: 1,850 l")
    assert say(client, "how much is left in the tank?").startswith("Water: 1,850 l")


def test_low_and_stale(client, db, nora, resources):
    _, wood = resources
    log(db, nora, wood, "reading", 1.5, hours_ago=72)
    reply = say(client, "how much firewood is left")
    assert "Firewood: 1.5 m³ (last reading 3 days ago by Nora)." in reply
    assert en.LEVEL_LOW.format(threshold="2", unit="m³") in reply
    assert en.LEVEL_STALE in reply


def test_all_levels(client, db, nora, resources):
    water, _ = resources
    categories.create(db, Spec(slug="gas", name="Gas", unit="kg", kind="flow"))
    db.commit()
    gas = categories.by_slug(db, "gas")
    log(db, nora, water, "reading", 1850)
    log(db, nora, gas, "use", 3)
    lines = say(client, "levels").split("\n")
    assert lines[0].startswith("Water: 1,850 l")
    assert lines[1] == en.LEVEL_NONE.format(name="Firewood")  # nothing logged at all
    assert lines[2] == "Gas: 3 kg used in the last 7 days."


def test_level_counted_from_zero_says_so(client, db, nora, resources):
    _, wood = resources
    log(db, nora, wood, "add", 1)
    reply = say(client, "how much firewood do we have?")
    assert reply.startswith(en.LEVEL_FROM_ZERO_LINE.format(name="Firewood", value="1", unit="m³"))
    assert en.LEVEL_LOW.format(threshold="2", unit="m³") in reply


def test_history_with_numbers_corrections_and_notes(client, db, nora, ada, resources):
    _, wood = resources
    r = log(db, nora, wood, "reading", 4, hours_ago=5)
    u = log(db, nora, wood, "use", 2, hours_ago=2, note="sauna")
    ledger.correct_entry(db, ada, u, 1.5, channel="web")
    db.commit()
    reply = say(client, "last 5 firewood entries")
    head, first, second = reply.split("\n")
    assert head == "Last 2 Firewood entries:"
    assert first.startswith(f"#{u.id} ")
    assert "Nora used 2 m³ (corrected to 1.5 m³)" in first
    assert first.endswith('"sauna"')
    assert second.startswith(f"#{r.id} ")
    assert "Nora reading 4 m³" in second


def test_history_count_is_capped_and_empty_history(client, db, nora, resources, fake_llm):
    water, _ = resources
    for i in range(HISTORY_MAX + 3):
        log(db, nora, water, "use", 1, hours_ago=i / 10)
    model_says(fake_llm, "show_history", resource="water", count=20)
    assert say(client, "show me all water entries ever").count("\n#") == HISTORY_MAX
    assert say(client, "last 3 wood entries") == en.HISTORY_NONE.format(name="Firewood")


def test_model_questions_are_answered_by_code(client, db, nora, resources, fake_llm):
    water, _ = resources
    log(db, nora, water, "reading", 1850)
    before = db.query(ResourceEntry).count()
    # The model's own text with a made-up number is never used.
    model_says(fake_llm, "show_levels", resource="water tank", reply="We have 99,999 l!")
    reply = say(client, "do we still have enough water for the weekend?")
    assert reply.startswith("Water: 1,850 l")
    assert "99,999" not in reply
    # A question the model took for a log entry is answered too, not logged.
    model_says(fake_llm, "log_entry", resource="water", entry_type="reading", amount=1000)
    assert say(client, "is the water tank at 1000 l?").startswith("Water: 1,850 l")
    assert no_new_entries(db, before)


def test_unknown_resource_or_none_configured(client, db, nora, resources, fake_llm):
    model_says(fake_llm, "show_levels", resource="petrol")
    assert say(client, "how is the petrol situation?") == en.LEVEL_WHICH.format(
        names="Water, Firewood"
    )
    model_says(fake_llm, "show_history", resource=None)
    assert "Which resource" in say(client, "show me the history")
    for c in categories.list_all(db):
        categories.delete(db, c)
    db.commit()
    assert say(client, "levels") == en.LOG_NO_RESOURCES


def test_archived_resource_is_not_listed(client, db, nora, resources):
    _, wood = resources
    categories.archive(db, wood)
    db.commit()
    assert "Firewood" not in say(client, "levels")


# --- questions about the place, not about a resource ----------------------------


def test_question_about_something_untracked_gets_the_models_answer(
    client, db, nora, resources, fake_llm
):
    """The live model proposed "when is dinner?" as the level of "dinner"."""
    before = db.query(ResourceEntry).count()
    model_says(fake_llm, "show_levels", resource="dinner", reply="Dinner is around 19:00.")
    assert say(client, "when is dinner?") == "Dinner is around 19:00."
    model_says(fake_llm, "log_entry", resource="bed sheets", entry_type="add",
               reply="Upstairs in the school.")  # fmt: skip
    assert say(client, "where do I put the bed sheets?") == "Upstairs in the school."
    model_says(fake_llm, "show_history", resource="dishwasher")  # no answer: code asks
    assert say(client, "how does the dishwasher work?") == en.LEVEL_WHICH.format(
        names="Water, Firewood"
    )
    assert no_new_entries(db, before)


def test_question_about_a_tracked_resource_still_gets_the_levels(
    client, db, nora, resources, fake_llm
):
    model_says(fake_llm, "show_levels", resource="tank", reply="You have 9,999 l.")
    answer = say(client, "how much is in the tank?")
    assert answer.startswith("Water:")
    assert "9,999" not in answer
    model_says(fake_llm, "log_entry", resource="water", entry_type="reading", reply="Lots.")
    assert say(client, "how much water is left?").startswith("Water:")
