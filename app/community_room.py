"""Which Matrix room is the COMMONS community room (D6).

An admin writes "COMMONS, use this room for updates" in the room. The choice
is community configuration: stored in `config_kv`, signed into the decision
log as `config.changed` in the same transaction, and announced on the event
bus so the bot starts posting there at once. Rules only, no model: a setting
shouldn't depend on how a model reads a sentence.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app import decision_log
from app.events import Event, publish
from app.i18n import en
from app.models import ConfigKV, Member, utcnow
from app.stream import ROOM_KEY, ROOM_SET

# "COMMONS, use this room for updates", "@commons: make this the community room".
_ADDRESS = r"(?:@?commons\b[^\s]*\s*[,:]?\s*)?"
_SET_ROOM = re.compile(
    rf"^{_ADDRESS}(?:please\s+)?(?:"
    r"use\s+this\s+room\s+for\s+(?:the\s+)?(?:commons\s+)?(?:updates|news)"
    r"|make\s+this\s+(?:room\s+)?the\s+(?:commons\s+|community\s+)?(?:room|updates\s+room)"
    r"|post\s+(?:the\s+)?(?:commons\s+)?updates\s+(?:here|in\s+this\s+room)"
    r")(?:\s+please)?\s*[.!]*$",
    re.IGNORECASE,
)


def asks_to_set_room(text: str) -> bool:
    return bool(_SET_ROOM.match(" ".join(text.split())))


def current(db: Session) -> str | None:
    row = db.get(ConfigKV, ROOM_KEY)
    return None if row is None else row.value_json


def set_room(
    db: Session, signer: decision_log.Signer, member: Member, room_id: str, *, channel: str
) -> str:
    """Make `room_id` the community room as `member` (admins only); returns the reply."""
    if not member.is_active or "admin" not in member.role_names:
        return en.ROOM_ADMINS_ONLY
    row = db.get(ConfigKV, ROOM_KEY)
    old = None if row is None else row.value_json
    if old == room_id:
        return en.ROOM_ALREADY
    now = utcnow()
    if row is None:
        db.add(ConfigKV(key=ROOM_KEY, value_json=room_id, updated_at=now, updated_by=member.id))
    else:
        row.value_json, row.updated_at, row.updated_by = room_id, now, member.id
    decision_log.append(
        db,
        signer,
        "config.changed",
        {"key": ROOM_KEY, "old": old, "new": room_id, "source": channel},
        actor=member.id,
    )
    publish(db, Event(ROOM_SET, {"room_id": room_id}))
    db.commit()
    return en.ROOM_SET
