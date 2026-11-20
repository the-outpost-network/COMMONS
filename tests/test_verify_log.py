"""Feature 7: tally replay, the verify/export routes and the offline verifier.

Votes go through the real service (app/governance.py), so the chains here are the
ones the box writes. Forgeries are re-hashed and re-signed with the box's own key:
the replay must catch what hashes and signatures can't (someone with the key).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import SERVER, VOTING, sign_in

from app import chain_verify, decision_log, governance, members
from app.governance import ABSTAIN
from app.models import ConfigKV

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_log.py"
NORA = f"@nora:{SERVER}"


@pytest.fixture
def voting(db):
    db.add(ConfigKV(key="voting", value_json=VOTING))
    db.commit()


@pytest.fixture
def people(db, log_signer, voting):
    def add(name, *roles):
        return members.add_member(db, f"@{name}:{SERVER}", name.title(), roles, signer=log_signer)

    p = {"ada": add("ada", "admin"), "bo": add("bo", "board"), "nora": add("nora")}
    p["ben"] = add("ben")
    db.commit()
    return p


def open_(db, signer, member, options=("Yes", "No")):
    p = governance.open_proposal(
        db, member, "Buy a pump?", options, vote_type_slug="rules", signer=signer, channel="web"
    )
    db.commit()
    return p


def vote(db, signer, member, proposal, position):
    choice = ABSTAIN if position == ABSTAIN else proposal.options[position - 1].id
    governance.cast_vote(db, member, proposal.id, choice, signer=signer, channel="matrix")
    db.commit()


def close(db, signer, proposal):
    governance.close_if_due(db, proposal.id, signer=signer, now=proposal.closes_at)
    db.commit()


@pytest.fixture
def history(db, log_signer, people):
    """A closed vote (with a changed vote and an abstention) and a cancelled one."""
    first = open_(db, log_signer, people["nora"], ("Yes", "No", "Later"))
    vote(db, log_signer, people["ada"], first, 1)
    vote(db, log_signer, people["bo"], first, 2)
    vote(db, log_signer, people["bo"], first, 1)  # changed
    vote(db, log_signer, people["nora"], first, ABSTAIN)
    close(db, log_signer, first)
    second = open_(db, log_signer, people["ben"])
    vote(db, log_signer, people["ada"], second, 2)
    governance.cancel_proposal(
        db, people["bo"], second.id, "Asked twice", signer=log_signer, channel="web"
    )
    db.commit()
    return decision_log.load_entries(db), decision_log.public_keys(db)


def resign(entries: list[dict], signer, from_index: int = 0) -> list[dict]:
    """Renumber, re-link, re-hash and re-sign: a forger who holds the key."""
    prev = entries[from_index - 1]["entry_hash"] if from_index else chain_verify.GENESIS_HASH
    for seq, entry in enumerate(entries[from_index:], start=from_index + 1):
        entry["seq"], entry["prev_hash"] = seq, prev
        digest = chain_verify.entry_hash(chain_verify.entry_body(entry))
        entry["entry_hash"], entry["signature"] = digest, signer.sign(digest)
        prev = digest
    return entries


def index_of(entries, kind, nth=0):
    return [i for i, e in enumerate(entries) if e["kind"] == kind][nth]


# --- replay on real chains ------------------------------------------------------


def test_a_real_vote_history_verifies_and_is_recounted(history):
    entries, keys = history
    result = chain_verify.verify_chain(entries, keys)
    assert result.ok, result
    assert result.tallies == 1
    assert result.head_seq == len(entries)


def forge(history, signer, change):
    entries, keys = history
    forged = copy.deepcopy(entries)
    i = change(forged)
    return chain_verify.verify_chain(resign(forged, signer, i), keys), forged[i]["seq"]


def test_a_forged_outcome_is_caught_although_it_is_signed(history, log_signer):
    def flip(entries):
        i = index_of(entries, "proposal.closed")
        p = entries[i]["payload"]
        loser = next(t for t in p["tally"] if t["option_id"] != p["outcome_option_id"])
        p["outcome_option_id"], p["outcome_label"] = loser["option_id"], loser["label"]
        return i

    result, seq = forge(history, log_signer, flip)
    assert not result.ok
    assert result.failed_seq == seq
    assert "outcome" in result.reason


def test_moved_counts_that_change_the_outcome_are_caught(history, log_signer):
    def swap(entries):
        i = index_of(entries, "proposal.closed")
        tally = entries[i]["payload"]["tally"]
        tally[0]["votes"], tally[1]["votes"] = tally[1]["votes"], tally[0]["votes"]
        return i

    result, _ = forge(history, log_signer, swap)
    assert not result.ok
    assert "outcome" in result.reason


def test_an_invented_voter_is_caught(history, log_signer):
    def stuff(entries):
        i = index_of(entries, "proposal.closed")
        p = entries[i]["payload"]
        p["tally"][0]["votes"] += 1
        p["cast"] += 1
        p["turnout"] += 1
        return i

    result, seq = forge(history, log_signer, stuff)
    assert not result.ok
    assert result.failed_seq == seq
    assert "turnout" in result.reason


def test_a_dropped_voter_is_caught(history, log_signer):
    def drop(entries):
        i = index_of(entries, "vote.cast")
        del entries[i]
        return i

    result, _ = forge(history, log_signer, drop)
    assert not result.ok
    assert "turnout" in result.reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("vote_after_close", "after it was closed"),
        ("vote_after_cancel", "after it was cancelled"),
        ("second_cast", "second vote.cast"),
        ("change_without_cast", "never cast"),
        ("unknown_proposal", "never opened"),
        ("other_signer", "not the voter"),
        ("unknown_option", "didn't offer"),
        ("rules_changed", "rules at the close"),
        ("opened_twice", "opened a second time"),
        ("malformed", "malformed"),
    ],
)
def test_vote_records_that_do_not_add_up_are_caught(history, log_signer, change, reason):
    def edit(entries):
        cast = entries[index_of(entries, "vote.cast")]
        closed = index_of(entries, "proposal.closed")
        if change == "vote_after_close":
            entries.insert(closed + 1, copy.deepcopy(cast) | {"kind": "vote.changed"})
            return closed + 1
        if change == "vote_after_cancel":
            i = index_of(entries, "proposal.cancelled")
            late = copy.deepcopy(entries[index_of(entries, "vote.cast", 3)])
            entries.insert(i + 1, late | {"kind": "vote.changed"})
            return i + 1
        if change == "second_cast":
            entries.insert(closed, copy.deepcopy(cast))
            return closed
        if change == "change_without_cast":
            entries[index_of(entries, "vote.cast")]["kind"] = "vote.changed"
            return index_of(entries, "vote.changed")
        if change == "unknown_proposal":
            entries[closed]["payload"]["proposal_id"] = 999
            return closed
        if change == "other_signer":
            cast["actor"] = cast["actor"] + 1
            return index_of(entries, "vote.cast")
        if change == "unknown_option":
            entries[closed]["payload"]["tally"][0]["option_id"] = 999
            return closed
        if change == "rules_changed":
            entries[closed]["payload"]["threshold_pct"] = 75
            return closed
        if change == "opened_twice":
            opened = copy.deepcopy(entries[index_of(entries, "proposal.opened")])
            entries.insert(closed, opened)
            return closed
        del entries[closed]["payload"]["tally"]
        return closed

    result, seq = forge(history, log_signer, edit)
    assert not result.ok
    assert result.failed_seq == seq
    assert reason in result.reason


def test_a_member_deactivated_after_voting_drops_out_of_the_turnout(db, log_signer, people):
    p = open_(db, log_signer, people["nora"])
    vote(db, log_signer, people["ada"], p, 1)
    vote(db, log_signer, people["ben"], p, 2)
    members.deactivate(db, people["ben"], people["ada"].id, signer=log_signer, channel="web")
    db.commit()
    close(db, log_signer, p)
    closed = decision_log.load_entries(db)[-1]["payload"]
    assert closed["turnout"] == 1
    assert decision_log.verify_db(db).ok


def test_replay_can_be_left_out_for_the_chain_alone(history, log_signer):
    entries, keys = history
    forged = copy.deepcopy(entries)
    i = index_of(forged, "proposal.closed")
    forged[i]["payload"]["outcome"] = "tie"
    resign(forged, log_signer, i)
    assert chain_verify.verify_chain(forged, keys, replay=False).ok
    assert not chain_verify.verify_chain(forged, keys).ok


def test_the_verifier_needs_only_the_standard_library_and_cryptography():
    code = (
        "import sys, importlib.util as u;"
        f"s = u.spec_from_file_location('cv', {str(ROOT / 'app' / 'chain_verify.py')!r});"
        "m = u.module_from_spec(s); sys.modules['cv'] = m; s.loader.exec_module(m);"
        "bad = sorted(n for n in sys.modules if n.split('.')[0] in "
        "('app', 'sqlalchemy', 'fastapi', 'pydantic'));"
        "print(','.join(bad))"
    )
    out = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


# --- routes ---------------------------------------------------------------------


def as_member(client, fake_matrix, user_id=NORA):
    fake_matrix.user_id = user_id
    assert sign_in(client, fake_matrix).status_code == 303
    return client


def test_verify_route_reports_the_chain_and_the_recount(client, fake_matrix, history):
    entries, _ = history
    body = as_member(client, fake_matrix).get("/api/log/verify").json()
    assert body["ok"] is True
    assert body["entries"] == len(entries)
    assert body["head_hash"] == entries[-1]["entry_hash"]
    assert body["tallies_recounted"] == 1
    assert body["failed_seq"] is None


def test_verify_button_swaps_in_the_result(client, fake_matrix, history):
    as_member(client, fake_matrix)
    page = client.get("/log").text
    assert 'hx-get="/api/log/verify"' in page
    assert 'href="/api/log/export"' in page
    assert 'href="/api/log/keys"' in page
    fragment = client.get("/api/log/verify", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert 'id="verification"' in fragment.text
    assert "results of all 1 closed votes were recounted" in fragment.text
    assert "<html" not in fragment.text


def load_script():
    spec = importlib.util.spec_from_file_location("verify_log", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_and_offline_verify_round_trip(client, fake_matrix, history, tmp_path):
    verify_log = load_script()
    entries, keys = history
    as_member(client, fake_matrix)
    export = client.get("/api/log/export")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    assert [json.loads(line) for line in export.text.splitlines()] == entries
    assert client.get("/api/log/keys").json() == keys
    log, pub = tmp_path / "log.jsonl", tmp_path / "keys.json"
    log.write_text(export.text, encoding="utf-8")
    pub.write_text(json.dumps(keys), encoding="utf-8")
    head = f"{entries[-1]['seq']}:{entries[-1]['entry_hash']}"
    assert verify_log.main([str(log), "--pubkeys", str(pub), "--anchor", head]) == 0

    lines = export.text.splitlines()
    i = index_of(entries, "proposal.closed")
    tampered = json.loads(lines[i])
    tampered["payload"]["result"] = "rejected"
    lines[i] = json.dumps(tampered)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_log.main([str(log), "--pubkeys", str(pub)]) == 1


def test_offline_verifier_runs_standalone(history, tmp_path):
    entries, keys = history
    log, pub = tmp_path / "log.jsonl", tmp_path / "keys.json"
    log.write_text("".join(json.dumps(e) + "\n" for e in entries[:-1]), encoding="utf-8")
    pub.write_text(json.dumps(keys), encoding="utf-8")
    run = [sys.executable, "-I", str(SCRIPT), str(log), "--pubkeys", str(pub)]
    ok = subprocess.run(run, capture_output=True, text=True, cwd=tmp_path)  # noqa: S603  # nosec B603
    assert ok.returncode == 0, ok.stderr
    assert "OK:" in ok.stdout
    assert "1 vote results recounted" in ok.stdout
    # A truncated log passes alone, but not against an anchor it no longer holds.
    later = f"--anchor={entries[-1]['seq']}:{entries[-1]['entry_hash']}"
    broken = subprocess.run([*run, later], capture_output=True, text=True, cwd=tmp_path)  # noqa: S603  # nosec B603
    assert broken.returncode == 1
    assert f"BROKEN at entry {entries[-1]['seq']}" in broken.stdout
    unreadable = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-I", str(SCRIPT), str(tmp_path / "none"), "--pubkeys", str(pub)],
        capture_output=True,
        text=True,
    )
    assert unreadable.returncode == 2
