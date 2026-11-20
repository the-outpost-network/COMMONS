#!/usr/bin/env python3
"""Anonymous usage numbers from the real COMMONS database.

    make usage-report                              # Markdown, on the running box
    make usage-report FORMAT=csv SINCE=2026-11-13  # CSV, from that day on

Runs inside the app container, with the app's settings and database account. The
database transaction is read-only, and the demo database is refused.

Only aggregate counts come out. Names, Matrix ids, notes and message text are
never even selected; member ids are used in memory to count distinct people.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

# `python -` (how `make usage-report` runs it in the container) has no __file__
# and already finds `app` in the working directory.
if "__file__" in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Connection, Engine, select, text

from app.config import load_settings, read_secret
from app.db import make_engine
from app.models import Bookable, Booking, Member, ResourceCategory, ResourceEntry

CHANNELS = ("matrix", "web", "sensor")
EXIT_REFUSED = 2

# Governance tables may not exist yet: the report then says so instead of
# printing a 0 that reads like "nobody voted".
HAS_TABLE = text("SELECT to_regclass(:name) IS NOT NULL")
PROPOSALS = text("SELECT created_at FROM proposal")
VOTES = text("SELECT member_id, channel, cast_at, superseded_at FROM vote")
# community.DEMO_KEY, written by scripts/seed_demo.py. Spelled out so the report
# also runs, through stdin, in an app image that predates the demo marker.
IS_DEMO = text("SELECT EXISTS (SELECT 1 FROM config_kv WHERE key = 'demo_data')")


@dataclass(frozen=True)
class CategoryCount:
    name: str
    unit: str
    entries: int
    corrections: int
    undone: int


@dataclass(frozen=True)
class BookableCount:
    name: str
    kind: str
    bookings: int
    cancelled: int


@dataclass(frozen=True)
class Report:
    generated_on: date
    since: date | None
    first_use: date | None
    days_in_use: int
    days_with_activity: int
    active_members: int
    member_accounts: int
    categories: list[CategoryCount]
    bookables: list[BookableCount]
    channels: dict[str, int]
    # None: the feature's tables aren't on this box.
    proposals: int | None
    votes_current: int | None
    votes_changed: int | None


def refusal(env: Mapping[str, str], database: str | None) -> str | None:
    """Why this must not be reported as real use, or None if it may."""
    mode = env.get("COMMONS_ENV", "").strip().lower()
    if mode not in ("", "production"):
        return f"COMMONS_ENV is {mode!r}, not production"
    if "demo" in (database or "").lower():
        return "this is the demo database"
    return None


@contextmanager
def read_only(engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction PostgreSQL won't let write anything."""
    with engine.connect() as connection, connection.begin():
        connection.execute(text("SET TRANSACTION READ ONLY"))
        yield connection


