#!/usr/bin/env python3
"""Generate Synapse + MAS bootstrap secrets into secrets/matrix/.

    .venv/bin/python scripts/synapse_trial_init.py

Writes (mode 0600, never overwrites, never prints secret values):
  postgres_password   database password (shared by Synapse and MAS, trial only)
  synapse.yaml        Synapse config part with the database and MAS secrets
  signing.key         Synapse's server signing key
  mas.yaml            full MAS config: deploy/synapse/mas.yaml + generated keys
"""

import argparse
import base64
import os
import secrets
import subprocess  # nosec B404 - runs docker with fixed arguments only
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "secrets" / "matrix"
MAS_IMAGE = (
    "ghcr.io/element-hq/matrix-authentication-service:1.28.0"
    "@sha256:494c67f08ee8344368f4ed336611dea02b26c4e058bf37a212d090d50b612659"
)


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def refresh_mas_hostnames() -> int:
    mas_path = OUT / "mas.yaml"
    if not mas_path.is_file():
        print(
            f"error: {mas_path} is missing; run this script without options first", file=sys.stderr
        )
        return 1

    template = yaml.safe_load((ROOT / "deploy" / "synapse" / "mas.yaml").read_text())
    mas = yaml.safe_load(mas_path.read_text())
    if not isinstance(mas, dict) or not isinstance(template, dict):
        print("error: MAS config must be a YAML mapping", file=sys.stderr)
        return 1
    try:
        mas["http"]["public_base"] = template["http"]["public_base"]
        mas["http"]["issuer"] = template["http"]["issuer"]
        mas["matrix"]["homeserver"] = template["matrix"]["homeserver"]
    except (KeyError, TypeError):
        print("error: MAS config is missing expected hostname settings", file=sys.stderr)
        return 1

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=OUT, prefix=".mas.yaml.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            yaml.safe_dump(mas, temporary, sort_keys=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, mas_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    print("ok: refreshed MAS hostnames without changing generated secret values")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-hostnames",
        action="store_true",
        help="update hostnames in the existing generated MAS config without changing secrets",
    )
    args = parser.parse_args()
    if args.refresh_hostnames:
        return refresh_mas_hostnames()

    names = ("postgres_password", "synapse.yaml", "signing.key", "mas.yaml")
    existing = [n for n in names if (OUT / n).exists()]
    if existing:
        print(f"error: {OUT} already has {', '.join(existing)}; not overwriting", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)

    db_password = secrets.token_urlsafe(32)
    mas_secret = secrets.token_urlsafe(48)

    synapse = {
        "database": {
            "name": "psycopg2",
            "args": {
                "user": "matrix",
                "password": db_password,
                "dbname": "synapse",
                "host": "postgres",
                "port": 5432,
                "cp_min": 2,
                "cp_max": 5,
            },
        },
        "matrix_authentication_service": {
            "enabled": True,
            "endpoint": "http://mas:8080/",
            "secret": mas_secret,
        },
        "macaroon_secret_key": secrets.token_urlsafe(48),
        "form_secret": secrets.token_urlsafe(48),
    }
    seed = base64.b64encode(secrets.token_bytes(32)).decode().rstrip("=")

    # Fixed argv (pinned MAS image), no shell, no user input; docker from PATH like make.
    generated = subprocess.run(  # noqa: S603  # nosec B603 B607
        ["docker", "run", "--rm", MAS_IMAGE, "config", "generate"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    mas = yaml.safe_load((ROOT / "deploy" / "synapse" / "mas.yaml").read_text())
    mas["secrets"] = yaml.safe_load(generated)["secrets"]
    mas["database"]["password"] = db_password
    mas["matrix"]["secret"] = mas_secret

    write_private(OUT / "postgres_password", db_password + "\n")
    write_private(OUT / "synapse.yaml", yaml.safe_dump(synapse, sort_keys=False))
    write_private(OUT / "signing.key", f"ed25519 a_trial {seed}\n")
    write_private(OUT / "mas.yaml", yaml.safe_dump(mas, sort_keys=False))
    print(f"ok: wrote {', '.join(names)} to {OUT.relative_to(ROOT)}/ (mode 0600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
