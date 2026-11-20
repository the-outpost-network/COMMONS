#!/usr/bin/env python3
"""Fail if repository files use an unapproved theoutpost.network hostname."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - runs git with fixed arguments only
import sys
from pathlib import Path

OUTPOST_DOMAIN = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"(?:(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)\.)*"
    r"theoutpost\.network(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
ALLOWED_HOSTNAMES = {
    "theoutpost.network",
    "commons.theoutpost.network",
    "matrix.commons.theoutpost.network",
    "auth.commons.theoutpost.network",
}


def find_unapproved_domain_references(text: str) -> list[tuple[int, str]]:
    return [
        (line_number, match.group())
        for line_number, line in enumerate(text.splitlines(), start=1)
        for match in OUTPOST_DOMAIN.finditer(line)
        if match.group().lower() not in ALLOWED_HOSTNAMES
    ]


def tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to scan repository files")
    result = subprocess.run(  # noqa: S603  # nosec B603 - fixed git subcommand and arguments; no user input
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(path.decode()) for path in result.stdout.split(b"\0") if path]


def main() -> int:
    violations = []
    for path in tracked_files():
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            continue
        if b"\0" in content:
            continue
        for line_number, hostname in find_unapproved_domain_references(
            content.decode("utf-8", "replace")
        ):
            violations.append(f"{path}:{line_number}: {hostname}")

    if violations:
        print(
            "Unapproved theoutpost.network hostnames found; use commons.theoutpost.network "
            "or its configured Matrix/MAS service hosts:"
        )
        print("\n".join(f"  {location}" for location in violations))
        return 1
    print("Repository hostname check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