def collect(
    connection: Connection,
    *,
    timezone: str,
    since: date | None = None,
    now: datetime | None = None,
) -> Report:
    tz = ZoneInfo(timezone)
    now = now or datetime.now(UTC)
    start = None if since is None else datetime.combine(since, time.min, tz)

    def in_period(moment: datetime) -> bool:
        return start is None or moment >= start

    rows = connection.execute(
        select(
            ResourceEntry.id,
            ResourceEntry.category_id,
            ResourceEntry.member_id,
            ResourceEntry.channel,
            ResourceEntry.entry_type,
            ResourceEntry.created_at,
            ResourceEntry.corrects_id,
            ResourceEntry.void,
        ).order_by(ResourceEntry.created_at, ResourceEntry.id)
    ).all()
    originals = [r for r in rows if r.corrects_id is None]
    fixes = [r for r in rows if r.corrects_id is not None]
    # The latest correction decides, as in the ledger; rows are in time order.
    undone = {r.corrects_id: r.void for r in fixes}
    undone_ids = {entry_id for entry_id, void in undone.items() if void}
    entries = [r for r in originals if r.id not in undone_ids and in_period(r.created_at)]
    dropped = [r for r in originals if r.id in undone_ids and in_period(r.created_at)]
    corrections = [
        r
        for r in fixes
        if not r.void and r.corrects_id not in undone_ids and in_period(r.created_at)
    ]

    bookings = [
        r
        for r in connection.execute(
            select(
                Booking.bookable_id,
                Booking.member_id,
                Booking.channel,
                Booking.created_at,
                Booking.cancelled_at,
            )
        ).all()
        if in_period(r.created_at)
    ]
    kept_bookings = [b for b in bookings if b.cancelled_at is None]

    proposals = votes_current = votes_changed = None
    votes = []
    if connection.execute(HAS_TABLE, {"name": "public.proposal"}).scalar_one():
        proposals = sum(in_period(r.created_at) for r in connection.execute(PROPOSALS))
    if connection.execute(HAS_TABLE, {"name": "public.vote"}).scalar_one():
        votes = [r for r in connection.execute(VOTES).all() if in_period(r.cast_at)]
        votes_current = sum(v.superseded_at is None for v in votes)
        votes_changed = len(votes) - votes_current

    actions = [*entries, *corrections, *kept_bookings, *votes]
    moments = [
        *(r.created_at for r in [*entries, *corrections, *kept_bookings]),
        *(v.cast_at for v in votes),
    ]
    days = {m.astimezone(tz).date() for m in moments}
    today = now.astimezone(tz).date()
    first_use = min(days, default=None)

    categories = []
    for c in connection.execute(
        select(
            ResourceCategory.id,
            ResourceCategory.name,
            ResourceCategory.unit,
            ResourceCategory.archived_at,
        ).order_by(ResourceCategory.position, ResourceCategory.id)
    ):
        count = CategoryCount(
            c.name,
            c.unit,
            sum(r.category_id == c.id for r in entries),
            sum(r.category_id == c.id for r in corrections),
            sum(r.category_id == c.id for r in dropped),
        )
        if c.archived_at is None or count.entries or count.corrections or count.undone:
            categories.append(count)

    bookables = []
    for b in connection.execute(
        select(Bookable.id, Bookable.name, Bookable.type, Bookable.archived_at).order_by(
            Bookable.position, Bookable.id
        )
    ):
        kept = sum(r.bookable_id == b.id for r in kept_bookings)
        cancelled = sum(r.bookable_id == b.id and r.cancelled_at is not None for r in bookings)
        if b.archived_at is None or kept or cancelled:
            bookables.append(BookableCount(b.name, b.type, kept, cancelled))

    accounts = connection.execute(select(Member.id).where(Member.deactivated_at.is_(None))).all()
    return Report(
        generated_on=today,
        since=since,
        first_use=first_use,
        days_in_use=(today - first_use).days + 1 if first_use else 0,
        days_with_activity=len(days),
        active_members=len({a.member_id for a in actions}),
        member_accounts=len(accounts),
        categories=categories,
        bookables=bookables,
        channels={ch: sum(a.channel == ch for a in actions) for ch in CHANNELS},
        proposals=proposals,
        votes_current=votes_current,
        votes_changed=votes_changed,
    )


def _share(part: int, total: int) -> int:
    return round(100 * part / total) if total else 0


def _cell(value: object) -> str:
    """A Markdown table cell; names come from the community's configuration."""
    return " ".join(str(value).split()).replace("|", "\\|")


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return [*lines, ""]


def _or_not_built(value: int | None) -> int | str:
    return "not on this box yet" if value is None else value


