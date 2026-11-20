#!/usr/bin/env python3
"""Encrypted backups of the COMMONS box and restoring them into a fresh stack.

    scripts/backup.sh                                   # nightly (systemd timer)
    scripts/restore.sh FILE --identity KEYFILE          # on a fresh clone
    python3 scripts/backup.py extract FILE --identity KEYFILE --to DIR

One backup is one file, `commons-YYYYMMDDTHHMMSSZ.tar.gz.age`: a gzip'd tar of

- `commons.dump`, `synapse.dump`, `mas.dump`: `pg_dump -Fc` of the three databases,
- `app-data.tar`: the app volume, with the decision-log signing key (data/keys),
- `synapse-data.tar`: homeserver media,
- `bot-data.tar`: the bot's crypto store and cross-signing recovery key,
- `caddy-data.tar`: TLS certificates and, in `tls internal` mode, the local CA,
- `host-files.tar`: `secrets/` (Matrix, MAS and database keys and passwords),
  `community.yaml` and `.env`, without which the dumps can't be used,
- `manifest.json`: format, time, git commit and the SHA-256 of every part,

encrypted with `age` to the public keys in COMMONS_BACKUP_RECIPIENTS. The private
keys never live on the box. Files are written 0600 to COMMONS_BACKUP_DIR, which
must be on another device than the stack (the USB disk); the newest
COMMONS_BACKUP_KEEP (14) are kept. A failed backup is logged and sent to every
admin as a private message by the bot (app/backup_alert.py).

Standard library only: it runs on the host, next to `docker compose`.
docs/BACKUP.md has the setup and a step-by-step restore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FORMAT = 1
PREFIX = "commons-"
SUFFIX = ".tar.gz.age"
NAME_RE = re.compile(r"^commons-(\d{8}T\d{6}Z)\.tar\.gz\.age$")
PARTIAL_SUFFIX = ".partial"
DEFAULT_KEEP = 14

PG_USER = "matrix"  # POSTGRES_USER in docker-compose.yml: owns synapse and mas
DATABASES = ("commons", "synapse", "mas")
# Restore into the empty database the init scripts create; any error aborts and
# rolls back, so a half-restored database never starts.
PG_DUMP_ARGS = ("--format=custom", "--no-password")
PG_RESTORE_ARGS = ("--exit-on-error", "--single-transaction", "--no-password")


@dataclass(frozen=True)
class Volume:
    part: str  # file name in the archive, without .tar
    service: str  # compose service that mounts it at /data
    stop: bool = False  # stop the service while copying (live SQLite files)
    # What to copy: Caddy's /data belongs to root, only /data/caddy to its user.
    path: str = "/data"


VOLUMES = (
    Volume("app-data", "app"),
    Volume("synapse-data", "matrix"),
    Volume("bot-data", "bot", stop=True),
    # Certificates and, in `tls internal` mode, the local CA members' phones trust
    # (docs/SECURITY.md): losing it means every member installs a new one.
    Volume("caddy-data", "caddy", path="/data/caddy"),
)
# Paths relative to the checkout; secrets/ is required, the others are copied if present.
HOST_FILES = ("secrets", "community.yaml", ".env")
PARTS = (
    *(f"{db}.dump" for db in DATABASES),
    *(f"{v.part}.tar" for v in VOLUMES),
    "host-files.tar",
)
MANIFEST = "manifest.json"

# Why a backup failed: a fixed word for logs and the admins' message, never raw output.
REASONS = ("config", "database", "files", "encrypt", "write", "other")

RECIPIENT_RE = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
SECRET_MARK = "AGE-SECRET-KEY-"  # noqa: S105  # nosec B105 - the marker we refuse


class BackupError(Exception):
    """A refusal or failure; `reason` is one of REASONS."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def log(message: str) -> None:
    print(f"commons-backup: {message}", flush=True)


def tool(name: str) -> str:
    """Absolute path of a program on PATH (docker, age, git)."""
    path = shutil.which(name)
    if path is None:
        raise BackupError("encrypt" if name == "age" else "config", f"{name} is not installed")
    return path


def _run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    # Fixed argument lists built in this file; never a shell.
    return subprocess.run(argv, **kwargs)  # noqa: S603  # nosec B603


