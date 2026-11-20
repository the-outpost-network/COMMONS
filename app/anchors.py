"""Chain heads in the community room, and a daily seal over the ledger.

**Anchors.** After every closed vote, and once a day, COMMONS posts the decision
log's latest entry number and fingerprint (`seq:entry_hash`) in the community room.
Members' phones then hold copies the box can't take back: if anyone later rewrote
or cut the log, `scripts/verify_log.py --anchor seq:hash` with a posted fingerprint
would fail. Each post is itself a signed `log.anchored` entry, written in the same
transaction as the room line (`app/feed.py`), so the log says which fingerprints
went out. Without a community room nothing is posted: an anchor nobody can see
witnesses nothing.

**Ledger digest.** Ledger entries are too many for the chain, so once a day a
`ledger.digest` entry seals the previous day's entries: their count, first and
last id, and the SHA-256 of their canonical JSON (`DIGEST_FIELDS`, oldest first).
Days without entries get a digest too, so a day can't quietly gain entries later.
Missed days (the box was off) are caught up, oldest first.

The daily work runs when the community's quiet hours end (`bookings.quiet_hours_end`,
08:00 by default), so the room line never arrives at night.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app import bookables, community, decision_log
from app.events import Event, publish
from app.models import DecisionLog, ResourceEntry, utcnow
from app.stream import ROOM_KEY

log = logging.getLogger("commons.anchors")

ANCHORED = "log.anchored"
DIGEST = "ledger.digest"
CHECK_SECONDS = 60
MAX_CATCH_UP_DAYS = 400
# What a digest covers, per entry. Changing it changes every later digest: add a
# new field only together with a `version` bump in the payload.
DIGEST_FIELDS = (
    "id",
    "category_id",
    "member_id",
    "channel",
    "entry_type",
    "amount",
    "note",
    "occurred_at",
    "created_at",
    "edited_at",
    "corrects_id",
    "void",
)
DIGEST_VERSION = 1


def room_set(db: Session) -> bool:
    return bool(community.get(db, ROOM_KEY))


def head(db: Session) -> DecisionLog | None:
    return db.scalar(select(DecisionLog).order_by(DecisionLog.seq.desc()).limit(1))


def anchor(
    db: Session, signer: decision_log.Signer, reason: str, **extra: Any
) -> DecisionLog | None:
    """Post the current head to the community room (caller commits); None without a room.

    `reason`: "proposal.closed", "daily" or "manual". The anchored entry is the one
    before this `log.anchored` entry: an entry can't contain its own hash.
    """
    if not room_set(db):
        return None
    latest = head(db)
    if latest is None:
        return None
    payload = {
        "anchored_seq": latest.seq,
        "anchored_hash": latest.entry_hash,
        "reason": reason,
        "posted_to": "community_room",
        **extra,
    }
    entry = decision_log.append(db, signer, ANCHORED, payload)
    publish(db, Event(ANCHORED, {"seq": latest.seq, "hash": latest.entry_hash}))
    return entry


# --- the ledger digest ------------------------------------------------------------


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def entry_record(e: ResourceEntry) -> dict[str, Any]:
    """One entry as the digest hashes it: exactly `DIGEST_FIELDS`."""
    record = {name: getattr(e, name) for name in DIGEST_FIELDS}
    for name in ("occurred_at", "created_at", "edited_at"):
        record[name] = _ts(record[name])
    return record


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tz)
    return start.astimezone(UTC), datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(
        UTC
    )


def digest_payload(db: Session, day: date, tz: ZoneInfo) -> dict[str, Any]:
    """The seal over the entries written on `day` (community time), oldest first."""
    start, end = day_bounds(day, tz)
    entries = db.scalars(
        select(ResourceEntry)
        .where(ResourceEntry.created_at >= start, ResourceEntry.created_at < end)
        .order_by(ResourceEntry.id)
    ).all()
    records = [entry_record(e) for e in entries]
    return {
        "day": day.isoformat(),
        "timezone": tz.key,
        "entries": len(records),
        "first_id": records[0]["id"] if records else None,
        "last_id": records[-1]["id"] if records else None,
        "sha256": hashlib.sha256(decision_log.canonical(records)).hexdigest(),
        "fields": list(DIGEST_FIELDS),
        "version": DIGEST_VERSION,
    }


def _last_digest_day(db: Session) -> date | None:
    row = db.scalar(
        select(DecisionLog).where(DecisionLog.kind == DIGEST).order_by(DecisionLog.seq.desc())
    )
    return None if row is None else date.fromisoformat(row.payload_json["day"])


def _first_entry_day(db: Session, tz: ZoneInfo) -> date | None:
    first = db.scalar(select(ResourceEntry.created_at).order_by(ResourceEntry.id).limit(1))
    return None if first is None else first.astimezone(tz).date()


def _daily_anchor_done(db: Session, day: date) -> bool:
    return (
        db.scalar(
            select(DecisionLog.seq)
            .where(
                DecisionLog.kind == ANCHORED,
                DecisionLog.payload_json["reason"].astext == "daily",
                DecisionLog.payload_json["day"].astext == day.isoformat(),
            )
            .limit(1)
        )
        is not None
    )


def _starts_at(db: Session) -> time:
    hh, mm = bookables.settings(db).quiet_hours_end.split(":")
    return time(int(hh), int(mm))


def run_daily(
    db: Session, signer: decision_log.Signer, tz: ZoneInfo, now: datetime | None = None
) -> dict[str, int]:
    """Today's digests and anchor, once each; caller commits. Idempotent.

    Before the quiet hours end, nothing. Then: a digest for every day from the one
    after the last digest (or the first ledger entry) up to yesterday, and one
    anchor for today if anything was logged since the last one.
    """
    now = now or utcnow()
    db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": decision_log.LOG_LOCK_ID})
    local = now.astimezone(tz)
    done = {"digests": 0, "anchors": 0}
    if local.time() < _starts_at(db):
        return done
    yesterday = local.date() - timedelta(days=1)
    last = _last_digest_day(db)
    day = last + timedelta(days=1) if last else _first_entry_day(db, tz)
    if day is not None:
        day = max(day, yesterday - timedelta(days=MAX_CATCH_UP_DAYS))
        while day <= yesterday:
            decision_log.append(db, signer, DIGEST, digest_payload(db, day, tz))
            done["digests"] += 1
            day += timedelta(days=1)
    latest = head(db)
    if (
        latest is not None
        and latest.kind != ANCHORED
        and not _daily_anchor_done(db, local.date())
        and anchor(db, signer, "daily", day=local.date().isoformat()) is not None
    ):
        done["anchors"] += 1
    return done


def _check(sm: sessionmaker[Session], signer: decision_log.Signer, tz: ZoneInfo) -> dict:
    with sm() as db, db.begin():
        return run_daily(db, signer, tz)


async def run_forever(
    sm: sessionmaker[Session],
    signer: decision_log.Signer,
    tz: ZoneInfo,
    every_seconds: float = CHECK_SECONDS,
) -> None:
    """Background loop started by the app's lifespan; one app worker, so one loop."""
    while True:
        try:
            done = await run_in_threadpool(_check, sm, signer, tz)
            if any(done.values()):
                log.info("sealed %(digests)d ledger day(s), posted %(anchors)d anchor(s)", done)
        except Exception:
            log.exception("daily ledger digest or anchor failed")
        await asyncio.sleep(every_seconds)
