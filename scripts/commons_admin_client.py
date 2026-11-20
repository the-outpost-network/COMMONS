#!/usr/bin/env python3
"""Give COMMONS an OAuth client for the MAS admin API.

    .venv/bin/python scripts/commons_admin_client.py

COMMONS uses it (client-credentials grant, short-lived MAS admin tokens) to keep a
member's Matrix display name in step with COMMONS. MAS reads the client from a
second config file, so the generated mas.yaml stays untouched.

Writes into secrets/matrix/ (mode 0600, never overwrites, never prints values):
  mas-commons-client.yaml     MAS config part: the client and its admin permission
  matrix_admin_client_secret  the same secret, for the app
"""

import os
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "secrets" / "matrix"
# Not secret; must match COMMONS_MATRIX_ADMIN_CLIENT_ID in docker-compose.yml.
CLIENT_ID = "01KCMMNS0APP0000000000000C"


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main() -> int:
    names = ("mas-commons-client.yaml", "matrix_admin_client_secret")
    if not OUT.is_dir():
        print(f"error: {OUT} is missing; run scripts/synapse_trial_init.py first", file=sys.stderr)
        return 1
    existing = [n for n in names if (OUT / n).exists()]
    if existing:
        print(f"error: {OUT} already has {', '.join(existing)}; not overwriting", file=sys.stderr)
        return 1

    secret = secrets.token_urlsafe(48)
    config = {
        "clients": [
            {
                "client_id": CLIENT_ID,
                "client_auth_method": "client_secret_basic",
                "client_secret": secret,
            }
        ],
        # Lets this client request admin scopes with the client-credentials grant.
        "policy": {"data": {"admin_clients": [CLIENT_ID]}},
    }
    write_private(OUT / "mas-commons-client.yaml", yaml.safe_dump(config, sort_keys=False))
    write_private(OUT / "matrix_admin_client_secret", secret + "\n")
    print(f"ok: wrote {', '.join(names)} to {OUT.relative_to(ROOT)}/ (mode 0600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
