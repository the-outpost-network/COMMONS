#!/usr/bin/env python3
"""A small, clearly synthetic demo: three days of use, for trying COMMONS out.

    make seed-demo                  # rebuilds the separate demo database
    make seed-demo AS_OF=2026-11-19 # the same three days, ending the day before

Everything is written through the real services, so the data obeys the same rules
as real use. The result is the same for a given `--as-of` date and cast.

It uses only what the community file defines: its resources, its bookable things
(plus one generic thing for a type the file has none of) and its vote types. In
three days it shows each part once: readings and use of every resource, a typo
corrected and a double entry undone, one resource close to its warning level,
bookings past and ahead (one given back, one cancelled), and three votes: one
passed, one cancelled and one still open. Three days are too few for a forecast;
the board says so.

It can't reach real data. It runs only with COMMONS_ENV=demo, on a database with
"demo" in its name that is still empty, with its own data directory and so its
own signing key. The database is marked as demo data in `config_kv` and in the
decision log: the app then refuses to start on it outside demo mode, and the
usage report refuses it.

Who appears: fictional members by default. `make seed-demo` passes the git-ignored
`demo-members.json` instead if it exists (a list of `{"localpart", "name",
"roles"}`). A localpart that matches an account on the homeserver lets that
person sign in to the demo.

Vote messages count as sent and nothing goes to the community room, so the bot
doesn't post old lines. The open vote behaves like a real one from then on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

# `python -` (how `make seed-demo` runs it in the container) has no __file__ and
# already finds `app` in the working directory.
if "__file__" in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from app import (
    bookables,
    calendar,
    categories,
    community,
    decision_log,
    governance,
    impact,
    ledger,
    members,
)
from app.bookables import ThingSpec
from app.config import load_settings, read_secret
from app.db import make_engine, make_sessionmaker, upgrade_database
from app.events import PENDING
from app.models import Bookable, ChatNotification, Member, ResourceCategory

DAYS = 3
EXIT_REFUSED = 2

DEFAULT_CAST = (
    {"localpart": "ana", "name": "Ana Example", "roles": ["admin", "board"]},
    {"localpart": "ben", "name": "Ben Example", "roles": ["board"]},
    {"localpart": "cleo", "name": "Cleo Example", "roles": []},
    {"localpart": "dov", "name": "Dov Example", "roles": []},
)
# One generic thing for each type the community file has none of.
FALLBACK_THINGS = {
    "space": {"name": "Meeting room", "type": "space"},
    "vehicle": {"name": "Shared car", "type": "vehicle"},
    "tool": {"name": "Ladder", "type": "tool"},
}
BOOKING_NOTES = {"space": "group meeting", "vehicle": "weekly shop", "tool": "small repairs"}
BOOKING_HOURS = {"space": 3, "vehicle": 4, "tool": 4}

# Days are counted back from the demo day (-1 is yesterday), `hour` is local time.
# `ballots` has one character per member, in cast order (repeated for a longer
# cast): an option's number, "a" to abstain, "-" for no vote. `changes` lists
# (member index, new choice). The last member never votes on the open vote, so it
# can't close early because everyone voted.
DEMO_VOTES = (
    {
        "day": -3,
        "hour": 19,
        "type": "spending",
        "proposer": 1,
        "question": "Buy a second rain barrel for the garden?",
        "why": "The first one ran dry twice last month.",
        "options": ["Yes", "No"],
        "amount": "1200",
        "ballots": "121a",
    },
    {
        "day": -2,
        "hour": 18,
        "type": "events",
        "proposer": 3,
        "anonymous": True,
        "question": "Hold a repair café in the common space on Saturday?",
        "options": ["Yes", "No"],
        "ballots": "1-2-",
        # (member index, hours after opening, reason)
        "cancel": (0, 20, "The space is booked that day; we'll ask again next month."),
    },
    {
        "day": -1,
        "hour": 10,
        "type": "rules",
        "proposer": 0,
        "question": "Make Sunday morning the shared cleaning time?",
        "why": "One fixed time, so the shared rooms get done together.",
        "options": ["Yes", "No", "Saturday instead"],
        "ballots": "13a-",
        "changes": [(0, "3")],
    },
)


@dataclass(frozen=True)
class Seeded:
    members: int
    entries: int
    bookings: int
    votes: int = 0


class SeedRefused(Exception):
    pass


def refusal(env: Mapping[str, str], database: str | None, data_dir: Path) -> str | None:
    """Why this must not run here, or None if it may."""
    if env.get("COMMONS_ENV", "").strip().lower() != "demo":
        return "COMMONS_ENV must be demo"
    if "demo" not in (database or "").lower():
        return f"database {database!r} is not a demo database"
    if data_dir.name != "demo":
        return f"data directory {data_dir} is not a demo directory, so the key could be real"
    return None


def parse_cast(raw: str) -> list[dict]:
    """The members to create: the JSON from demo-members.json, or the fictional cast."""
    if not raw.strip():
        return [dict(m) for m in DEFAULT_CAST]
    try:
        cast = json.loads(raw)
        people = [
            {"localpart": str(m["localpart"]), "name": str(m["name"]), "roles": list(m["roles"])}
            for m in cast
        ]
    except (ValueError, TypeError, KeyError) as e:
        raise SeedRefused(f"demo-members.json is not a list of members ({e})") from None
    if len(people) < 2 or not any("admin" in p["roles"] for p in people):
        raise SeedRefused("the demo needs at least two members, one of them an admin")
    return people


class _Demo:
    """The three days: who does what, when and through which channel."""

    def __init__(self, db: Session, cast: list[Member], tz: ZoneInfo, as_of: date) -> None:
        self.db = db
        self.cast = cast
        self.tz = tz
        self.as_of = as_of
        self.entries = 0
        self.bookings = 0
        self.turn = 0
        self.last_moment = self.at(-1, 18)

    def at(self, day: int, hour: int, minute: int = 0) -> datetime:
        local = datetime.combine(self.as_of + timedelta(days=day), time(hour, minute), self.tz)
        return local.astimezone(UTC)

    def next_member(self) -> Member:
        """Everyone takes a turn, so every member has done something."""
        member = self.cast[self.turn % len(self.cast)]
        self.turn += 1
        return member

    def channel(self) -> str:
        return "web" if self.turn % 3 == 0 else "matrix"

    def quiet(self) -> None:
        """Drop waiting events: they would become room lines and reach the bot."""
        self.db.info.pop(PENDING, None)

    def log(self, category, kind, amount, when, member=None):
        self.entries += 1
        return ledger.add_entry(
            self.db,
            member or self.next_member(),
            category,
            kind,
            amount,
            channel=self.channel(),
            occurred_at=when,
            now=when,
        )

    def book(self, thing, starts, created, *, note=None):
        self.quiet()  # a booking's savepoint would turn waiting events into room lines
        member = self.next_member()
        booking = calendar.book(
            self.db,
            member,
            thing,
            starts,
            starts + timedelta(hours=BOOKING_HOURS[thing.type]),
            channel=self.channel(),
            note=note or BOOKING_NOTES[thing.type],
            destination="town" if thing.type == "vehicle" else None,
            now=created,
        )
        self.bookings += 1
        return member, booking


def _capacity(category: ResourceCategory) -> float:
    return category.capacity or (category.shortage_threshold or 20.0) * 6


def _round(category: ResourceCategory, value: float) -> float:
    capacity = _capacity(category)
    size = 10.0 if capacity >= 1000 else 1.0 if capacity >= 100 else 0.5 if capacity >= 20 else 0.1
    return max(size, round(round(value / size) * size, 1))


def _seed_stock(d: _Demo, category: ResourceCategory, runs_low: bool) -> None:
    """A morning reading each day; the one that runs low ends just above its threshold."""
    capacity = _capacity(category)
    threshold = category.shortage_threshold or capacity * 0.25
    if runs_low:
        levels = (threshold * 1.6, threshold * 1.35, threshold * 1.1)
    else:
        levels = (capacity * 0.8, capacity * 0.7, capacity * 0.75)
    for day, level in zip(range(-DAYS, 0), levels, strict=True):
        d.log(category, "reading", _round(category, level), d.at(day, 8, 30))
    if not runs_low:
        d.log(category, "add", _round(category, capacity * 0.1), d.at(-2, 17))


def _seed_flow(d: _Demo, category: ResourceCategory) -> None:
    for day in range(-DAYS, 0):
        d.log(category, "use", _round(category, _capacity(category) * 0.03), d.at(day, 12))


def _seed_slips(d: _Demo, stocks: list[ResourceCategory]) -> None:
    """A typo fixed a few minutes later, and a reading logged twice and undone."""
    if not stocks:
        return
    first, last = stocks[0], stocks[-1]
    member = d.cast[0]
    when = d.at(-1, 12)
    level = float(ledger.level(d.db, first).value)
    wrong = d.log(first, "reading", _round(first, level * 1.1), when, member)
    ledger.correct_entry(d.db, member, wrong, level, channel="web", now=when + timedelta(minutes=9))
    d.entries += 1
    when = d.at(-1, 13)
    level = float(ledger.level(d.db, last).value)
    d.log(last, "reading", level, when, member)
    twin = d.log(last, "reading", level, when + timedelta(minutes=2), member)
    ledger.correct_entry(
        d.db, member, twin, 0, channel="web", void=True, now=when + timedelta(minutes=7)
    )
    d.entries += 1


def _seed_bookings(d: _Demo, things: list[Bookable]) -> None:
    """Each thing booked once in the past; one of each type ahead; the last one cancelled."""
    ahead: set[str] = set()
    for index, thing in enumerate(things):
        starts = d.at(-3 + index % 2, 10)
        member, booking = d.book(thing, starts, starts - timedelta(days=1))
        if thing.type != "space":
            ends = booking.ends_at or starts + timedelta(hours=BOOKING_HOURS[thing.type])
            calendar.return_item(d.db, member, booking, now=ends - timedelta(minutes=30))
        if thing.type not in ahead:
            ahead.add(thing.type)
            d.book(thing, d.at(1, 14), d.at(-1, 9))
    if things:
        member, booking = d.book(things[-1], d.at(2, 10), d.at(-1, 11), note="maybe")
        calendar.cancel(d.db, member, booking, now=d.at(-1, 15))


def _choice(proposal, raw: str) -> governance.Choice:
    if raw == "a":
        return governance.ABSTAIN
    return proposal.options[int(raw) - 1].id


def _seed_votes(d: _Demo, signer: decision_log.Signer) -> int:
    """`DEMO_VOTES` through the real service; their chat messages count as sent."""
    try:
        governance.voting_rules(d.db)
    except governance.GovernanceError:
        return 0  # a community file without voting rules: a demo without votes
    people, end = d.cast, d.last_moment
    count = 0
    for spec in DEMO_VOTES:
        opened = d.at(spec["day"], spec["hour"])
        proposal = governance.open_proposal(
            d.db,
            people[spec["proposer"] % len(people)],
            spec["question"],
            spec["options"],
            vote_type_slug=spec["type"],
            signer=signer,
            channel="matrix",
            why=spec.get("why", ""),
            amount=Decimal(spec["amount"]) if "amount" in spec else None,
            anonymous=spec.get("anonymous", False),
            now=opened,
            tz=d.tz,
        )
        count += 1
        last_ballot = min(proposal.closes_at, end) - timedelta(minutes=5)
        cancel = spec.get("cancel")
        if cancel:
            last_ballot = min(last_ballot, opened + timedelta(hours=cancel[1]))
        ballots = (spec["ballots"] * len(people))[: len(people)]
        if proposal.closes_at > end:
            ballots = ballots[:-1] + "-"
        when = opened
        for person, raw in zip(people, ballots, strict=True):
            if raw == "-" or proposal.status != "open":
                continue
            when = min(when + timedelta(minutes=45), last_ballot)
            governance.cast_vote(
                d.db,
                person,
                proposal.id,
                _choice(proposal, raw),
                signer=signer,
                channel="matrix",
                now=when,
            )
        for index, raw in spec.get("changes", []):
            if proposal.status == "open":
                when = min(when + timedelta(hours=1), last_ballot)
                governance.cast_vote(
                    d.db,
                    people[index % len(people)],
                    proposal.id,
                    _choice(proposal, raw),
                    signer=signer,
                    channel="web",
                    now=when,
                )
        if cancel and proposal.status == "open":
            governance.cancel_proposal(
                d.db,
                people[cancel[0] % len(people)],
                proposal.id,
                cancel[2],
                signer=signer,
                channel="web",
                now=opened + timedelta(hours=cancel[1]),
            )
        elif proposal.closes_at <= end:
            governance.close_if_due(
                d.db, proposal.id, signer=signer, now=proposal.closes_at + timedelta(minutes=1)
            )
        d.quiet()
    d.db.execute(
        update(ChatNotification)
        .where(ChatNotification.delivered_at.is_(None))
        .values(delivered_at=ChatNotification.created_at)
    )
    return count


def demo_config(config: community.CommunityFile) -> community.CommunityFile:
    """The community's file, plus a generic thing for each type it has none of."""
    types = {thing.type for thing in config.all_things()}
    extra = [ThingSpec(**spec) for kind, spec in FALLBACK_THINGS.items() if kind not in types]
    return config.model_copy(update={"bookable": [*config.bookable, *extra]})


