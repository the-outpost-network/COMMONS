#!/usr/bin/env python3
"""Check that every phone asks for push notifications without content.

    make check-pushers

Push leaves the box through the egress proxy to Element's gateway (matrix.org)
and on to Apple or Google. A pusher registered with `"format": "event_id_only"`
gets only the event id, room id, unread count and its own push key; the phone
then fetches the message from our server. Without that format Synapse would also
send the sender and room name (never the message text: `push.include_content:
false` in deploy/synapse/homeserver.yaml).

Reads the `pushers` table of the Synapse database (read-only, through
`docker compose exec postgres psql`), prints one line per pusher without its
push key, and exits 1 if an enabled HTTP pusher uses another format.

Standard library only: it runs on the host, next to `docker compose`.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PG_USER = "matrix"  # POSTGRES_USER in docker-compose.yml: owns synapse and mas
FORMAT = "event_id_only"
# The push key (device token) is never selected: it is not needed for the check.
SQL = (
    "SELECT user_name, app_id, kind, COALESCE(data::json->>'format', ''), "
    "COALESCE(data::json->>'url', ''), enabled, ts FROM pushers ORDER BY ts"
)


@dataclass(frozen=True)
class Pusher:
    user: str
    app_id: str
    kind: str
    format: str
    gateway: str
    enabled: bool
    since: str


def parse(output: str) -> list[Pusher]:
    """Rows from `psql -tA -F '\\t'`."""
    pushers = []
    for line in output.splitlines():
        if not line.strip():
            continue
        user, app_id, kind, fmt, url, enabled, ts = line.split("\t")
        since = datetime.fromtimestamp(int(ts) / 1000, UTC).strftime("%Y-%m-%d")
        pushers.append(
            Pusher(user, app_id, kind, fmt, urlsplit(url).hostname or "", enabled == "t", since)
        )
    return pushers


def problems(pushers: list[Pusher]) -> list[Pusher]:
    """Enabled HTTP pushers that would get more than the event id."""
    return [p for p in pushers if p.enabled and p.kind == "http" and p.format != FORMAT]


def read_pushers() -> str:
    docker = shutil.which("docker")
    if docker is None:
        sys.exit("check-pushers: docker not found")
    cmd = [docker, "compose", "exec", "-T", "postgres", "psql", "-U", PG_USER, "-d", "synapse",
           "-v", "ON_ERROR_STOP=1", "-tA", "-F", "\t", "-c", SQL]  # fmt: skip
    return subprocess.run(  # noqa: S603  # nosec B603
        cmd, cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def main() -> int:
    pushers = parse(read_pushers())
    for p in pushers:
        state = "" if p.enabled else " (disabled)"
        print(f"{p.since}  {p.user}  {p.app_id}  {p.format or '(no format)'}  {p.gateway}{state}")
    bad = problems(pushers)
    if bad:
        print(f"check-pushers: {len(bad)} pusher(s) without {FORMAT}; see docs/SECURITY.md")
        return 1
    print(f"check-pushers: {len(pushers)} pusher(s), every enabled one uses {FORMAT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
