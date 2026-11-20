"""The decision log page (/log): every member can read the record of votes.

Read-only. It lists when votes opened, closed (with the outcome) or were
cancelled, newest first, with a short plain-language line for each and the full
payload, hashes and signature on request. Other entries (ballots, role and
configuration changes) stay in the chain but are not listed. The result of
verifying the whole chain (hashes, links, signatures) is shown on every load. Every
signed-in member may see it, not only admins: the log exists so members don't
have to trust the admins. It holds no secrets (invite codes and notes are never
logged).

Verifying also recounts every closed vote from the log (`app.chain_verify`).
Members can run the same check again with **Verify chain** (`/api/log/verify`,
an HTML fragment for htmx, JSON otherwise), and download the whole log
(`/api/log/export`, JSON Lines) and the public keys (`/api/log/keys`) to check
it on their own computer with `scripts/verify_log.py`. All of it is for signed-in
members only, like the page.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from app import anchors, decision_log
from app.auth import DB, SignedIn
from app.calendar import member_name
from app.i18n import en
from app.models import Bookable, DecisionLog, Member, SigningKey

router = APIRouter()

PAGE_SIZE = 50
LISTED_KINDS = ("proposal.opened", "proposal.closed", "proposal.cancelled")
EXPORT_FILE = "commons-decision-log.jsonl"
KEYS_FILE = "commons-log-keys.json"


@dataclass(frozen=True)
class Row:
    entry: DecisionLog
    actor: str
    summary: str
    payload: str
    source: str  # web / matrix / community.yaml / auto (no actor) / blank
    day: str  # heading of the local day the entry belongs to

    @property
    def short_hash(self) -> str:
        return short_hash(self.entry.entry_hash)


def short_hash(digest: str) -> str:
    """The first 4 and last 3 hex digits, as on the board: '9c1e…a07'."""
    return f"{digest[:4]}\u2026{digest[-3:]}" if digest else ""


def source_of(entry: DecisionLog) -> str:
    payload = entry.payload_json
    channel = payload.get("channel") or payload.get("source")
    if isinstance(channel, str) and channel:
        return channel
    return en.LOG_SOURCE_AUTO if entry.actor_member_id is None else ""


def day_heading(ts: datetime, tz: ZoneInfo, today: datetime) -> str:
    """'Today · Thu 12 Nov', 'Yesterday · Wed 11 Nov' or 'Tue 3 Nov 2026', local time."""
    day = ts.astimezone(tz).date()
    label = f"{day:%a} {day.day} {day:%b}"
    if day == today.date():
        return en.LOG_TODAY.format(day=label)
    if day == (today - timedelta(days=1)).date():
        return en.LOG_YESTERDAY.format(day=label)
    return f"{label} {day.year}"


def actor_name(member: Member | None) -> str:
    if member is None:
        return en.LOG_SYSTEM
    return member_name(member)


@dataclass(frozen=True)
class Names:
    """Today's names for the members and things the entries on a page refer to.
    Display only: the entries keep the ids they were signed with."""

    by_matrix_id: Mapping[str, str] = field(default_factory=dict)
    by_member_id: Mapping[int, str] = field(default_factory=dict)
    things: Mapping[str, str] = field(default_factory=dict)

    def member(self, payload: Mapping[str, Any]) -> str | None:
        mxid, member_id = payload.get("matrix_user_id"), payload.get("member_id")
        if isinstance(mxid, str) and mxid in self.by_matrix_id:
            return self.by_matrix_id[mxid]
        if isinstance(member_id, int) and member_id in self.by_member_id:
            return self.by_member_id[member_id]
        if isinstance(mxid, str):
            return mxid.removeprefix("@").split(":", 1)[0]  # like member_name
        return None


def _field_name(key: str) -> str:
    if key in en.LOG_FIELD_NAMES:
        return en.LOG_FIELD_NAMES[key]
    for fields in (en.RESOURCES_FIELDS, en.BOOKABLE_FIELDS):
        if key in fields:
            return fields[key][0].lower()
    return key


def _config_line(payload: Mapping[str, Any]) -> str | None:
    """A resource or thing added, deleted, archived, restored or changed; else None."""
    key = payload.get("key")
    if not isinstance(key, str) or "." not in key:
        return None
    group, slug = key.split(".", 1)
    lines = en.LOG_CONFIG.get(group)
    old, new = payload.get("old"), payload.get("new")
    if lines is None or not isinstance(old, dict | None) or not isinstance(new, dict | None):
        return None
    if old is None and new is None:
        return None
    name = str((new or old).get("name") or slug)
    if old is None:
        return lines["added"].format(name=name)
    if new is None:
        return lines["deleted"].format(name=name)
    if old.get("archived") != new.get("archived"):
        return lines["archived" if new.get("archived") else "restored"].format(name=name)
    changed = [k for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)]
    fields = ", ".join(_field_name(k) for k in changed) or "-"
    return lines["changed"].format(name=name, fields=fields)


def summarize(kind: str, payload: Mapping[str, Any], names: Names | None = None) -> str:
    """One line for an entry. Payload values come from the log: shown escaped, never trusted."""
    names = names or Names()
    if kind == "config.changed":
        line = _config_line(payload)
        if line is not None:
            return line
    template = en.LOG_KIND.get(kind)
    if kind == "proposal.closed" and payload.get("outcome_label"):
        template = en.LOG_CLOSED_WITH_WINNER
    if template is None:
        return kind
    extra: dict[str, Any] = {}
    who = names.member(payload)
    if who is not None:
        extra["who"] = who
    thing = payload.get("bookable")
    if isinstance(thing, str):
        extra["thing"] = names.things.get(thing, thing)
    try:
        return template.format(**{**payload, **extra})
    except (KeyError, IndexError, ValueError, AttributeError):
        return kind


def _names(db, entries: list[DecisionLog]) -> Names:
    """Names for the members and things the payloads on this page mention."""
    mxids, ids, slugs = set(), set(), set()
    for e in entries:
        p = e.payload_json if isinstance(e.payload_json, dict) else {}
        if isinstance(p.get("matrix_user_id"), str):
            mxids.add(p["matrix_user_id"])
        if isinstance(p.get("member_id"), int):
            ids.add(p["member_id"])
        if isinstance(p.get("bookable"), str):
            slugs.add(p["bookable"])
    found = (
        list(
            db.scalars(select(Member).where(Member.matrix_user_id.in_(mxids) | Member.id.in_(ids)))
        )
        if mxids or ids
        else []
    )
    things = list(db.scalars(select(Bookable).where(Bookable.slug.in_(slugs)))) if slugs else []
    return Names(
        by_matrix_id={m.matrix_user_id: member_name(m) for m in found},
        by_member_id={m.id: member_name(m) for m in found},
        things={t.slug: t.name for t in things},
    )


@router.get("/log")
def log_page(
    request: Request,
    db: DB,
    current: SignedIn,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """Member: read the votes in the decision log and see whether its chain verifies."""
    query = (
        select(DecisionLog)
        .where(DecisionLog.kind.in_(LISTED_KINDS))
        .order_by(DecisionLog.seq.desc())
        .limit(PAGE_SIZE + 1)
    )
    if before is not None:
        query = query.where(DecisionLog.seq < before)
    entries = list(db.scalars(query))
    entries, more = entries[:PAGE_SIZE], len(entries) > PAGE_SIZE
    older = entries[-1].seq if more else None  # next page: entries before this seq

    actor_ids = {e.actor_member_id for e in entries if e.actor_member_id is not None}
    actors = (
        {m.id: m for m in db.scalars(select(Member).where(Member.id.in_(actor_ids)))}
        if actor_ids
        else {}
    )
    names = _names(db, entries)
    tz = ZoneInfo(request.app.state.settings.timezone)
    today = datetime.now(tz)
    rows = [
        Row(
            entry=e,
            actor=actor_name(actors.get(e.actor_member_id)),
            summary=summarize(e.kind, e.payload_json, names),
            payload=json.dumps(e.payload_json, indent=2, sort_keys=True, ensure_ascii=False),
            source=source_of(e),
            day=day_heading(e.ts, tz, today),
        )
        for e in entries
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "log.html",
        {
            "title": en.LOG_TITLE,
            "nav": "log",
            "current": current,
            "rows": rows,
            "older": older,
            "first_page": before is None,
            "export_file": EXPORT_FILE,
            "anchor": last_anchor(db),
            "keys_file": KEYS_FILE,
            **_verification(db),
        },
    )


def _verification(db) -> dict[str, Any]:
    return {
        "result": decision_log.verify_db(db),
        "keys": list(db.scalars(select(SigningKey).order_by(SigningKey.created_at))),
    }


def last_anchor(db) -> dict[str, Any] | None:
    """The newest checkpoint posted to the community room (app/anchors.py)."""
    row = db.scalar(
        select(DecisionLog)
        .where(DecisionLog.kind == anchors.ANCHORED)
        .order_by(DecisionLog.seq.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {"seq": row.payload_json["anchored_seq"], "hash": row.payload_json["anchored_hash"]}


@router.get("/api/log/verify")
def verify(request: Request, db: DB, current: SignedIn) -> Response:
    """Member: verify the whole chain again, with the tally replay.

    htmx (the Verify chain button) gets the result box; anything else gets JSON.
    """
    if request.headers.get("HX-Request") == "true":
        return request.app.state.templates.TemplateResponse(
            request, "_log_verify.html", {"current": current, **_verification(db)}
        )
    result = decision_log.verify_db(db)
    return JSONResponse(
        {
            "ok": result.ok,
            "entries": result.head_seq,
            "head_seq": result.head_seq,
            "head_hash": result.head_hash,
            "tallies_recounted": result.tallies,
            "failed_seq": result.failed_seq,
            "reason": result.reason,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/log/export")
def export(db: DB, current: SignedIn) -> Response:
    """Member: the whole log as JSON Lines (every entry, with hashes and signatures)."""
    # Built in full while the request's session is open: the log is small.
    return Response(
        "".join(decision_log.export_lines(db)),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{EXPORT_FILE}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/log/keys")
def keys(db: DB, current: SignedIn) -> Response:
    """Member: the public keys the log is signed with (key id -> raw Ed25519 hex)."""
    return JSONResponse(
        decision_log.public_keys(db),
        headers={
            "Content-Disposition": f'attachment; filename="{KEYS_FILE}"',
            "Cache-Control": "no-store",
        },
    )
