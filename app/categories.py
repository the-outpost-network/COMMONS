"""Resource categories: what the community keeps track of (water, firewood, ...).

The admin pages and the community.yaml seed both go through `CategorySpec` (one
set of rules for both) and the functions here. Invariants:

- the slug is fixed once created (chat, code and later the ledger refer to it);
- every word that names a category (slug, name, aliases) names only one
  category, compared case-insensitively, so "wood" can never be ambiguous;
- a category with history is archived, never deleted: `delete` only works while
  nothing points at it, and the database enforces that.

Callers check permissions and commit.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import KINDS, ResourceAlias, ResourceCategory, utcnow

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ALIAS_RE = re.compile(r"^[\w][\w .'-]{0,39}$")
MAX_ALIASES = 10


class CategoryError(ValueError):
    """A rule was broken. ``field`` names the form field it belongs to, if any."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class HasHistory(CategoryError):
    pass


def normalize_word(raw: str) -> str:
    return " ".join(raw.split()).lower()


class CategorySpec(BaseModel):
    """One category as written in community.yaml or sent by the admin form."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=60)
    unit: str = Field(min_length=1, max_length=20)
    kind: str
    shortage_threshold: float | None = Field(default=None, ge=0, le=1e9)
    capacity: float | None = Field(default=None, gt=0, le=1e9)
    forecast_horizon_days: int = Field(default=30, ge=1, le=365)
    aliases: list[str] = Field(default_factory=list, max_length=MAX_ALIASES)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError(
                "use 2 to 32 lower-case letters, digits or '-', starting with a letter"
            )
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"must be {' or '.join(KINDS)}")
        return v

    @field_validator("name", "unit")
    @classmethod
    def _one_line(cls, v: str) -> str:
        v = " ".join(v.split())
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("must be one line of text")
        return v

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            alias = normalize_word(raw)
            if not alias:
                continue
            if not ALIAS_RE.match(alias):
                raise ValueError(f"{raw!r}: use letters, digits, spaces, ' . or - (max 40)")
            if alias not in out:
                out.append(alias)
        return out

    @model_validator(mode="after")
    def _threshold_below_capacity(self) -> CategorySpec:
        if (
            self.shortage_threshold is not None
            and self.capacity is not None
            and self.shortage_threshold >= self.capacity
        ):
            raise ValueError("the shortage threshold must be lower than the capacity")
        # An alias equal to the slug or name adds nothing.
        own = {self.slug, normalize_word(self.name)}
        self.aliases = [a for a in self.aliases if a not in own]
        return self

    def words(self) -> set[str]:
        return {self.slug, normalize_word(self.name), *self.aliases}


def spec_errors(e: ValidationError) -> dict[str, str]:
    """Pydantic errors as {field: readable message} (field "" = whole category)."""
    out: dict[str, str] = {}
    for err in e.errors():
        loc = str(err["loc"][0]) if err["loc"] else ""
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "string_too_short":
            msg = "Required."
        elif err["type"] == "string_too_long":
            msg = f"At most {err['ctx']['max_length']} characters."
        # Shown under the field: always a sentence, like "Required.".
        msg = msg[:1].upper() + msg[1:]
        out.setdefault(loc, msg if msg.endswith((".", "?", "!")) else msg + ".")
    return out


def spec_from(category: ResourceCategory) -> CategorySpec:
    return CategorySpec(
        slug=category.slug,
        name=category.name,
        unit=category.unit,
        kind=category.kind,
        shortage_threshold=category.shortage_threshold,
        capacity=category.capacity,
        forecast_horizon_days=category.forecast_horizon_days,
        aliases=category.alias_names,
    )


def values(category: ResourceCategory) -> dict:
    """What the decision log records about a category (`config.changed` old and new)."""
    return {**spec_from(category).model_dump(mode="json"), "archived": not category.is_active}


# --- reading -----------------------------------------------------------------


def list_all(db: Session) -> list[ResourceCategory]:
    """Active categories first, each group in display order."""
    return list(
        db.scalars(
            select(ResourceCategory).order_by(
                ResourceCategory.archived_at.is_not(None),
                ResourceCategory.position,
                ResourceCategory.id,
            )
        )
    )


def list_active(db: Session) -> list[ResourceCategory]:
    return [c for c in list_all(db) if c.is_active]


def get(db: Session, category_id: int) -> ResourceCategory | None:
    return db.get(ResourceCategory, category_id)


def by_slug(db: Session, slug: str) -> ResourceCategory | None:
    return db.scalar(select(ResourceCategory).where(ResourceCategory.slug == slug))


def resolve(db: Session, text: str) -> ResourceCategory | None:
    """The active category a chat word means: its slug, name or an alias, any case."""
    word = normalize_word(text)
    if not word:
        return None
    for c in list_active(db):
        if word in {c.slug, normalize_word(c.name), *c.alias_names}:
            return c
    return None


# --- writing -----------------------------------------------------------------


def _clash(db: Session, spec: CategorySpec, exclude_id: int | None) -> CategoryError | None:
    for other in list_all(db):
        if other.id == exclude_id:
            continue
        if other.slug == spec.slug:
            return CategoryError(f"another resource already uses the id {spec.slug!r}", "slug")
        if normalize_word(other.name) == normalize_word(spec.name):
            return CategoryError(f"another resource is already called {other.name!r}", "name")
        taken = spec.words() & {other.slug, normalize_word(other.name), *other.alias_names}
        if taken:
            word = sorted(taken)[0]
            return CategoryError(f"{word!r} already means {other.name}", "aliases")
    return None


def _apply(category: ResourceCategory, spec: CategorySpec) -> None:
    category.name = spec.name
    category.unit = spec.unit
    category.kind = spec.kind
    category.shortage_threshold = spec.shortage_threshold
    category.capacity = spec.capacity
    category.forecast_horizon_days = spec.forecast_horizon_days
    wanted = list(spec.aliases)
    category.aliases = [a for a in category.aliases if a.alias in wanted] + [
        ResourceAlias(alias=a) for a in wanted if a not in category.alias_names
    ]


def create(db: Session, spec: CategorySpec) -> ResourceCategory:
    if (err := _clash(db, spec, None)) is not None:
        raise err
    last = db.scalar(select(func.max(ResourceCategory.position))) or 0
    category = ResourceCategory(slug=spec.slug, position=last + 1)
    _apply(category, spec)
    db.add(category)
    db.flush()
    return category


def update(db: Session, category: ResourceCategory, spec: CategorySpec) -> None:
    """Change everything except the slug, which is fixed once created."""
    if spec.slug != category.slug:
        raise CategoryError("the id of a resource can't be changed", "slug")
    if (err := _clash(db, spec, category.id)) is not None:
        raise err
    _apply(category, spec)
    # Removing an alias and adding it to another category in one go must not
    # trip the primary key: write the removals first.
    db.flush()


def archive(db: Session, category: ResourceCategory) -> None:
    """Hide a category from chat and the board; its history stays."""
    if category.archived_at is None:
        category.archived_at = utcnow()
        db.flush()


def restore(db: Session, category: ResourceCategory) -> None:
    if category.archived_at is not None:
        # Its words may have been taken by another category in the meantime.
        if (err := _clash(db, spec_from(category), category.id)) is not None:
            raise err
        category.archived_at = None
        db.flush()


def move(db: Session, category: ResourceCategory, step: int) -> None:
    """Swap with the neighbour above (step -1) or below (step +1) among its group."""
    group = [c for c in list_all(db) if c.is_active == category.is_active]
    for i, c in enumerate(group):
        c.position = i + 1
    i = group.index(category)
    j = i + step
    if 0 <= j < len(group):
        group[i].position, group[j].position = group[j].position, group[i].position
    db.flush()


def delete(db: Session, category: ResourceCategory) -> None:
    """Remove a category for good. Only while nothing refers to it (no entries)."""
    try:
        with db.begin_nested():
            db.delete(category)
            db.flush()
    except IntegrityError as e:
        raise HasHistory(f"{category.name} has entries; archive it instead") from e
