"""community.yaml: the community's own settings, read once to seed the database.

On the first start the app reads the file, checks it, and copies it into the
database; a `config_kv` row remembers that. From then on the database is the
source of truth (admins change it on the settings pages) and the file is not
even read, so a later typo in it can't stop a running box.

Resource categories go into their own table (app/categories.py), and so do
the bookable things (`bookable`, app/bookables.py; an old `spaces:` list
still works and means type "space"), and so do the badge rules (`badge_rule`,
app/impact.py). The `bookings` settings go into `config_kv` like the rest.
The other sections (community, roles, voting) are checked strictly and
stored as JSON in `config_kv` until the features that use them get tables.
Every seeded section is recorded as a signed `config.changed` entry in the
decision log, in the same transaction.

The `guide` section (what members should know about the place: house rules,
chores, waste, travel) goes into `config_kv` as well; the chat assistant puts it
into the model's system prompt so it can answer questions about the place.

Sections added after a box was first seeded are seeded once on their own: a box
seeded before bookings or the guide existed reads the file one more time for
them (`BOOKINGS_SEEDED_KEY`, `GUIDE_SEEDED_KEY`), and nothing else in it.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app import bookables, categories, decision_log
from app.bookables import BookingSettings, ThingSpec
from app.models import ROLES, BadgeRule, ConfigKV, utcnow

SEEDED_KEY = "community_yaml.seeded_at"
BOOKINGS_SEEDED_KEY = "community_yaml.bookings_seeded_at"
GUIDE_SEEDED_KEY = "community_yaml.guide_seeded_at"
GUIDE_KEY = "guide"
# Set by scripts/seed_demo.py: everything in this database is synthetic.
DEMO_KEY = "demo_data"
# The whole guide goes into every prompt of a 3B model with an 8k context:
# about 2,000 tokens at most, next to ~2,000 for the instructions and the chat.
GUIDE_MAX_CHARS = 8000
GUIDE_TOPIC_MAX_CHARS = 1500
NAME_KEY = "community.name"
TIMEZONE_KEY = "community.timezone"
VOTING_KEY = "voting"
MAX_BYTES = 256_000

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]
Name = Annotated[str, Field(min_length=1, max_length=60)]
Description = Annotated[str, Field(max_length=500)]
# Participation counters a badge may use (app/impact.py counts them). Never
# resource use: badges reward taking part, not consuming more.
BadgeCounter = Literal["ledger_entries", "ledger_days_in_a_row", "bookings_honoured", "votes"]


class CommunityConfigError(Exception):
    """community.yaml is missing or wrong. The message says where and why."""


class _Strict(BaseModel):
    # Unknown keys are errors, so a typo ("treshold") is not silently ignored.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Community(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    timezone: str = "Europe/Stockholm"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r} (use e.g. Europe/Stockholm)") from None
        return v


class Role(_Strict):
    slug: Slug
    name: Name


class Space(_Strict):
    slug: Slug
    name: Name
    description: Description = ""


class VoteType(_Strict):
    """A kind of vote: how long voting runs and when the counts are shown."""

    slug: Slug
    name: Name
    duration_days: Annotated[int, Field(ge=1, le=90)]
    # live: anonymous counts while voting; after_close: only turnout until it closes.
    results: Literal["live", "after_close"] = "live"
    # True: the proposer states an amount (or "amount unknown"), e.g. for spending.
    amount: bool = False


class Voting(_Strict):
    """Votes are secret: the log records who voted, never what they chose."""

    # None = no quorum: every vote counts however many members voted.
    quorum_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    # Default passing share: an option wins with MORE than this share of the votes
    # cast (abstentions don't count). The proposer may set another, at least 50.
    threshold_pct: Annotated[float, Field(ge=50, lt=100)]
    # Answers beyond the first two (e.g. "Wait until Monday").
    extra_options: Annotated[int, Field(ge=0, le=4)] = 2
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    # Local times on the closing day when the bot reminds members who haven't voted.
    reminder_times: Annotated[list[time], Field(max_length=4)] = [time(9, 0), time(18, 0)]
    # Reminders are only ever sent between these two local times.
    reminder_window: tuple[time, time] = (time(8, 0), time(22, 0))
    types: Annotated[list[VoteType], Field(min_length=1)]

    @field_validator("types")
    @classmethod
    def _unique_types(cls, v: list[VoteType]) -> list[VoteType]:
        return _unique_slugs(v)

    @model_validator(mode="after")
    def _reminders_inside_their_window(self) -> Voting:
        start, end = self.reminder_window
        if start >= end:
            raise ValueError("reminder_window must start before it ends")
        if any(not start <= t <= end for t in self.reminder_times):
            raise ValueError("every reminder time must lie inside reminder_window")
        return self

    @model_validator(mode="after")
    def _currency_for_amounts(self) -> Voting:
        if self.currency is None and any(t.amount for t in self.types):
            raise ValueError("a vote type asks for an amount, so set a currency (e.g. SEK)")
        return self


class BadgeRuleSpec(_Strict):
    count: BadgeCounter
    min: Annotated[int, Field(ge=1)]


class Badge(_Strict):
    slug: Slug
    name: Name
    description: Description = ""
    rule: BadgeRuleSpec


class GuideTopic(_Strict):
    title: Name
    text: Annotated[str, Field(min_length=1, max_length=GUIDE_TOPIC_MAX_CHARS)]


def _unique_slugs(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    for item in items:
        if item.slug in seen:
            raise ValueError(f"slug {item.slug!r} is used twice")
        seen.add(item.slug)
    return items


class CommunityFile(_Strict):
    community: Community = Field(default_factory=Community)
    # Optional sections: left out = not seeded (the features that use them aren't built yet).
    roles: list[Role] | None = None
    resource_categories: list[categories.CategorySpec] = Field(min_length=1)
    bookable: list[ThingSpec] = Field(default_factory=list)
    # The old name for bookable things of type "space"; still read.
    spaces: list[Space] = Field(default_factory=list)
    bookings: BookingSettings = Field(default_factory=BookingSettings)
    voting: Voting | None = None
    badges: list[Badge] = Field(default_factory=list)
    # What members should know about the place, for the chat assistant.
    guide: list[GuideTopic] = Field(default_factory=list)

    @field_validator("resource_categories", "spaces", "badges")
    @classmethod
    def _unique(cls, v: list[Any]) -> list[Any]:
        return _unique_slugs(v)

    @field_validator("guide")
    @classmethod
    def _guide_fits(cls, v: list[GuideTopic]) -> list[GuideTopic]:
        seen: set[str] = set()
        for topic in v:
            if topic.title.casefold() in seen:
                raise ValueError(f"the guide topic {topic.title!r} is there twice")
            seen.add(topic.title.casefold())
        total = sum(len(t.title) + len(t.text) for t in v)
        if total > GUIDE_MAX_CHARS:
            raise ValueError(
                f"the guide has {total} characters; keep it under {GUIDE_MAX_CHARS} "
                "so it fits the model's context"
            )
        return v

    @model_validator(mode="after")
    def _unique_thing_names(self) -> CommunityFile:
        seen: set[str] = set()
        for thing in self.all_things():
            if thing.name.casefold() in seen:
                raise ValueError(f"the name {thing.name!r} is used for two bookable things")
            seen.add(thing.name.casefold())
        return self

    def all_things(self) -> list[ThingSpec]:
        """`bookable`, then each entry of an old `spaces:` list as type "space"."""
        legacy = [
            ThingSpec(slug=s.slug, name=s.name, type="space", description=s.description)
            for s in self.spaces
        ]
        return [*self.bookable, *legacy]

    @field_validator("roles")
    @classmethod
    def _fixed_roles(cls, v: list[Role] | None) -> list[Role] | None:
        if v is None:
            return v
        _unique_slugs(v)
        # The database only knows these roles for now (CHECK on member_role.role).
        if {r.slug for r in v} != set(ROLES):
            raise ValueError(f"must be exactly {', '.join(ROLES)}; you can rename them")
        return v

    def config_values(self) -> dict[str, Any]:
        """config_kv key -> JSON value for everything except categories and bookings."""
        data = self.model_dump(mode="json")
        values: dict[str, Any] = {}
        if self.community.name:
            values[NAME_KEY] = " ".join(self.community.name.split())
        values[TIMEZONE_KEY] = self.community.timezone
        if self.roles is not None:
            values["roles"] = data["roles"]
        if self.voting is not None:
            values["voting"] = data["voting"]
        return values


def _where(loc: tuple, data: Any) -> str:
    """'resource_categories[1] (firewood).kind' for an error location."""
    parts: list[str] = []
    node = data
    for key in loc:
        if isinstance(key, int):
            label = f"[{key}]"
            if isinstance(node, list) and key < len(node) and isinstance(node[key], dict):
                slug = node[key].get("slug")
                if slug:
                    label += f" ({slug})"
            parts.append(label)
            node = node[key] if isinstance(node, list) and key < len(node) else None
        else:
            parts.append(("." if parts else "") + str(key))
            node = node.get(key) if isinstance(node, dict) else None
    return "".join(parts) or "(top level)"


def _message(err: dict) -> str:
    if err["type"] == "extra_forbidden":
        return "unknown setting (typo?)"
    if err["type"] == "string_pattern_mismatch":
        return "use lower-case letters, digits or '-', starting with a letter"
    msg = err["msg"].removeprefix("Value error, ")
    return msg.replace("Input should be", "must be")


def parse(text_: str, source: str = "community.yaml") -> CommunityFile:
    """Check the file; raise CommunityConfigError listing every problem."""
    try:
        data = yaml.safe_load(text_)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(e, "problem", None) or e
        raise CommunityConfigError(f"{source}{where}: not valid YAML: {problem}") from None
    if not isinstance(data, dict):
        raise CommunityConfigError(f"{source}: expected sections like resource_categories:")
    try:
        config = CommunityFile.model_validate(data)
    except ValidationError as e:
        problems = [
            f"  {_where(err['loc'], data)}: {_message(err)}" for err in e.errors(include_url=False)
        ]
        raise CommunityConfigError(f"{source} has errors:\n" + "\n".join(problems)) from None

    seen: dict[str, str] = {}
    for i, spec in enumerate(config.resource_categories):
        for word in sorted(spec.words()):
            if word in seen and seen[word] != spec.slug:
                raise CommunityConfigError(
                    f"{source}: resource_categories[{i}] ({spec.slug}): "
                    f"{word!r} already means {seen[word]}"
                )
            seen[word] = spec.slug
    return config


def load(path: Path) -> CommunityFile:
    if not path.is_file():
        hint = " (a directory: Docker creates one for a missing file)" if path.is_dir() else ""
        raise CommunityConfigError(
            f"{path} not found{hint}. Run `make init` (it copies community.yaml.example) and "
            "review the values before the first start."
        )
    if path.stat().st_size > MAX_BYTES:
        raise CommunityConfigError(f"{path} is larger than {MAX_BYTES} bytes")
    return parse(path.read_text(encoding="utf-8"), str(path))


def is_seeded(db: Session) -> bool:
    return db.get(ConfigKV, SEEDED_KEY) is not None


def is_demo(db: Session) -> bool:
    return db.get(ConfigKV, DEMO_KEY) is not None


def mark_demo(db: Session, signer: decision_log.Signer) -> None:
    """Record, also in the decision log, that this database holds synthetic data."""
    db.add(ConfigKV(key=DEMO_KEY, value_json=True))
    _log_seeded(db, signer, {DEMO_KEY: True})
    db.flush()


def demo_refusal(environment: str, database: str | None, marked: bool) -> str | None:
    """Why the app must not run on this database in this mode, or None if it may."""
    demo_data = marked or "demo" in (database or "").lower()
    if environment == "demo" and not demo_data:
        return "COMMONS_ENV is demo, but this is not the demo database"
    if environment != "demo" and demo_data:
        return "this is the demo database, which only runs with COMMONS_ENV=demo"
    return None


def seed(db: Session, config: CommunityFile, signer: decision_log.Signer) -> None:
    """Write the file's contents and their decision-log entries (caller commits)."""
    for spec in config.resource_categories:
        categories.create(db, spec)
    changes: dict[str, Any] = {
        "resource_categories": [s.model_dump(mode="json") for s in config.resource_categories]
    }
    for key, value in config.config_values().items():
        db.add(ConfigKV(key=key, value_json=value))
        changes[key] = value
    changes["badges"] = seed_badges(db, config)
    _log_seeded(db, signer, changes)
    db.add(ConfigKV(key=SEEDED_KEY, value_json=utcnow().isoformat()))
    seed_bookings(db, config, signer)
    seed_guide(db, config, signer)


