"""Signed, append-only decision log (frozen v1 entry format).

Each entry is the canonical JSON of
``{seq, ts, actor, kind, payload, prev_hash, key_id}``; ``entry_hash`` is its
SHA-256 and ``signature`` is Ed25519 over the 32 raw digest bytes.

``append`` writes in the caller's transaction, so a state change and its log
entry commit (or roll back) together. Hashing and verification (with the tally
replay) live in ``app.chain_verify``, which needs neither the database nor the
app, so ``scripts/verify_log.py`` runs the same code on an exported log away
from the box.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.chain_verify import (  # noqa: F401  (re-exported: the one implementation)
    GENESIS_HASH,
    VerifyResult,
    canonical,
    entry_body,
    entry_hash,
    replay_tallies,
    verify_chain,
)
from app.models import DecisionLog, SigningKey, utcnow

KEY_FILE = "log_signing_ed25519.pem"
# pg_advisory_xact_lock id that serializes appends ("COMMONS1" as a bigint).
LOG_LOCK_ID = 0x434F4D4D4F4E5331

KINDS = frozenset(
    {
        "proposal.opened",
        "vote.cast",
        "vote.changed",
        "proposal.closed",
        "proposal.cancelled",
        "config.changed",
        "role.granted",
        "role.revoked",
        "member.invited",
        "member.deactivated",
        "booking.changed_by_admin",
        "booking.cancelled_by_admin",
        "key.rotated",
        "log.anchored",
        "ledger.digest",
    }
)


class DecisionLogError(RuntimeError):
    """The log cannot be appended to (bad entry, missing key, ...)."""


# --- canonical form ---------------------------------------------------------


def format_ts(ts: datetime) -> str:
    """UTC, whole seconds, trailing Z: ``2026-11-12T18:03:11Z``."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware UTC")
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- keys -------------------------------------------------------------------


@dataclass(frozen=True)
class Signer:
    key_id: str
    private_key: Ed25519PrivateKey

    @property
    def public_key_hex(self) -> str:
        return public_key_hex(self.private_key.public_key())

    def sign(self, digest_hex: str) -> str:
        return self.private_key.sign(bytes.fromhex(digest_hex)).hex()


def public_key_hex(key: Ed25519PublicKey) -> str:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def key_id_for(public_hex: str) -> str:
    """Stable id derived from the public key, so ids never clash across installs."""
    return "ed25519:" + hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


def _signer(private_key: Ed25519PrivateKey) -> Signer:
    return Signer(key_id_for(public_key_hex(private_key.public_key())), private_key)


def load_or_create_signer(key_dir: Path, *, allow_create: bool = True) -> Signer:
    """Load the log signing key, or generate it on first start (dir 0700, file 0600).

    The key file is created with O_EXCL, so two processes can't both write one.
    """
    path = key_dir / KEY_FILE
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise DecisionLogError(f"{path} must not be readable by group or others (chmod 600)")
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise DecisionLogError(f"{path} is not an Ed25519 private key")
        return _signer(key)
    if not allow_create:
        raise DecisionLogError(
            f"decision-log signing key {path} is missing, but the log already has signing "
            "keys. Restore the key from the backup; a new key needs a key.rotated entry."
        )
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return _signer(key)