def _popen(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(argv, **kwargs)  # noqa: S603  # nosec B603


# --- names and retention -------------------------------------------------------------


def backup_name(now: datetime) -> str:
    return f"{PREFIX}{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"


def backups_in(directory: Path) -> list[Path]:
    """Finished backups, oldest first (the name sorts by time)."""
    return sorted(p for p in directory.iterdir() if p.is_file() and NAME_RE.match(p.name))


def to_prune(names: Sequence[str], keep: int) -> list[str]:
    """Backup names beyond the newest `keep`; names that aren't backups are never touched."""
    if keep < 1:
        raise ValueError("keep at least one backup")
    ours = sorted(n for n in names if NAME_RE.match(n))
    return ours[:-keep] if len(ours) > keep else []


def prune(directory: Path, keep: int) -> list[str]:
    gone = to_prune([p.name for p in backups_in(directory)], keep)
    for name in gone:
        (directory / name).unlink()
    return gone


# --- configuration checks ------------------------------------------------------------


def read_recipients(path: Path | None) -> list[str]:
    """The age public keys to encrypt to; refuses a missing file, no key or a private key."""
    if path is None:
        raise BackupError("config", "COMMONS_BACKUP_RECIPIENTS is not set")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise BackupError("config", f"cannot read the recipients file {path}") from None
    if SECRET_MARK in text.upper():
        # Never echo the line: it is a private key.
        raise BackupError(
            "config",
            f"{path} contains an age private key; only public keys (age1…) belong on the box",
        )
    keys = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not RECIPIENT_RE.match(line):
            raise BackupError("config", f"{path} line {n} is not an age public key (age1…)")
        keys.append(line)
    if not keys:
        raise BackupError("config", f"{path} has no age public key")
    return keys


def _device(path: Path) -> int:
    return path.stat().st_dev


def check_destination(out_dir: Path | None, stack_paths: Sequence[Path], allow_same: bool) -> Path:
    """The backup directory must exist and sit on another device than the stack."""
    if out_dir is None:
        raise BackupError("config", "COMMONS_BACKUP_DIR is not set")
    if not out_dir.is_dir():
        raise BackupError("config", f"backup directory {out_dir} does not exist (disk mounted?)")
    if not os.access(out_dir, os.W_OK):
        raise BackupError("config", f"backup directory {out_dir} is not writable")
    if not allow_same:
        dev = _device(out_dir)
        for p in stack_paths:
            if p.exists() and _device(p) == dev:
                raise BackupError(
                    "config",
                    f"backup directory {out_dir} is on the same disk as {p}; "
                    "mount the backup disk (or pass --allow-same-device for a test)",
                )
    return out_dir


# --- docker compose ------------------------------------------------------------------


class Compose:
    """`docker compose` in the checkout; the only way this script touches the stack."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _cmd(self, args: Sequence[str]) -> list[str]:
        return [tool("docker"), "compose", *args]

    def run(self, args: Sequence[str], *, stdin=None, stdout=None, check: bool = True):
        return _run(self._cmd(args), cwd=self.root, stdin=stdin, stdout=stdout, check=check)

    def output(self, args: Sequence[str]) -> str:
        return _run(
            self._cmd(args), cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def running(self, service: str) -> bool:
        try:
            return bool(self.output(["ps", "--status", "running", "-q", service]).strip())
        except subprocess.CalledProcessError:
            return False

    def volume_tar(self, v: Volume, extract: bool) -> list[str]:
        """Run tar in a one-off container of the volume's service, as its own user."""
        mode = "-xf" if extract else "-cf"
        return [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "tar",
            v.service,
            "-C",
            v.path,
            mode,
            "-",
            *([] if extract else ["."]),
        ]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_part(stage: Path, name: str) -> int:
    return os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def dump_databases(compose: Compose, stage: Path) -> None:
    for db in DATABASES:
        fd = _write_part(stage, f"{db}.dump")
        try:
            compose.run(
                ["exec", "-T", "postgres", "pg_dump", "-U", PG_USER, *PG_DUMP_ARGS, db], stdout=fd
            )
        except (subprocess.CalledProcessError, OSError):
            raise BackupError("database", f"pg_dump of {db} failed") from None
        finally:
            os.close(fd)


def copy_volumes(compose: Compose, stage: Path) -> None:
    for v in VOLUMES:
        was_running = v.stop and compose.running(v.service)
        fd = _write_part(stage, f"{v.part}.tar")
        try:
            if was_running:
                compose.run(["stop", v.service])
            compose.run(compose.volume_tar(v, extract=False), stdout=fd)
        except (subprocess.CalledProcessError, OSError):
            raise BackupError("files", f"copying the {v.part} volume failed") from None
        finally:
            os.close(fd)
            if was_running:
                compose.run(["start", v.service], check=False)


def _clean_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def pack_host_files(root: Path, stage: Path) -> None:
    if not (root / "secrets").is_dir():
        raise BackupError("files", f"{root / 'secrets'} is missing")
    fd = _write_part(stage, "host-files.tar")
    with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w") as tar:
        for rel in HOST_FILES:
            if (root / rel).exists():
                tar.add(root / rel, arcname=rel, filter=_clean_info)


def git_commit(root: Path) -> str | None:
    try:
        out = _run(
            [tool("git"), "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        )
    except (OSError, BackupError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def write_manifest(stage: Path, created: datetime, commit: str | None) -> dict:
    manifest = {
        "format": FORMAT,
        "created": created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit,
        "parts": {
            name: {"sha256": sha256_file(stage / name), "bytes": (stage / name).stat().st_size}
            for name in PARTS
        },
    }
    fd = _write_part(stage, MANIFEST)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def encrypt(stage: Path, recipients_file: Path, target: Path) -> None:
    """Stream the parts through age into `target` (0600, never overwritten)."""
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    if target.exists():
        raise BackupError("write", f"{target.name} already exists")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise BackupError("write", f"cannot create {partial.name}") from None
    try:
        proc = _popen(
            [tool("age"), "--encrypt", "--recipients-file", str(recipients_file)],
            stdin=subprocess.PIPE,
            stdout=fd,
        )
        if proc.stdin is None:  # pragma: no cover - PIPE always gives one
            raise BackupError("encrypt", "age has no input")
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|gz") as tar:
                for name in (MANIFEST, *PARTS):
                    tar.add(stage / name, arcname=name, filter=_clean_info)
        except (OSError, tarfile.TarError):
            proc.kill()
            proc.wait()
            raise BackupError("encrypt", "writing to age failed") from None
        finally:
            proc.stdin.close()
        if proc.wait() != 0:
            raise BackupError("encrypt", "age failed")
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        partial.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.link(partial, target)  # fails instead of overwriting
        partial.unlink()
        _fsync_dir(target.parent)
    except OSError:
        partial.unlink(missing_ok=True)
        raise BackupError("write", f"cannot write {target.name}") from None


def stack_paths(root: Path, compose: Compose | None) -> list[Path]:
    """Where the stack keeps data on this host: the checkout and Docker's data root."""
    paths = [root]
    if compose is not None:
        try:
            out = _run(
                [tool("docker"), "info", "--format", "{{.DockerRootDir}}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, BackupError, subprocess.CalledProcessError):
            out = ""
        if out:
            paths.append(Path(out))
    return paths


def backup(
    root: Path,
    out_dir: Path | None,
    recipients_file: Path | None,
    *,
    keep: int = DEFAULT_KEEP,
    allow_same_device: bool = False,
    now: datetime | None = None,
    tmp_dir: Path | None = None,
) -> Path:
    now = now or datetime.now(UTC)
    if keep < 1:
        raise BackupError("config", "COMMONS_BACKUP_KEEP must be at least 1")
    read_recipients(recipients_file)
    if recipients_file is None:  # pragma: no cover - read_recipients refused already
        raise BackupError("config", "no recipients")
    compose = Compose(root)
    out_dir = check_destination(out_dir, stack_paths(root, compose), allow_same_device)
    target = out_dir / backup_name(now)
    # Plaintext parts only live in this 0700 directory, removed on every exit.
    with tempfile.TemporaryDirectory(prefix="commons-backup-", dir=tmp_dir) as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o700)
        dump_databases(compose, stage)
        copy_volumes(compose, stage)
        pack_host_files(root, stage)
        write_manifest(stage, now, git_commit(root))
        encrypt(stage, recipients_file, target)
    gone = prune(out_dir, keep)
    log(f"wrote {target.name} ({target.stat().st_size} bytes, sha256 {sha256_file(target)})")
    if gone:
        log(f"removed {len(gone)} old backup(s): {', '.join(gone)}")
    return target


def send_alert(root: Path, reason: str) -> None:
    """Ask the app to message every admin; logs (never raises) when that fails too."""
    try:
        Compose(root).run(
            ["exec", "-T", "app", "python", "-m", "app.backup_alert", reason],
            stdout=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        log("could not tell the admins (is the app running?)")


# --- restore -------------------------------------------------------------------------


def decrypt_and_extract(archive: Path, identity: Path, dest: Path) -> dict:
    """Decrypt `archive` into `dest` and check every part against the manifest."""
    allowed = {MANIFEST, *PARTS}
    proc = _popen(
        [tool("age"), "--decrypt", "--identity", str(identity), str(archive)],
        stdout=subprocess.PIPE,
    )
    if proc.stdout is None:  # pragma: no cover
        raise BackupError("encrypt", "age has no output")
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:
            for member in tar:
                if member.name not in allowed or not member.isfile() or member.name in seen:
                    raise BackupError("files", f"unexpected entry {member.name!r} in the backup")
                seen.add(member.name)
                tar.extract(member, dest, filter="data")
    except (tarfile.TarError, EOFError, OSError):
        raise BackupError("encrypt", "cannot decrypt or read the backup (wrong key?)") from None
    finally:
        proc.stdout.close()
        if proc.poll() is None and sys.exc_info()[0] is not None:
            proc.kill()
        returncode = proc.wait()
    if returncode != 0:
        raise BackupError("encrypt", "age could not decrypt the backup (wrong key?)")
    return check_manifest(dest)


def check_manifest(dest: Path) -> dict:
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("files", "the backup has no readable manifest") from None
    if manifest.get("format") != FORMAT:
        raise BackupError("files", f"unknown backup format {manifest.get('format')!r}")
    parts = manifest.get("parts") or {}
    if set(parts) != set(PARTS):
        raise BackupError("files", "the manifest does not list the expected parts")
    for name, meta in parts.items():
        path = dest / name
        if not path.is_file() or sha256_file(path) != meta.get("sha256"):
            raise BackupError("files", f"{name} does not match the manifest")
    return manifest


def _wait_for_postgres(compose: Compose, timeout: float = 120) -> None:
    # Over TCP: the init scripts run on a socket-only server, so this waits for the real one.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = compose.run(
            ["exec", "-T", "postgres", "pg_isready", "-q", "-h", "127.0.0.1", "-U", PG_USER],
            stdout=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode == 0:
            return
        time.sleep(2)
    raise BackupError("database", "PostgreSQL did not start")


def _psql_value(compose: Compose, db: str, sql: str) -> str:
    return compose.output(
        ["exec", "-T", "postgres", "psql", "-U", PG_USER, "-d", db, "-tAc", sql]
    ).strip()


def restore(root: Path, archive: Path, identity: Path, *, tmp_dir: Path | None = None) -> dict:
    """Restore into a fresh checkout: no secrets/, empty databases, empty volumes."""
    if (root / "secrets").exists():
        raise BackupError(
            "config",
            f"{root / 'secrets'} exists: restore only into a fresh clone, never over a running box",
        )
    for rel in HOST_FILES[1:]:
        if (root / rel).exists():
            raise BackupError("config", f"{root / rel} exists: restore into a fresh clone")
    compose = Compose(root)
    # docker-compose.yml names the project, so a second checkout on the same host
    # would drive the running box's containers: refuse unless the project is unused.
    if compose.output(["ps", "-a", "-q"]).strip():
        raise BackupError(
            "config",
            "this compose project already has containers; on a host that runs COMMONS, "
            "set COMPOSE_PROJECT_NAME=commons-restore for the drill",
        )
    with tempfile.TemporaryDirectory(prefix="commons-restore-", dir=tmp_dir) as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o700)
        manifest = decrypt_and_extract(archive, identity, stage)
        log(f"backup from {manifest['created']} (commit {manifest.get('commit') or 'unknown'})")

        with tarfile.open(stage / "host-files.tar") as tar:
            for member in tar:
                top = member.name.split("/", 1)[0]
                if top not in HOST_FILES:
                    raise BackupError("files", f"unexpected host file {member.name!r}")
            tar.extractall(root, filter="data")
        os.chmod(root / "secrets", 0o700)
        log("restored secrets/, community.yaml and .env")

        try:
            compose.run(["up", "-d", "--no-deps", "postgres"])
            _wait_for_postgres(compose)
            for db in DATABASES:
                tables = _psql_value(
                    compose, db, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                )
                if tables != "0":
                    raise BackupError("database", f"database {db} is not empty; use fresh volumes")
                with (stage / f"{db}.dump").open("rb") as f:
                    compose.run(
                        [
                            "exec",
                            "-T",
                            "postgres",
                            "pg_restore",
                            "-U",
                            PG_USER,
                            "-d",
                            db,
                            *PG_RESTORE_ARGS,
                        ],
                        stdin=f,
                    )
                log(f"restored database {db}")
        except subprocess.CalledProcessError:
            raise BackupError("database", "restoring the databases failed") from None

        try:
            compose.run(["build", "--quiet", *(v.service for v in VOLUMES)])
            for v in VOLUMES:
                present = compose.output(
                    [
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--entrypoint",
                        "find",
                        v.service,
                        v.path,
                        "-mindepth",
                        "1",
                        "-maxdepth",
                        "1",
                    ]
                ).strip()
                if present:
                    raise BackupError(
                        "files", f"the {v.part} volume is not empty; use fresh volumes"
                    )
                with (stage / f"{v.part}.tar").open("rb") as f:
                    compose.run(compose.volume_tar(v, extract=True), stdin=f)
                log(f"restored volume {v.part}")
        except subprocess.CalledProcessError:
            raise BackupError("files", "restoring the volumes failed") from None
    log("done. Next: make up, then: docker compose exec app python -m app.cli verify-log")
    return manifest


# --- command line --------------------------------------------------------------------


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def _root() -> Path:
    return _env_path("COMMONS_ROOT") or Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backup.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="write one encrypted backup and prune old ones")
    b.add_argument("--allow-same-device", action="store_true", help="tests only")
    b.add_argument("--no-alert", action="store_true", help="don't message the admins on failure")
    r = sub.add_parser("restore", help="restore a backup into this fresh checkout")
    r.add_argument("archive", type=Path)
    r.add_argument("--identity", type=Path, required=True, help="age private key file")
    x = sub.add_parser("extract", help="decrypt and check a backup into a directory")
    x.add_argument("archive", type=Path)
    x.add_argument("--identity", type=Path, required=True)
    x.add_argument("--to", type=Path, required=True)
    args = p.parse_args(argv)
    os.umask(0o077)
    root = _root()
    tmp_dir = _env_path("COMMONS_BACKUP_TMPDIR")
    try:
        if args.cmd == "backup":
            keep_raw = os.environ.get("COMMONS_BACKUP_KEEP", str(DEFAULT_KEEP))
            try:
                keep = int(keep_raw)
            except ValueError:
                raise BackupError("config", "COMMONS_BACKUP_KEEP must be a number") from None
            backup(
                root,
                _env_path("COMMONS_BACKUP_DIR"),
                _env_path("COMMONS_BACKUP_RECIPIENTS"),
                keep=keep,
                allow_same_device=args.allow_same_device,
                tmp_dir=tmp_dir,
            )
        elif args.cmd == "restore":
            restore(root, args.archive, args.identity, tmp_dir=tmp_dir)
        else:
            args.to.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(args.to.iterdir()):
                raise BackupError("config", f"{args.to} is not empty")
            m = decrypt_and_extract(args.archive, args.identity, args.to)
            log(f"ok: {len(m['parts'])} parts match the manifest (backup from {m['created']})")
    except BackupError as e:
        reason, message = e.reason, str(e)
    except Exception as e:  # never die silently at 3 a.m.
        reason, message = "other", f"unexpected {type(e).__name__}"
    else:
        return 0
    log(f"{args.cmd} failed ({reason}): {message}")
    if args.cmd == "backup" and not args.no_alert:
        send_alert(root, reason)
    return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
