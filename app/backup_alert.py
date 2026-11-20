"""A failed nightly backup, told to every admin.

scripts/backup.py runs on the host. When a backup fails it runs

    docker compose exec -T app python -m app.backup_alert <reason>

which posts the reason to /internal/backup-failed with the service token, so the
running app writes one private feed item per active admin and the bot sends it
to them (bot/dm.py), like a give-back reminder. Only a fixed reason word crosses
over, never a path or raw output; the details stay in the host's journal.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import Event, publish
from app.models import Member, MemberRole

KIND = "backup.failed"
# Same words as REASONS in scripts/backup.py (tests/test_backup.py checks they match).
REASONS = ("config", "database", "files", "encrypt", "write", "other")
APP_URL = "http://127.0.0.1:8000/internal/backup-failed"


def notify_failed(db: Session, reason: str) -> int:
    """Queue one private message per active admin (the caller commits); returns how many."""
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    admins = db.scalars(
        select(Member)
        .join(MemberRole, MemberRole.member_id == Member.id)
        .where(MemberRole.role == "admin", Member.deactivated_at.is_(None))
        .order_by(Member.id)
    ).all()
    for m in admins:
        publish(db, Event(KIND, {"member_id": m.id, "reason": reason}))
    return len(admins)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in REASONS:
        print(f"usage: python -m app.backup_alert {{{','.join(REASONS)}}}", file=sys.stderr)
        return 2
    from app.config import read_secret

    request = urllib.request.Request(
        APP_URL,
        data=json.dumps({"reason": args[0]}).encode(),
        headers={
            "Authorization": f"Bearer {read_secret('internal_api_token')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as r:  # noqa: S310  # nosec B310
        admins = json.load(r).get("admins", 0)
    print(f"ok: told {admins} admin(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
