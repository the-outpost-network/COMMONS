#!/usr/bin/env python3
"""Add a COMMONS member by Matrix user id (wrapper around `python -m app.cli`).

    scripts/add_member.py @alex:commons.theoutpost.network --role admin

In Docker: docker compose exec app python -m app.cli add-member ...
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli import main

sys.exit(main(["add-member", *sys.argv[1:]]))