def init_signer(db: Session, key_dir: Path) -> Signer:
    """Startup: load (or on a fresh install, create) the key and register its public half.

    Refuses to silently start a second key when the log already has one but the
    private key file is gone (lost volume, partial restore).
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOG_LOCK_ID})
    registered = db.scalar(select(SigningKey.key_id).limit(1)) is not None
    signer = load_or_create_signer(key_dir, allow_create=not registered)
    row = db.get(SigningKey, signer.key_id)
    if row is None:
        if registered:
            raise DecisionLogError(
                f"signing key {signer.key_id} is not registered for this log; "
                "rotating keys needs a key.rotated entry (not supported yet)"
            )
        db.add(SigningKey(key_id=signer.key_id, public_key=signer.public_key_hex))
        db.flush()
    elif row.public_key != signer.public_key_hex:
        raise DecisionLogError(f"public key for {signer.key_id} does not match the key file")
    elif row.retired_at is not None:
        raise DecisionLogError(f"signing key {signer.key_id} is retired")
    return signer


def public_keys(db: Session) -> dict[str, str]:
    """key_id -> raw public key hex, retired keys included (old entries still verify)."""
    return {k.key_id: k.public_key for k in db.scalars(select(SigningKey))}


# --- append -----------------------------------------------------------------


def append(
    db: Session,
    signer: Signer,
    kind: str,
    payload: Mapping[str, Any],
    actor: int | None = None,
) -> DecisionLog:
    """Append one signed entry inside the caller's transaction (the caller commits).

    A transaction-level advisory lock serializes appends, so two writers never
    chain onto the same head; the primary key on ``seq`` is the backstop.
    """
    if kind not in KINDS:
        raise DecisionLogError(f"unknown decision-log kind {kind!r}")
    if not isinstance(payload, Mapping):
        raise DecisionLogError("payload must be a JSON object")
    if actor is not None and (not isinstance(actor, int) or isinstance(actor, bool)):
        raise DecisionLogError("actor must be a member id or None")
    try:
        # Round-trip through canonical JSON: what we store is exactly what we hash.
        clean_payload = json.loads(canonical(dict(payload)))
    except (TypeError, ValueError) as e:
        raise DecisionLogError(f"payload is not valid JSON: {e}") from None
    if _has_nul(clean_payload):
        # PostgreSQL JSONB can't store U+0000; fail here with a clear message.
        raise DecisionLogError("payload must not contain NUL characters")

    db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOG_LOCK_ID})
    head = db.execute(
        select(DecisionLog.seq, DecisionLog.entry_hash).order_by(DecisionLog.seq.desc()).limit(1)
    ).first()
    seq, prev = (head.seq + 1, head.entry_hash) if head else (1, GENESIS_HASH)
    ts = utcnow().replace(microsecond=0)
    body = {
        "seq": seq,
        "ts": format_ts(ts),
        "actor": actor,
        "kind": kind,
        "payload": clean_payload,
        "prev_hash": prev,
        "key_id": signer.key_id,
    }
    digest = entry_hash(body)
    row = DecisionLog(
        seq=seq,
        ts=ts,
        actor_member_id=actor,
        kind=kind,
        payload_json=clean_payload,
        prev_hash=prev,
        entry_hash=digest,
        signature=signer.sign(digest),
        key_id=signer.key_id,
    )
    db.add(row)
    db.flush()
    return row


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


def export_entry(row: DecisionLog) -> dict[str, Any]:
    """A stored row in the export/verify shape (envelope + entry_hash + signature)."""
    return {
        "seq": row.seq,
        "ts": format_ts(row.ts),
        "actor": row.actor_member_id,
        "kind": row.kind,
        "payload": row.payload_json,
        "prev_hash": row.prev_hash,
        "key_id": row.key_id,
        "entry_hash": row.entry_hash,
        "signature": row.signature,
    }


def load_entries(db: Session) -> list[dict[str, Any]]:
    return [export_entry(r) for r in db.scalars(select(DecisionLog).order_by(DecisionLog.seq))]


# --- verify -----------------------------------------------------------------


def verify_db(db: Session, anchors: Iterable[tuple[int, str]] = ()) -> VerifyResult:
    """The whole stored chain: links, signatures, anchors and the tally replay."""
    return verify_chain(load_entries(db), public_keys(db), anchors)


def export_lines(db: Session) -> Iterable[str]:
    """The log as JSON Lines, one exported entry per line, oldest first."""
    for row in db.scalars(select(DecisionLog).order_by(DecisionLog.seq)).yield_per(500):
        yield json.dumps(export_entry(row), ensure_ascii=False, sort_keys=True) + "\n"
