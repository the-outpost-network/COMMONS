"""A failed backup reaches every admin privately; `app.cli verify-log` after a restore."""

from __future__ import annotations

import anyio
import pytest
from sqlalchemy import select
from tests.conftest import INTERNAL_TOKEN, SERVER

from app import backup_alert, cli, decision_log, members
from app.i18n import en
from app.models import EventFeed
from bot.dm import DM_KINDS, DirectMessages

ADA = f"@ada:{SERVER}"
FRIDA = f"@frida:{SERVER}"
OLD = f"@old:{SERVER}"
NORA = f"@nora:{SERVER}"
AUTH = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


@pytest.fixture
def admins(db, log_signer):
    log = {"signer": log_signer, "channel": "cli"}
    ada = members.add_member(db, ADA, "Ada", ("admin",), **log)
    frida = members.add_member(db, FRIDA, "Frida", ("admin",), **log)
    old = members.add_member(db, OLD, "Old", ("admin",), **log)
    members.add_member(db, NORA, "Nora")
    members.deactivate(db, old, signer=log_signer, channel="cli")
    db.commit()
    return ada, frida


def feed_rows(db) -> list[EventFeed]:
    db.expire_all()
    return list(db.scalars(select(EventFeed).where(EventFeed.kind == backup_alert.KIND)))


def test_every_active_admin_gets_a_private_message(client, db, admins):
    r = client.post("/internal/backup-failed", json={"reason": "config"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"admins": 2}
    rows = feed_rows(db)
    assert sorted(row.data["to"] for row in rows) == [ADA, FRIDA]  # not Nora, not Old
    reason = en.BACKUP_FAILED_REASONS["config"]
    assert all(row.summary == en.BACKUP_FAILED.format(reason=reason) for row in rows)
    assert all(row.member_id is None for row in rows)


def test_only_known_reasons(client, db, admins):
    r = client.post("/internal/backup-failed", json={"reason": "/etc/shadow missing"}, headers=AUTH)
    assert r.status_code == 400  # the app answers validation errors with 400
    assert feed_rows(db) == []


def test_needs_the_service_token(client, db, admins):
    r = client.post("/internal/backup-failed", json={"reason": "config"})
    assert r.status_code == 401
    r = client.post(
        "/internal/backup-failed",
        json={"reason": "config"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401
    assert feed_rows(db) == []


def test_every_reason_has_words():
    assert set(en.BACKUP_FAILED_REASONS) == set(backup_alert.REASONS)


def test_alert_command_refuses_unknown_reasons(capsys):
    assert backup_alert.main(["oops"]) == 2
    assert backup_alert.main([]) == 2
    assert "usage" in capsys.readouterr().err


class FakeMatrix:
    bot_user_id = f"@commons:{SERVER}"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def joined_rooms(self) -> list[str]:
        return ["!dm:x"]

    async def members(self, room_id: str) -> set[str]:
        return {self.bot_user_id, ADA}

    async def create_direct(self, user_id: str) -> str:  # pragma: no cover
        raise AssertionError("has a room already")

    async def send_text(self, room_id: str, text: str) -> None:
        self.sent.append((room_id, text))


def test_the_bot_sends_it_privately_never_to_the_room():
    assert "backup.failed" in DM_KINDS
    m = FakeMatrix()
    dm = DirectMessages(m, server_name=SERVER, community_room=lambda: "!village:x")
    item = {"id": 3, "kind": "backup.failed", "summary": "Last night's backup failed", "to": ADA}
    assert anyio.run(dm.on_feed, item) is True
    assert m.sent == [("!dm:x", "Last night's backup failed")]


# --- verify-log -----------------------------------------------------------------------


def run_cli(app, *argv: str) -> int:
    return cli.main(
        list(argv),
        sessionmaker=app.state.sessionmaker,
        server_name=SERVER,
        key_dir=app.state.settings.key_dir,
    )


def test_verify_log_reports_the_head(app, admins, capsys):
    assert run_cli(app, "verify-log") == 0
    out = capsys.readouterr().out
    assert out.startswith("ok: ")
    assert "entries verify; head " in out


def test_verify_log_fails_on_a_broken_chain(app, admins, capsys, monkeypatch):
    broken = decision_log.VerifyResult(False, 2, "0" * 64, 3, "signature is not valid")
    monkeypatch.setattr(decision_log, "verify_db", lambda db: broken)
    assert run_cli(app, "verify-log") == 1
    assert "does not verify at entry 3: signature is not valid" in capsys.readouterr().err
