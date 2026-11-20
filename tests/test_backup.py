"""scripts/backup.py without Docker: naming, retention, refusals, encryption round trip.

A stub `docker` on PATH stands in for `docker compose`: it prints fake dumps and
volume tars and records every call. `age` is the real tool (CI installs it).
The restore into a real stack is a manual drill (docs/BACKUP.md); the database
half of it runs in tests/test_backup_restore_db.py.
"""

# ruff: noqa: S603, S607 - fixed commands (age, docker) the tests run

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests import backup_script

from app import backup_alert

ROOT = Path(__file__).resolve().parents[1]
backup = backup_script.load()

NOW = datetime(2026, 11, 15, 2, 15, 0, tzinfo=UTC)
SECRET_VALUE = "very-secret-database-password-1234"

needs_age = pytest.mark.skipif(
    shutil.which("age") is None and not os.environ.get("CI"),
    reason="age is not installed (brew install age / apt install age)",
)

STUB = r"""#!{python}
import os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
fail = os.environ.get("STUB_FAIL", "")
if args[:1] == ["info"]:
    print(os.environ.get("STUB_DOCKER_ROOT", ""))
    sys.exit(0)
assert args[0] == "compose", args
args = args[1:]
if "pg_dump" in args:
    if fail == "pg_dump":
        sys.exit(1)
    sys.stdout.write("PGDMP fake dump of " + args[-1])
elif args[:1] == ["run"] and "tar" in args:
    if fail == "tar":
        sys.exit(2)
    sys.stdout.write("fake tar of " + args[args.index("tar") + 1])
elif args[:1] == ["ps"]:
    print("0123abcd" if os.environ.get("STUB_BOT_RUNNING") else "")
elif "app.backup_alert" in args:
    if fail == "alert":
        sys.exit(1)
"""


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture
def keypair(tmp_path: Path) -> tuple[Path, Path]:
    """An age identity (private, stays off the box) and a recipients file (public)."""
    if shutil.which("age-keygen") is None:
        pytest.skip("age is not installed")
    identity = tmp_path / "identity.key"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    public = subprocess.run(
        ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
    ).stdout.strip()
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(f"# board key\n{public}\n")
    return identity, recipients


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A fake checkout with secrets/, community.yaml and .env."""
    root = tmp_path / "checkout"
    (root / "secrets" / "matrix").mkdir(parents=True)
    (root / "secrets" / "database_password").write_text(SECRET_VALUE)
    (root / "secrets" / "matrix" / "mas.yaml").write_text("secrets: {}\n")
    (root / "community.yaml").write_text("name: Outpost\n")
    (root / ".env").write_text("COMMONS_HOSTS=commons.example.test\n")
    return root


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake docker first on PATH; returns its call log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(STUB.replace("{python}", sys.executable))
    docker.chmod(0o755)
    calls = tmp_path / "docker-calls.log"
    calls.touch()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(calls))
    monkeypatch.delenv("STUB_FAIL", raising=False)
    return calls


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "usb"
    d.mkdir()
    return d


@pytest.fixture
def stage_tmp(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


def run_backup(checkout: Path, out_dir: Path, recipients: Path, stage_tmp: Path, **kw):
    return backup.backup(
        checkout,
        out_dir,
        recipients,
        allow_same_device=True,  # tmp_path is one disk
        now=kw.pop("now", NOW),
        tmp_dir=stage_tmp,
        **kw,
    )


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines()


# --- names and retention --------------------------------------------------------------


def test_names_carry_only_the_time():
    name = backup.backup_name(NOW)
    assert name == "commons-20261115T021500Z.tar.gz.age"
    assert backup.NAME_RE.match(name)
    vienna = NOW.astimezone(datetime.now().astimezone().tzinfo)
    assert backup.backup_name(vienna) == name  # always UTC


def test_retention_keeps_the_newest_14_and_ignores_other_files():
    names = [backup.backup_name(NOW - timedelta(days=d)) for d in range(20)]
    names += ["notes.txt", "commons-old.tar", names[0] + ".partial"]
    gone = backup.to_prune(names, 14)
    assert len(gone) == 6
    assert set(gone) == {backup.backup_name(NOW - timedelta(days=d)) for d in range(14, 20)}
    assert backup.to_prune(names[:3], 14) == []
    with pytest.raises(ValueError, match="at least one"):
        backup.to_prune(names, 0)


# --- refusals -------------------------------------------------------------------------


def test_refuses_without_a_recipients_file(tmp_path: Path):
    with pytest.raises(backup.BackupError, match="not set") as e:
        backup.read_recipients(None)
    assert e.value.reason == "config"
    with pytest.raises(backup.BackupError, match="cannot read"):
        backup.read_recipients(tmp_path / "missing.txt")


def test_refuses_a_recipients_file_without_keys(tmp_path: Path):
    f = tmp_path / "r.txt"
    f.write_text("# nobody yet\n\n")
    with pytest.raises(backup.BackupError, match="no age public key"):
        backup.read_recipients(f)
    f.write_text("ssh-ed25519 AAAA nora@laptop\n")
    with pytest.raises(backup.BackupError, match="line 1 is not an age public key"):
        backup.read_recipients(f)


def test_refuses_a_private_key_and_never_prints_it(tmp_path: Path):
    f = tmp_path / "r.txt"
    secret = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"
    f.write_text(f"{secret}\n")
    with pytest.raises(backup.BackupError, match="private key") as e:
        backup.read_recipients(f)
    assert secret not in str(e.value)


def test_accepts_age_public_keys(tmp_path: Path):
    key = "age1" + "q" * 58
    f = tmp_path / "r.txt"
    f.write_text(f"# two holders\n{key}\n  {key[:-1]}p  \n")
    assert backup.read_recipients(f) == [key, key[:-1] + "p"]


def test_refuses_a_missing_or_same_disk_destination(tmp_path: Path, checkout: Path):
    with pytest.raises(backup.BackupError, match="not set"):
        backup.check_destination(None, [checkout], False)
    with pytest.raises(backup.BackupError, match="does not exist"):
        backup.check_destination(tmp_path / "unmounted", [checkout], False)
    usb = tmp_path / "usb"
    usb.mkdir()
    # Same disk as the checkout: an unmounted USB mount point looks exactly like this.
    with pytest.raises(backup.BackupError, match="same disk") as e:
        backup.check_destination(usb, [checkout], False)
    assert e.value.reason == "config"
    assert backup.check_destination(usb, [checkout], True) == usb


def test_restore_refuses_a_checkout_that_has_secrets(checkout: Path, tmp_path: Path):
    with pytest.raises(backup.BackupError, match="fresh clone"):
        backup.restore(checkout, tmp_path / "x.age", tmp_path / "id.key")


def test_restore_refuses_leftover_config(tmp_path: Path):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "community.yaml").write_text("x: 1\n")
    with pytest.raises(backup.BackupError, match=r"community\.yaml exists"):
        backup.restore(root, tmp_path / "x.age", tmp_path / "id.key")


# --- a whole backup -------------------------------------------------------------------


@needs_age
def test_backup_writes_one_encrypted_0600_file_and_cleans_up(
    checkout, out_dir, keypair, stub, stage_tmp, tmp_path, monkeypatch
):
    identity, recipients = keypair
    monkeypatch.setenv("STUB_BOT_RUNNING", "1")
    target = run_backup(checkout, out_dir, recipients, stage_tmp)

    assert [p.name for p in out_dir.iterdir()] == [target.name]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes().startswith(b"age-encryption.org/v1")
    assert SECRET_VALUE.encode() not in target.read_bytes()
    assert list(stage_tmp.iterdir()) == []  # no plaintext left behind

    log = calls(stub)
    for db in backup.DATABASES:
        dump = f"compose exec -T postgres pg_dump -U matrix --format=custom --no-password {db}"
        assert dump in log
    # The bot is stopped while its crypto store is copied, and started again.
    i = log.index("compose stop bot")
    assert "--entrypoint tar bot -C /data -cf - ." in log[i + 1]
    assert log[i + 2] == "compose start bot"

    restored = tmp_path / "restored"
    restored.mkdir()
    manifest = backup.decrypt_and_extract(target, identity, restored)
    assert manifest["format"] == backup.FORMAT
    assert manifest["created"] == "2026-11-15T02:15:00Z"
    assert set(manifest["parts"]) == set(backup.PARTS)
    assert (restored / "commons.dump").read_text() == "PGDMP fake dump of commons"
    assert (restored / "bot-data.tar").read_text() == "fake tar of bot"
    with tarfile.open(restored / "host-files.tar") as tar:
        names = set(tar.getnames())
        assert {"secrets/database_password", "secrets/matrix/mas.yaml"} <= names
        assert {"community.yaml", ".env"} <= names
        f = tar.extractfile("secrets/database_password")
        assert f is not None
        assert f.read().decode() == SECRET_VALUE


@needs_age
def test_backup_leaves_a_stopped_bot_stopped(checkout, out_dir, keypair, stub, stage_tmp):
    run_backup(checkout, out_dir, keypair[1], stage_tmp)
    log = calls(stub)
    assert "compose stop bot" not in log
    assert "compose start bot" not in log


@needs_age
def test_backup_prunes_beyond_keep_only_after_writing(checkout, out_dir, keypair, stub, stage_tmp):
    old = [backup.backup_name(NOW - timedelta(days=d)) for d in range(1, 16)]
    for name in old:
        (out_dir / name).write_bytes(b"old")
    (out_dir / "README.txt").write_text("the COMMONS backup disk")
    target = run_backup(checkout, out_dir, keypair[1], stage_tmp, keep=14)
    left = sorted(p.name for p in out_dir.iterdir())
    assert target.name in left
    assert "README.txt" in left
    assert len([n for n in left if backup.NAME_RE.match(n)]) == 14
    assert old[-1] not in left
    assert old[-2] not in left


@needs_age
def test_a_failed_dump_writes_nothing_and_prunes_nothing(
    checkout, out_dir, keypair, stub, stage_tmp, monkeypatch
):
    old = backup.backup_name(NOW - timedelta(days=30))
    (out_dir / old).write_bytes(b"old")
    monkeypatch.setenv("STUB_FAIL", "pg_dump")
    with pytest.raises(backup.BackupError) as e:
        run_backup(checkout, out_dir, keypair[1], stage_tmp, keep=1)
    assert e.value.reason == "database"
    assert [p.name for p in out_dir.iterdir()] == [old]
    assert list(stage_tmp.iterdir()) == []


@needs_age
def test_never_overwrites_a_backup(checkout, out_dir, keypair, stub, stage_tmp):
    run_backup(checkout, out_dir, keypair[1], stage_tmp)
    with pytest.raises(backup.BackupError, match="already exists") as e:
        run_backup(checkout, out_dir, keypair[1], stage_tmp)
    assert e.value.reason == "write"


# --- the command line and the admins' alert -------------------------------------------


def _env(monkeypatch, checkout, out_dir, recipients, stage_tmp):
    monkeypatch.setenv("COMMONS_ROOT", str(checkout))
    monkeypatch.setenv("COMMONS_BACKUP_DIR", str(out_dir))
    monkeypatch.setenv("COMMONS_BACKUP_TMPDIR", str(stage_tmp))
    if recipients is None:
        monkeypatch.delenv("COMMONS_BACKUP_RECIPIENTS", raising=False)
    else:
        monkeypatch.setenv("COMMONS_BACKUP_RECIPIENTS", str(recipients))


def test_cli_without_recipients_refuses_and_tells_the_admins(
    checkout, out_dir, stub, stage_tmp, monkeypatch, capsys
):
    _env(monkeypatch, checkout, out_dir, None, stage_tmp)
    assert backup.main(["backup", "--allow-same-device"]) == 1
    log = calls(stub)
    assert not any("pg_dump" in c for c in log)  # refused before touching the stack
    assert log[-1] == "compose exec -T app python -m app.backup_alert config"
    assert "failed (config)" in capsys.readouterr().out
    assert list(out_dir.iterdir()) == []


@needs_age
def test_cli_failure_alert_and_log_carry_no_secrets(
    checkout, out_dir, keypair, stub, stage_tmp, monkeypatch, capsys
):
    _env(monkeypatch, checkout, out_dir, keypair[1], stage_tmp)
    monkeypatch.setenv("STUB_FAIL", "tar")
    assert backup.main(["backup", "--allow-same-device"]) == 1
    out = capsys.readouterr().out
    assert "failed (files)" in out
    assert SECRET_VALUE not in out
    assert calls(stub)[-1].endswith("app.backup_alert files")


@needs_age
def test_cli_success_logs_name_size_and_hash_only(
    checkout, out_dir, keypair, stub, stage_tmp, monkeypatch, capsys
):
    _env(monkeypatch, checkout, out_dir, keypair[1], stage_tmp)
    assert backup.main(["backup", "--allow-same-device"]) == 0
    out = capsys.readouterr().out
    assert "wrote commons-" in out
    assert "sha256" in out
    assert SECRET_VALUE not in out
    assert not any("backup_alert" in c for c in calls(stub))


def test_cli_alert_failure_is_logged_not_raised(
    checkout, out_dir, stub, stage_tmp, monkeypatch, capsys
):
    _env(monkeypatch, checkout, out_dir, None, stage_tmp)
    monkeypatch.setenv("STUB_FAIL", "alert")
    assert backup.main(["backup"]) == 1
    assert "could not tell the admins" in capsys.readouterr().out


def test_reasons_match_the_apps():
    assert backup.REASONS == backup_alert.REASONS


# --- decrypting and checking ----------------------------------------------------------


def _encrypt_tar(recipients: Path, files: dict[str, bytes], target: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    subprocess.run(
        ["age", "-e", "-R", str(recipients), "-o", str(target)], input=buf.getvalue(), check=True
    )


def _good_parts() -> dict[str, bytes]:
    parts = {name: f"content of {name}".encode() for name in backup.PARTS}
    manifest = {
        "format": 1,
        "created": "2026-11-15T02:15:00Z",
        "commit": None,
        "parts": {
            n: {"sha256": __import__("hashlib").sha256(d).hexdigest(), "bytes": len(d)}
            for n, d in parts.items()
        },
    }
    return {backup.MANIFEST: json.dumps(manifest).encode(), **parts}


@needs_age
def test_extract_accepts_a_good_archive(keypair, tmp_path):
    identity, recipients = keypair
    archive = tmp_path / "good.age"
    _encrypt_tar(recipients, _good_parts(), archive)
    dest = tmp_path / "out"
    dest.mkdir()
    assert backup.decrypt_and_extract(archive, identity, dest)["format"] == 1


@needs_age
def test_extract_refuses_a_part_that_does_not_match_the_manifest(keypair, tmp_path):
    identity, recipients = keypair
    files = _good_parts()
    files["commons.dump"] = b"swapped database"
    archive = tmp_path / "tampered.age"
    _encrypt_tar(recipients, files, archive)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(backup.BackupError, match=r"commons\.dump does not match"):
        backup.decrypt_and_extract(archive, identity, dest)


@needs_age
@pytest.mark.parametrize("name", ["../escape", "secrets/extra", "evil.sh"])
def test_extract_refuses_unexpected_entries(keypair, tmp_path, name):
    identity, recipients = keypair
    archive = tmp_path / "odd.age"
    _encrypt_tar(recipients, {**_good_parts(), name: b"x"}, archive)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(backup.BackupError, match="unexpected entry"):
        backup.decrypt_and_extract(archive, identity, dest)
    assert not (tmp_path / "escape").exists()


@needs_age
def test_extract_refuses_the_wrong_key(keypair, tmp_path):
    _, recipients = keypair
    archive = tmp_path / "good.age"
    _encrypt_tar(recipients, _good_parts(), archive)
    other = tmp_path / "other.key"
    subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(backup.BackupError, match="wrong key") as e:
        backup.decrypt_and_extract(archive, other, dest)
    assert e.value.reason == "encrypt"


# --- the script matches the stack -----------------------------------------------------


def test_every_backed_up_volume_is_mounted_at_data_by_its_service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for v in backup.VOLUMES:
        mounts = compose["services"][v.service]["volumes"]
        assert f"{v.part}:/data" in mounts, v
        assert v.path.startswith("/data"), v
    # Every named volume is backed up, except PostgreSQL (dumped instead) and Caddy's
    # autosaved config (written again from the Caddyfile on start).
    named = set(compose["volumes"])
    assert named - {v.part for v in backup.VOLUMES} == {"postgres-data", "caddy-config"}


def test_databases_match_the_init_scripts():
    init = (ROOT / "deploy/synapse/postgres-init-1.sql").read_text()
    init += (ROOT / "deploy/synapse/postgres-init-commons.sh").read_text()
    for db in backup.DATABASES:
        assert f"CREATE DATABASE {db}" in init
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["postgres"]["environment"]["POSTGRES_USER"] == backup.PG_USER


def test_wrapper_scripts_are_executable():
    for name in ("backup.sh", "restore.sh", "backup.py"):
        assert os.access(ROOT / "scripts" / name, os.X_OK), name


def test_restore_refuses_a_compose_project_that_is_in_use(tmp_path, stub, monkeypatch):
    """Same host as the running box without COMPOSE_PROJECT_NAME: never touch its containers."""
    root = tmp_path / "fresh"
    root.mkdir()
    monkeypatch.setenv("STUB_BOT_RUNNING", "1")  # `compose ps` lists a container
    with pytest.raises(backup.BackupError, match="COMPOSE_PROJECT_NAME"):
        backup.restore(root, tmp_path / "x.age", tmp_path / "id.key")
    assert calls(stub) == ["compose ps -a -q"]