def to_markdown(r: Report) -> str:
    period = f"from {r.since.isoformat()}" if r.since else "all recorded use"
    total = sum(r.channels.values())
    lines = [
        "# COMMONS usage report",
        "",
        f"Use of this COMMONS box, {period}, as of {r.generated_on.isoformat()}. "
        "Aggregate counts only. Undone entries and cancelled bookings are not counted as use.",
        "",
        *_table(
            ("Overall", "Count"),
            [
                ("Active members (logged, booked or voted)", r.active_members),
                ("Member accounts", r.member_accounts),
                ("First recorded use", r.first_use.isoformat() if r.first_use else "none yet"),
                ("Days since first recorded use", r.days_in_use),
                ("Days with activity", r.days_with_activity),
            ],
        ),
        *_table(
            ("Resource", "Unit", "Entries", "Corrections", "Undone (not counted)"),
            [(c.name, c.unit, c.entries, c.corrections, c.undone) for c in r.categories],
        ),
        *_table(
            ("Bookable", "Type", "Bookings", "Cancelled (not counted)"),
            [(b.name, b.kind, b.bookings, b.cancelled) for b in r.bookables],
        ),
        *_table(
            ("Governance", "Count"),
            [
                ("Proposals", _or_not_built(r.proposals)),
                ("Votes (current)", _or_not_built(r.votes_current)),
                ("Votes later changed", _or_not_built(r.votes_changed)),
            ],
        ),
        *_table(
            ("Channel", "Actions", "Share"),
            [(ch, n, f"{_share(n, total)}%") for ch, n in r.channels.items()],
        ),
    ]
    return "\n".join(lines)


def _csv_safe(value: str) -> str:
    """Keep a configured name from running as a formula in a spreadsheet."""
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def to_csv(r: Report) -> str:
    total = sum(r.channels.values())
    rows: list[tuple[str, str, str, object]] = [
        ("period", "", "generated_on", r.generated_on.isoformat()),
        ("period", "", "since", r.since.isoformat() if r.since else ""),
        ("period", "", "first_use", r.first_use.isoformat() if r.first_use else ""),
        ("overall", "", "active_members", r.active_members),
        ("overall", "", "member_accounts", r.member_accounts),
        ("overall", "", "days_in_use", r.days_in_use),
        ("overall", "", "days_with_activity", r.days_with_activity),
    ]
    for c in r.categories:
        name = _csv_safe(c.name)
        rows += [
            ("resource", name, "entries", c.entries),
            ("resource", name, "corrections", c.corrections),
            ("resource", name, "undone", c.undone),
        ]
    for b in r.bookables:
        name = _csv_safe(b.name)
        rows += [("bookable", name, "bookings", b.bookings)]
        rows += [("bookable", name, "cancelled", b.cancelled)]
    # Empty, not 0, when governance isn't on this box.
    for metric, value in (
        ("proposals", r.proposals),
        ("votes_current", r.votes_current),
        ("votes_changed", r.votes_changed),
    ):
        rows.append(("governance", "", metric, "" if value is None else value))
    for channel, n in r.channels.items():
        rows += [("channel", channel, "actions", n)]
        rows += [("channel", channel, "share_percent", _share(n, total))]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(("section", "item", "metric", "value"))
    writer.writerows(rows)
    return out.getvalue()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Anonymous COMMONS usage numbers (read-only).")
    parser.add_argument("--format", choices=("markdown", "csv"), default="markdown")
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="count only what was written from this local day on",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    engine: Engine | None = None,
) -> int:
    """Tests pass `env` and `engine`; otherwise they come from the environment."""
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    settings = load_settings()
    engine = engine or make_engine(settings.database_url, lambda: read_secret("database_password"))
    reason = refusal(env, engine.url.database)
    if reason:
        print(f"usage-report: refused, {reason}", file=sys.stderr)
        return EXIT_REFUSED
    with read_only(engine) as connection:
        if connection.execute(IS_DEMO).scalar_one():
            print("usage-report: refused, this database holds demo data", file=sys.stderr)
            return EXIT_REFUSED
        report = collect(connection, timezone=settings.timezone, since=args.since)
    print(to_csv(report) if args.format == "csv" else to_markdown(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
