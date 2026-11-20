#!/usr/bin/env python3
"""Verify a COMMONS decision log on your own computer, offline.

Download the log and the public keys from the Decision log page (or
`GET /api/log/export` and `GET /api/log/keys`), then run:

    python3 scripts/verify_log.py commons-decision-log.jsonl --pubkeys commons-log-keys.json
    python3 scripts/verify_log.py log.jsonl --pubkeys keys.json --anchor 412:ab34…  # repeatable

It checks what the box checks, with the same code (`app/chain_verify.py`, loaded
on its own): every entry's hash, the links between entries, the Ed25519
signatures, any anchors you hold (fingerprints posted in the community room),
and a recount of every closed vote from the logged counts. Needs only Python 3.12
and the `cryptography` package; it never connects to anything.

Exit status: 0 the log verifies, 1 it doesn't, 2 the files can't be read.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_BROKEN, EXIT_UNREADABLE = 0, 1, 2
ANCHOR = re.compile(r"^(\d+):([0-9a-fA-F]{64})$")
VERIFIER = Path(__file__).resolve().parent.parent / "app" / "chain_verify.py"


def load_verifier(path: Path = VERIFIER):
    """`app/chain_verify.py` on its own: not the app package, which needs the database."""
    spec = importlib.util.spec_from_file_location("commons_chain_verify", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def anchor(raw: str) -> tuple[int, str]:
    match = ANCHOR.match(raw.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"{raw!r} is not seq:hash (64 hex digits)")
    return int(match[1]), match[2].lower()


def read_entries(path: Path) -> list[dict]:
    entries = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as e:
                raise ValueError(f"{path}, line {n}: not JSON ({e})") from None
            if not isinstance(entry, dict):
                raise ValueError(f"{path}, line {n}: not a log entry")
            entries.append(entry)
    return entries


def read_keys(path: Path) -> dict[str, str]:
    keys = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(keys, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
    ):
        raise ValueError(f"{path}: expected {{key id: public key hex}}")
    return keys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an exported COMMONS decision log offline.")
    parser.add_argument("log", type=Path, help="the exported log (JSON Lines)")
    parser.add_argument(
        "--pubkeys", type=Path, required=True, help="the public keys (JSON: key id -> hex)"
    )
    parser.add_argument(
        "--anchor",
        type=anchor,
        action="append",
        default=[],
        metavar="SEQ:HASH",
        help="a fingerprint you hold, e.g. from the community room (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verifier = load_verifier()
        entries = read_entries(args.log)
        keys = read_keys(args.pubkeys)
    except (OSError, ValueError) as e:
        print(f"verify_log: can't read the files: {e}", file=sys.stderr)
        return EXIT_UNREADABLE
    result = verifier.verify_chain(entries, keys, args.anchor)
    if not result.ok:
        print(f"BROKEN at entry {result.failed_seq}: {result.reason}")
        if result.head_seq:
            print(f"entries 1 to {result.head_seq} are intact")
        return EXIT_BROKEN
    print(
        f"OK: {result.head_seq} entries verify (hashes, links, signatures), "
        f"{len(args.anchor)} anchors match, {result.tallies} vote results recounted"
    )
    print(f"head: {result.head_seq}:{result.head_hash}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