def _log_seeded(db: Session, signer: decision_log.Signer, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        decision_log.append(
            db,
            signer,
            "config.changed",
            {"key": key, "old": None, "new": value, "source": "community.yaml"},
        )


def seed_badges(db: Session, config: CommunityFile) -> list[dict[str, Any]]:
    """Write the badge rules (caller logs and commits); returns them as logged."""
    logged = []
    for position, badge in enumerate(config.badges):
        value = badge.model_dump(mode="json")
        db.add(
            BadgeRule(
                slug=badge.slug,
                name=badge.name,
                description=badge.description,
                rule_json=value["rule"],
                position=position,
            )
        )
        logged.append(value)
    return logged


def _badge_values(rules: list[BadgeRule]) -> list[dict[str, Any]]:
    return [
        {"slug": r.slug, "name": r.name, "description": r.description, "rule": r.rule_json}
        for r in rules
    ]


def replace_badges(db: Session, config: CommunityFile, signer: decision_log.Signer) -> bool:
    """Make the active badge rules the file's (admin command line); False if unchanged.

    A rule with a known slug is updated (and restored if it was archived), a new
    one is added, and one missing from the file is archived, never deleted:
    badges members earned stay theirs, also when a rule gets stricter. Logged as
    `config.changed` with the old and the new rules (caller commits).
    """
    rules = list(db.scalars(select(BadgeRule).order_by(BadgeRule.position, BadgeRule.id)))
    old = _badge_values([r for r in rules if r.archived_at is None])
    by_slug = {r.slug: r for r in rules}
    now = utcnow()
    for position, badge in enumerate(config.badges):
        rule = by_slug.pop(badge.slug, None)
        if rule is None:
            rule = BadgeRule(slug=badge.slug)
            db.add(rule)
        rule.name = badge.name
        rule.description = badge.description
        rule.rule_json = badge.rule.model_dump(mode="json")
        rule.position = position
        rule.archived_at = None
    for rule in by_slug.values():
        if rule.archived_at is None:
            rule.archived_at = now
    db.flush()
    active = db.scalars(
        select(BadgeRule).where(BadgeRule.archived_at.is_(None)).order_by(BadgeRule.position)
    )
    new = _badge_values(list(active))
    if old == new:
        return False
    decision_log.append(
        db, signer, "config.changed", {"key": "badges", "old": old, "new": new, "source": "cli"}
    )
    return True


def bookings_seeded(db: Session) -> bool:
    return db.get(ConfigKV, BOOKINGS_SEEDED_KEY) is not None


def seed_bookings(db: Session, config: CommunityFile, signer: decision_log.Signer) -> None:
    """Write the bookable things and booking settings and their log entries (caller commits)."""
    try:
        for spec in config.all_things():
            bookables.create(db, spec)
    except bookables.BookablesError as e:
        raise CommunityConfigError(f"community.yaml: {e}") from None
    settings = config.bookings.model_dump(mode="json")
    db.add(ConfigKV(key=bookables.SETTINGS_KEY, value_json=settings))
    things = [bookables.values(t) for t in bookables.list_all(db)]
    _log_seeded(db, signer, {"bookable": things, bookables.SETTINGS_KEY: settings})
    db.add(ConfigKV(key=BOOKINGS_SEEDED_KEY, value_json=utcnow().isoformat()))
    db.flush()


def guide_seeded(db: Session) -> bool:
    return db.get(ConfigKV, GUIDE_SEEDED_KEY) is not None


def seed_guide(db: Session, config: CommunityFile, signer: decision_log.Signer) -> None:
    """Write the guide and its log entry (caller commits)."""
    topics = [t.model_dump(mode="json") for t in config.guide]
    db.add(ConfigKV(key=GUIDE_KEY, value_json=topics))
    _log_seeded(db, signer, {GUIDE_KEY: topics})
    db.add(ConfigKV(key=GUIDE_SEEDED_KEY, value_json=utcnow().isoformat()))
    db.flush()


def replace_guide(db: Session, config: CommunityFile, signer: decision_log.Signer) -> bool:
    """Replace the guide with the file's (admin command line); False if unchanged.

    Logged as `config.changed` with the old and the new text (caller commits).
    """
    new = [t.model_dump(mode="json") for t in config.guide]
    row = db.get(ConfigKV, GUIDE_KEY)
    old = None if row is None else row.value_json
    if old == new:
        return False
    if row is None:
        db.add(ConfigKV(key=GUIDE_KEY, value_json=new))
    else:
        row.value_json = new
    decision_log.append(
        db, signer, "config.changed", {"key": GUIDE_KEY, "old": old, "new": new, "source": "cli"}
    )
    if not guide_seeded(db):
        db.add(ConfigKV(key=GUIDE_SEEDED_KEY, value_json=utcnow().isoformat()))
    db.flush()
    return True


def guide_for_prompt(db: Session) -> str:
    """The guide as plain text for the model's system prompt."""
    topics = get(db, GUIDE_KEY) or []
    parts = [f"## {t['title']}\n{t['text']}" for t in topics if t.get("title") and t.get("text")]
    return "\n\n".join(parts)


def seed_once(engine: Engine, path: Path | None, log, signer: decision_log.Signer) -> None:
    """Seed the database from ``path`` unless that already happened.

    No path (tests) = nothing to do. Raises CommunityConfigError, which stops
    the app with a readable message, if seeding is needed and the file is wrong.
    The decision-log lock is taken first, so two starting processes can't both seed.
    """
    if path is None:
        return
    with Session(engine) as db, db.begin():
        db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": decision_log.LOG_LOCK_ID})
        if is_seeded(db) and bookings_seeded(db) and guide_seeded(db):
            log.info("community.yaml was seeded before; the database is the source of truth")
            upgrade_voting(db, path, signer, log)
            return
        config = load(path)
        if is_seeded(db):
            # Seeded before these sections existed: add only the missing ones.
            if not bookings_seeded(db):
                seed_bookings(db, config, signer)
                log.info("seeded bookable things from %s", path)
            if not guide_seeded(db):
                seed_guide(db, config, signer)
                log.info("seeded the guide from %s: %d topics", path, len(config.guide))
            upgrade_voting(db, path, signer, log)
            return
        seed(db, config, signer)
    log.info("seeded from %s: %d resource categories", path, len(config.resource_categories))


def upgrade_voting(db: Session, path: Path, signer: decision_log.Signer, log) -> bool:
    """Store voting rules on a box seeded before vote types existed (Nov 2026), once.

    A box seeded earlier holds `{default_duration_days, quorum_pct, threshold_pct}`,
    which proposals can no longer use, or no voting rules at all (seeded before
    they were copied). Only then is the file read again, and its `voting:` section
    replaces the old value (or fills the gap), signed as `config.changed`. Current
    rules are never touched, so the database stays the source of truth. A wrong
    file is logged, not fatal: the box keeps running without proposals.
    """
    row = db.get(ConfigKV, VOTING_KEY)
    if row is not None:
        try:
            Voting.model_validate(row.value_json)
            return False
        except ValidationError:
            pass
    problem = "outdated" if row is not None else "missing"
    try:
        config = load(path)
    except CommunityConfigError as e:
        log.warning(
            "the stored voting rules are %s and %s can't replace them: %s", problem, path, e
        )
        return False
    if config.voting is None:
        log.warning("the stored voting rules are %s and %s has no voting: section", problem, path)
        return False
    new = config.model_dump(mode="json")["voting"]
    if row is None:
        old = None
        db.add(ConfigKV(key=VOTING_KEY, value_json=new))
    else:
        old = row.value_json
        row.value_json = new
        row.updated_at = utcnow()
        row.updated_by = None
    decision_log.append(
        db,
        signer,
        "config.changed",
        {"key": VOTING_KEY, "old": old, "new": new, "source": "community.yaml"},
    )
    db.flush()
    log.info("stored the voting rules from %s (they were %s)", path, problem)
    return True


def get(db: Session, key: str) -> Any:
    """Current JSON value of a config key, or None."""
    row = db.get(ConfigKV, key)
    return None if row is None else row.value_json


def community_name(engine: Engine) -> str | None:
    with Session(engine) as db:
        return get(db, NAME_KEY)