def seed(
    db: Session,
    config: community.CommunityFile,
    signer: decision_log.Signer,
    *,
    cast: list[dict],
    server_name: str,
    as_of: date,
) -> Seeded:
    """Write the three demo days (caller commits). Only into an empty database."""
    if db.scalar(select(func.count()).select_from(Member)) or community.is_seeded(db):
        raise SeedRefused("the database is not empty")
    community.seed(db, demo_config(config), signer)
    community.mark_demo(db, signer)
    tz = calendar.timezone(db)
    people = [
        members.add_member(
            db,
            f"@{person['localpart']}:{server_name}",
            person["name"],
            tuple(person["roles"]),
            signer=signer,
        )
        for person in cast
    ]
    demo = _Demo(db, people, tz, as_of)
    for person in people:
        person.created_at = demo.at(-DAYS - 1, 9)

    active = categories.list_active(db)
    stocks = [c for c in active if c.kind == "stock"]
    low = next((c for c in stocks if c.shortage_threshold), None)
    for category in active:
        if category.kind == "stock":
            _seed_stock(demo, category, category is low)
        else:
            _seed_flow(demo, category)
    _seed_slips(demo, stocks)
    _seed_bookings(demo, sorted(bookables.list_active(db), key=lambda t: (t.type, t.name)))
    votes = _seed_votes(demo, signer)
    db.flush()
    db.expire_all()
    for person in people:
        impact.sync(db, person, now=demo.last_moment)
    demo.quiet()
    return Seeded(len(people), demo.entries, demo.bookings, votes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fill the demo database with synthetic use.")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="the history ends the day before this date (default: today)",
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
    data_dir = Path(env.get("COMMONS_DATA_DIR", str(settings.data_dir)))
    engine = engine or make_engine(settings.database_url, lambda: read_secret("database_password"))
    reason = refusal(env, engine.url.database, data_dir)
    try:
        if reason:
            raise SeedRefused(reason)
        cast = parse_cast(env.get("COMMONS_DEMO_MEMBERS", ""))
        community_file = Path(env.get("COMMONS_COMMUNITY_FILE", str(settings.community_file)))
        config = community.load(community_file)
        server_name = env.get("COMMONS_MATRIX_SERVER_NAME", settings.matrix_server_name)
        timezone = ZoneInfo(settings.timezone)
        as_of = args.as_of or datetime.now(timezone).date()
        upgrade_database(engine)
        sessions = make_sessionmaker(engine)
        with sessions() as db, db.begin():
            signer = decision_log.init_signer(db, data_dir / "keys")
        with sessions() as db, db.begin():
            result = seed(db, config, signer, cast=cast, server_name=server_name, as_of=as_of)
    except (SeedRefused, community.CommunityConfigError) as e:
        print(f"seed-demo: refused, {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(
        f"seed-demo: {result.members} members, {result.entries} ledger entries, "
        f"{result.bookings} bookings, {result.votes} votes, ending {as_of - timedelta(days=1)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
