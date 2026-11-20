"""Undo and corrections from chat (D5): who may fix what, edit vs correction, replies."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError, IntegrityError
from tests.conftest import INTERNAL_TOKEN, SERVER

from app import categories, chat_fix, events, ledger, members
from app.assistant import INTENT_SCHEMA, intent_from_model
from app.chat_fix import FixRequest, parse_fix_rule
from app.i18n import en
from app.models import ResourceEntry, utcnow

NORA = f"@nora:{SERVER}"
BOB = f"@bob:{SERVER}"
ADA = f"@ada:{SERVER}"
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
def bob(db):
    m = members.add_member(db, BOB, "Bob")
    db.commit()
    return m


@pytest.fixture
def ada(db, log_signer):
    m = members.add_member(db, ADA, "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


def say(client, text: str, sender: str = NORA) -> str:
    r = client.post(
        "/internal/assistant",
        json={"sender": sender, "room_id": ROOM, "text": text},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def logged(db, member, category, entry_type, amount, minutes_ago=0):
    """An entry written `minutes_ago` minutes ago by the real clock (the DB checks it too)."""
    when = utcnow() - timedelta(minutes=minutes_ago)
    e = ledger.add_entry(db, member, category, entry_type, amount, channel="matrix", now=when)
    db.commit()
    return e


def rows(db) -> list[ResourceEntry]:
    db.expire_all()
    return db.query(ResourceEntry).order_by(ResourceEntry.id).all()


def model_says(fake_llm, about: str = "me", **slots):
    keys = ("resource", "entry_type", "amount", "unit", "day", "time", "note", "entry_ref")
    fake_llm.replies.append(
        {"intent": "correct_entry", "about": about, "display_name": None, "reply": None,
         "count": None, **{k: slots.get(k) for k in keys}}
    )  # fmt: skip


# -- the rules ------------------------------------------------------------------

FIX_CASES = [c for c in CASES if c.get("rules") == "fix"]


@pytest.mark.parametrize("case", FIX_CASES, ids=[c["text"] for c in FIX_CASES])
def test_rules_understand(case):
    req = parse_fix_rule(case["text"])
    assert req is not None
    for key in ("entry_ref", "amount", "resource", "unit", "undo"):
        if key in case:
            assert getattr(req, key) == case[key], key


@pytest.mark.parametrize(
    "text",
    ["the water was 800", "it was cold", "undo Frida's entry", "we used 2 m³ of firewood",
     "how much water do we have?", "undo 42", "that was wrong"],
)  # fmt: skip
def test_rules_leave_the_rest(text):
    assert parse_fix_rule(text) is None


def test_schema_offers_correct_entry():
    assert "correct_entry" in INTENT_SCHEMA["properties"]["intent"]["enum"]
    assert "entry_ref" in INTENT_SCHEMA["required"]


# -- the author, inside and outside the window ----------------------------------


def test_undo_inside_the_window_keeps_the_entry_in_the_history(
    client, db, nora, resources, fake_llm
):
    water, _ = resources
    logged(db, nora, water, "reading", 1000, minutes_ago=60)
    wrong = logged(db, nora, water, "use", 300, minutes_ago=2)
    heard = []
    stop = events.subscribe(heard.append)
    try:
        reply = say(client, "undo")
    finally:
        stop()
    assert reply.startswith(f"Undone: #{wrong.id} Water used (by Nora, ")
    assert "300 l, no longer counts. It stays in the history. Water is now at 1,000 l." in reply
    *_, fix = rows(db)
    assert (fix.entry_type, fix.void, fix.corrects_id, fix.member_id, fix.channel) == (
        "correction", True, wrong.id, nora.id, "matrix"
    )  # fmt: skip
    assert db.get(ResourceEntry, wrong.id).amount == 300  # the original is untouched
    assert [h.kind for h in heard if h.kind.startswith("ledger.entry_")] == [
        "ledger.entry_corrected"
    ]  # low-level alerts are tested in test_feed.py
    assert "(undone, no longer counts)" in say(client, "last 5 water entries")


def test_fix_inside_the_window_edits(client, db, nora, resources):
    _, wood = resources
    e = logged(db, nora, wood, "use", 2, minutes_ago=3)
    reply = say(client, "that was wrong, it was 1.5")
    assert reply.startswith(f"Fixed: #{e.id} Firewood used (by Nora, ")
    assert "2 m³ → 1.5 m³." in reply
    (only,) = rows(db)  # no correction row: an edit within 15 minutes
    assert (only.amount, only.edited_at is not None) == (1.5, True)
    assert "(edited)" in say(client, "last 5 firewood entries")


def test_fix_outside_the_window_adds_a_correction(client, db, nora, resources):
    _, wood = resources
    logged(db, nora, wood, "reading", 5, minutes_ago=60)
    e = logged(db, nora, wood, "use", 2, minutes_ago=20)
    reply = say(client, "oops, it was 1,5 m³")
    assert reply.startswith(f"Corrected: #{e.id} Firewood used")
    assert "2 m³ → 1.5 m³. The original stays in the history. Firewood is now at 3.5 m³." in reply
    *_, fix = rows(db)
    assert (fix.entry_type, fix.amount, fix.corrects_id, fix.void) == (
        "correction",
        1.5,
        e.id,
        False,
    )
    assert db.get(ResourceEntry, e.id).amount == 2


def test_undoing_a_reading_works_although_zero_is_a_real_level(client, db, nora, resources):
    water, _ = resources
    logged(db, nora, water, "reading", 800, minutes_ago=90)
    logged(db, nora, water, "reading", 2000, minutes_ago=20)
    assert "Water is now at 800 l." in say(client, "undo")
    assert ledger.level(db, water).value == 800


def test_a_later_value_brings_an_undone_entry_back(client, db, nora, resources):
    water, _ = resources
    logged(db, nora, water, "reading", 1000, minutes_ago=60)
    e = logged(db, nora, water, "use", 300, minutes_ago=30)
    say(client, "undo")
    reply = say(client, f"#{e.id} should be 200")
    assert "undone → 200 l" in reply
    assert ledger.level(db, water).value == 800
    assert en.FIX_REFUSED.split(":")[0] in say(client, f"#{e.id} should be 200")  # no change


def test_undo_twice_is_refused(client, db, nora, resources):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=30)
    say(client, "undo")
    assert f"#{e.id} is already undone" in say(client, "undo")


def test_the_last_entry_of_a_named_resource(client, db, nora, resources):
    water, wood = resources
    w = logged(db, nora, wood, "use", 2, minutes_ago=30)
    logged(db, nora, water, "use", 50, minutes_ago=25)
    reply = say(client, "no, the firewood was 1.5")
    assert reply.startswith(f"Corrected: #{w.id} Firewood used")
    assert "I don't know a resource" in say(client, "undo my last petrol entry")


def test_without_a_number_only_the_last_day(client, db, nora, resources):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=2 * 24 * 60)
    reply = say(client, "undo")
    assert reply == en.FIX_LAST_TOO_OLD.format(name="water", ref=e.id)
    assert len(rows(db)) == 1
    assert say(client, f"undo #{e.id}").startswith(f"Undone: #{e.id}")


def test_nothing_to_undo_and_unknown_numbers(client, db, nora, resources):
    assert say(client, "undo") == en.FIX_NOTHING_YET
    assert say(client, "undo #999") == en.FIX_NOT_FOUND.format(ref=999)


def test_wrong_unit_asks(client, db, nora, resources):
    _, wood = resources
    logged(db, nora, wood, "use", 2, minutes_ago=3)
    assert say(client, "it was 2 kg") == en.LOG_WRONG_UNIT.format(
        name="Firewood", unit="m³", given="kg"
    )
    assert rows(db)[0].amount == 2


def test_correction_follows_the_ledger_rules(client, db, nora, resources):
    water, _ = resources
    logged(db, nora, water, "use", 300, minutes_ago=30)
    assert "more than Water can hold" in say(client, "it should be 99999")
    assert len(rows(db)) == 1


# -- other members and admins ---------------------------------------------------


def test_another_member_cannot_fix_my_entry(client, db, nora, bob, resources):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=2)
    assert say(client, f"undo #{e.id}", sender=BOB) == en.FIX_NOT_YOURS.format(ref=e.id)
    assert say(client, f"#{e.id} should be 1", sender=BOB) == en.FIX_NOT_YOURS.format(ref=e.id)
    # "undo" without a number only ever reaches Bob's own entries.
    assert say(client, "undo", sender=BOB) == en.FIX_NOTHING_YET
    (only,) = rows(db)
    assert only.amount == 300


@pytest.mark.parametrize("minutes_ago", [2, 20])
def test_an_admin_corrects_by_number_inside_and_outside_the_window(
    client, db, nora, ada, resources, minutes_ago
):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=minutes_ago)
    reply = say(client, f"correct #{e.id} to 250 l", sender=ADA)
    # Never an in-place edit of someone else's entry, even inside the window.
    assert reply.startswith(f"Corrected: #{e.id} Water used (by Nora, ")
    *_, fix = rows(db)
    assert (fix.member_id, fix.corrects_id, fix.amount) == (ada.id, e.id, 250)
    assert db.get(ResourceEntry, e.id).edited_at is None
    assert say(client, f"undo #{e.id}", sender=ADA).startswith(f"Undone: #{e.id}")


def test_a_correction_number_means_its_original(client, db, nora, resources):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=30)
    say(client, "it was 200")
    fix = rows(db)[-1]
    assert say(client, f"#{fix.id} should be 100").startswith(f"Corrected: #{e.id}")


def test_deactivated_members_are_refused(client, db, nora, resources):
    water, _ = resources
    logged(db, nora, water, "use", 300, minutes_ago=2)
    nora.deactivated_at = utcnow()
    db.commit()
    assert say(client, "undo") == en.ASSISTANT_NOT_A_MEMBER


# -- the model is kept honest ---------------------------------------------------


def test_model_proposal_with_a_number(client, db, nora, resources, fake_llm):
    _, wood = resources
    e = logged(db, nora, wood, "use", 2, minutes_ago=30)
    model_says(fake_llm, entry_ref=e.id, amount=3, unit="m³")
    reply = say(client, f"hey, entry #{e.id} was really 3 m³, not 2")
    assert reply.startswith(f"Corrected: #{e.id} Firewood used")
    assert rows(db)[-1].amount == 3


def test_model_cannot_invent_an_entry_number_or_an_amount(
    client, db, nora, bob, resources, fake_llm
):
    water, _ = resources
    mine = logged(db, nora, water, "use", 300, minutes_ago=30)
    theirs = logged(db, bob, water, "use", 100, minutes_ago=30)
    # The model names Bob's entry and makes up an amount nobody wrote.
    model_says(fake_llm, entry_ref=theirs.id, amount=5)
    reply = say(client, "hmm, my water entry from earlier was not right")
    assert reply == en.FIX_NEED_AMOUNT  # no amount, no undo words: ask
    assert len(rows(db)) == 2
    # The entry number is never read as the amount.
    model_says(fake_llm, entry_ref=None, amount=float(mine.id))
    assert say(client, f"#{mine.id} is wrong, please fix") == en.FIX_NEED_AMOUNT


def test_model_undo_needs_undo_words(client, db, nora, resources, fake_llm):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=30)
    model_says(fake_llm, resource="water")
    assert say(client, "the water thing was a typo, it never happened").startswith(
        f"Undone: #{e.id}"
    )


@pytest.mark.parametrize(
    ("text", "about"),
    [("undo Frida's firewood entry", "someone_else"), ("please undo Frida's last log", "me"),
     ("undo the entry from @bob", "me")],
)  # fmt: skip
def test_model_undo_for_someone_else_is_answered_not_done(
    client, db, nora, resources, fake_llm, text, about
):
    water, _ = resources
    logged(db, nora, water, "use", 300, minutes_ago=2)
    model_says(fake_llm, about=about, resource="firewood")
    assert say(client, text) == en.FIX_SOMEONE_ELSE
    assert len(rows(db)) == 1


def test_grounding():
    g = chat_fix.grounded(FixRequest(entry_ref=7, amount=1.5), "#42 should be 1,5")
    assert (g.entry_ref, g.amount, g.undo) == (None, 1.5, False)
    g = chat_fix.grounded(FixRequest(entry_ref=42, amount=42), "undo #42")
    assert (g.entry_ref, g.amount, g.undo) == (42, None, True)


def test_intent_from_model_builds_a_fix():
    data = {"intent": "correct_entry", "about": "me", "amount": 2, "entry_ref": None,
            "resource": "wood", "unit": "m³"}  # fmt: skip
    intent = intent_from_model(data, "the wood should have been 2 m³")
    assert intent.name == "correct_entry"
    assert intent.fix == FixRequest(amount=2.0, resource="wood", unit="m³")


# -- the database ----------------------------------------------------------------


def test_database_refuses_turning_an_undo_into_something_else(db, nora, resources):
    water, _ = resources
    e = logged(db, nora, water, "use", 300, minutes_ago=2)
    fix = ledger.fix_entry(db, nora, e, None, channel="matrix")
    db.commit()
    with pytest.raises(DBAPIError, match="only amount, note and time"):
        db.execute(update(ResourceEntry).where(ResourceEntry.id == fix.row.id).values(void=False))
    db.rollback()


def test_only_a_correction_can_be_void(db, nora, resources):
    water, _ = resources
    now = utcnow()
    db.add(
        ResourceEntry(category_id=water.id, member_id=nora.id, channel="web", entry_type="use",
                      amount=1, occurred_at=now, created_at=now, void=True)
    )  # fmt: skip
    with pytest.raises(IntegrityError, match="ck_resource_entry_void"):
        db.flush()
    db.rollback()
