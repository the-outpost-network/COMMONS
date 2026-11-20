"""Decision log: canonical form, signing, appends and tamper detection."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
import threading
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import decision_log as dl
from app import members
from app.db import make_sessionmaker
from app.models import DecisionLog


@pytest.fixture
def sm(clean_db):
    return make_sessionmaker(clean_db)


@pytest.fixture
def signer(sm, tmp_path):
    with sm() as db, db.begin():
        return dl.init_signer(db, tmp_path / "keys")


def append_n(sm, signer, n: int, kind: str = "config.changed") -> None:
    with sm() as db, db.begin():
        for i in range(n):
            dl.append(db, signer, kind, {"key": f"k{i}", "old": None, "new": i})


def chain(sm):
    with sm() as db:
        return dl.load_entries(db), dl.public_keys(db)


# --- format -----------------------------------------------------------------


def test_canonical_json_is_sorted_compact_utf8():
    assert (
        dl.canonical({"b": 1, "a": {"d": [1, 2], "c": "m³"}})
        == '{"a":{"c":"m³","d":[1,2]},"b":1}'.encode()
    )


def test_ts_is_whole_seconds_utc_z():
    ts = datetime(2026, 11, 12, 18, 3, 11, 987654, tzinfo=UTC)
    assert dl.format_ts(ts) == "2026-11-12T18:03:11Z"
    with pytest.raises(ValueError, match="naive"):
        dl.format_ts(datetime(2026, 1, 1))  # noqa: DTZ001


def test_entry_matches_frozen_v1_format(sm, signer):
    with sm() as db, db.begin():
        actor = members.add_member(db, "@nora:example.test").id
        row = dl.append(db, signer, "role.granted", {"member": 7, "role": "board"}, actor=actor)
    entry = dl.export_entry(row)
    body = {
        "seq": 1,
        "ts": entry["ts"],
        "actor": actor,
        "kind": "role.granted",
        "payload": {"member": 7, "role": "board"},
        "prev_hash": "0" * 64,
        "key_id": signer.key_id,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert entry["entry_hash"] == hashlib.sha256(raw).hexdigest()
    # Ed25519 over the 32 raw digest bytes.
    signer.private_key.public_key().verify(
        bytes.fromhex(entry["signature"]), bytes.fromhex(entry["entry_hash"])
    )


def test_chain_links_and_verifies(sm, signer):
    append_n(sm, signer, 5)
    entries, keys = chain(sm)
    assert [e["seq"] for e in entries] == [1, 2, 3, 4, 5]
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
    result = dl.verify_chain(entries, keys)
    assert result.ok
    assert (result.head_seq, result.head_hash) == (5, entries[-1]["entry_hash"])


def test_empty_log_verifies():
    assert dl.verify_chain([], {}) == dl.VerifyResult(True, 0, "0" * 64)


def test_payload_survives_jsonb_round_trip(sm, signer):
    payload = {"unit": "m³", "threshold": 2.0, "big": 10**15, "nested": {"z": [None, True]}}
    with sm() as db, db.begin():
        dl.append(db, signer, "config.changed", payload)
    with sm() as db:
        assert dl.verify_db(db).ok


@pytest.mark.parametrize(
    ("kind", "payload", "actor", "match"),
    [
        ("vote.deleted", {}, None, "unknown decision-log kind"),
        ("vote.cast", ["not", "an", "object"], None, "JSON object"),
        ("vote.cast", {"x": float("nan")}, None, "not valid JSON"),
        ("vote.cast", {"x": object()}, None, "not valid JSON"),
        ("vote.cast", {"x": "a\x00b"}, None, "NUL"),
        ("vote.cast", {}, "3", "member id"),
        ("vote.cast", {}, True, "member id"),
    ],
)
def test_append_rejects_bad_entries(sm, signer, kind, payload, actor, match):
    with sm() as db, pytest.raises(dl.DecisionLogError, match=match):
        dl.append(db, signer, kind, payload, actor=actor)


def test_append_rolls_back_with_the_callers_transaction(sm, signer):
    with sm() as db:
        dl.append(db, signer, "config.changed", {"key": "x"})
        db.rollback()
    with sm() as db, db.begin():
        assert dl.append(db, signer, "config.changed", {"key": "y"}).seq == 1


# --- keys -------------------------------------------------------------------


def test_key_generated_once_with_0600(tmp_path):
    key_dir = tmp_path / "keys"
    first = dl.load_or_create_signer(key_dir)
    path = key_dir / dl.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
    again = dl.load_or_create_signer(key_dir)
    assert again.key_id == first.key_id
    assert again.public_key_hex == first.public_key_hex


def test_key_readable_by_others_is_refused(tmp_path):
    dl.load_or_create_signer(tmp_path)
    (tmp_path / dl.KEY_FILE).chmod(0o644)
    with pytest.raises(dl.DecisionLogError, match="chmod 600"):
        dl.load_or_create_signer(tmp_path)


def test_public_key_registered_in_signing_key(sm, signer):
    with sm() as db:
        assert dl.public_keys(db) == {signer.key_id: signer.public_key_hex}


def test_lost_key_file_is_not_silently_replaced(sm, signer, tmp_path):
    (tmp_path / "keys" / dl.KEY_FILE).unlink()
    with sm() as db, db.begin(), pytest.raises(dl.DecisionLogError, match="missing"):
        dl.init_signer(db, tmp_path / "keys")


def test_unregistered_key_is_refused(sm, signer, tmp_path):
    other = tmp_path / "other"
    dl.load_or_create_signer(other)
    with sm() as db, db.begin(), pytest.raises(dl.DecisionLogError, match="not registered"):
        dl.init_signer(db, other)


# --- tampering --------------------------------------------------------------


@pytest.fixture
def five(sm, signer):
    append_n(sm, signer, 5)
    return chain(sm)


def test_edited_payload_detected_at_its_seq(five):
    entries, keys = five
    forged = copy.deepcopy(entries)
    forged[2]["payload"]["new"] = 999
    result = dl.verify_chain(forged, keys)
    assert (result.ok, result.failed_seq, result.head_seq) == (False, 3, 2)
    assert "hash" in result.reason


def test_edited_payload_with_recomputed_hash_fails_signature(five):
    entries, keys = five
    forged = copy.deepcopy(entries)
    forged[2]["payload"]["new"] = 999
    forged[2]["entry_hash"] = dl.entry_hash(dl.entry_body(forged[2]))
    result = dl.verify_chain(forged, keys)
    assert (result.failed_seq, result.reason) == (3, "signature is not valid")


def test_resigned_forgery_breaks_the_next_link(five):
    entries, keys = five
    forger = Ed25519PrivateKey.generate()
    forged = copy.deepcopy(entries)
    forged[2]["payload"]["new"] = 999
    forged[2]["entry_hash"] = dl.entry_hash(dl.entry_body(forged[2]))
    forged[2]["signature"] = forger.sign(bytes.fromhex(forged[2]["entry_hash"])).hex()
    keys = {**keys, forged[2]["key_id"]: dl.public_key_hex(forger.public_key())}
    result = dl.verify_chain(forged, keys)
    # Entry 3 now verifies under the forger's key (an admin re-signing); entry 1 doesn't.
    assert result.failed_seq == 1


def test_deleted_row_detected_at_its_seq(five):
    entries, keys = five
    result = dl.verify_chain(entries[:2] + entries[3:], keys)
    assert (result.ok, result.failed_seq) == (False, 3)
    assert "missing" in result.reason


def test_deleted_first_row_detected(five):
    entries, keys = five
    assert dl.verify_chain(entries[1:], keys).failed_seq == 1


def test_reordered_rows_detected(five):
    entries, keys = five
    swapped = [entries[0], entries[2], entries[1], *entries[3:]]
    assert dl.verify_chain(swapped, keys).failed_seq == 2


def test_truncated_tail_passes_alone_but_fails_against_anchor(five):
    entries, keys = five
    anchor = (5, entries[4]["entry_hash"])
    assert dl.verify_chain(entries[:3], keys).ok
    result = dl.verify_chain(entries[:3], keys, anchors=[anchor])
    assert (result.ok, result.failed_seq, result.head_seq) == (False, 5, 3)
    assert dl.verify_chain(entries, keys, anchors=[anchor]).ok


def test_rewritten_chain_fails_against_anchor(five):
    entries, keys = five
    result = dl.verify_chain(entries, keys, anchors=[(4, "f" * 64)])
    assert (result.failed_seq, result.reason) == (4, "entry 4 does not match its anchor")


def test_wrong_key_detected(five):
    entries, keys = five
    stranger = dl.public_key_hex(Ed25519PrivateKey.generate().public_key())
    result = dl.verify_chain(entries, {k: stranger for k in keys})
    assert (result.failed_seq, result.reason) == (1, "signature is not valid")


def test_unknown_key_id_detected(five):
    entries, _ = five
    result = dl.verify_chain(entries, {})
    assert result.failed_seq == 1
    assert "unknown signing key" in result.reason


def test_db_level_tampering_by_superuser_detected(sm, five):
    """A rogue admin who disables the trigger and edits/deletes rows is still caught."""
    with sm() as db, db.begin():
        db.execute(text("ALTER TABLE decision_log DISABLE TRIGGER USER"))
        db.execute(
            text(
                "UPDATE decision_log SET payload_json = jsonb_set(payload_json, '{new}', '42') "
                "WHERE seq = 4"
            )
        )
        db.execute(text("ALTER TABLE decision_log ENABLE TRIGGER USER"))
    with sm() as db:
        assert dl.verify_db(db).failed_seq == 4

    with sm() as db, db.begin():
        db.execute(text("ALTER TABLE decision_log DISABLE TRIGGER USER"))
        db.execute(text("DELETE FROM decision_log WHERE seq = 2"))
        db.execute(text("ALTER TABLE decision_log ENABLE TRIGGER USER"))
    with sm() as db:
        assert dl.verify_db(db).failed_seq == 2


# --- concurrency ------------------------------------------------------------


def test_concurrent_appends_produce_a_valid_chain(sm, signer):
    workers, per_worker = 6, 8
    start = threading.Barrier(workers)
    errors: list[BaseException] = []

    def work(w: int) -> None:
        try:
            start.wait()
            for i in range(per_worker):
                with sm() as db, db.begin():
                    dl.append(db, signer, "config.changed", {"worker": w, "i": i})
        except BaseException as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    entries, keys = chain(sm)
    assert len(entries) == workers * per_worker
    assert dl.verify_chain(entries, keys).ok


def test_seq_primary_key_is_the_backstop(sm, signer):
    with sm() as db, db.begin():
        row = dl.append(db, signer, "config.changed", {})
    with sm() as db:
        db.add(
            DecisionLog(
                seq=1,
                ts=row.ts,
                kind="config.changed",
                payload_json={},
                prev_hash="0" * 64,
                entry_hash="1" * 64,
                signature="2" * 128,
                key_id=signer.key_id,
            )
        )
        with pytest.raises(IntegrityError, match="duplicate key"):
            db.flush()
