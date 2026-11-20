"""Database tables: members and sessions, resources, proposals and votes, the audit log.

Matrix handles authentication, so COMMONS stores no passwords: a member is
identified by their Matrix user id.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ROLES = ("member", "board", "admin")
CHANNELS = ("web", "matrix")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """``timestamptz`` that refuses naive datetimes and returns UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value

    def process_result_value(self, value, dialect):
        return None if value is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Member(Base):
    __tablename__ = "member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    matrix_user_id: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    roles: Mapped[list[MemberRole]] = relationship(
        back_populates="member", foreign_keys="MemberRole.member_id", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None

    @property
    def role_names(self) -> set[str]:
        return {r.role for r in self.roles}


class MemberRole(Base):
    __tablename__ = "member_role"
    __table_args__ = (
        CheckConstraint("role IN ('member', 'board', 'admin')", name="ck_member_role_role"),
    )

    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # NULL when granted from the command line (e.g. bootstrapping the first admin).
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("member.id"))

    member: Mapped[Member] = relationship(back_populates="roles", foreign_keys=[member_id])


class Session(Base):
    """A dashboard login. Only the SHA-256 of the cookie value is stored."""

    __tablename__ = "session"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    member: Mapped[Member] = relationship(lazy="joined")


class AuthEvent(Base):
    """Account history: sign-ins, failures, logouts and profile changes."""

    __tablename__ = "auth_event"
    __table_args__ = (
        CheckConstraint("channel IN ('web', 'matrix')", name="ck_auth_event_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    # login, login_failed, logout, logout_everywhere, display_name_changed
    kind: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(10), default="web")


class EventFeed(Base):
    """What happened, one short line per event, for the COMMONS room (and later the board).

    Written in the same transaction as the change it describes (app/feed.py), so a
    line exists exactly when the change does. The id is the event-stream id the bot
    resumes from after a reconnect. Summaries are written by code, never by the model.
    """

    __tablename__ = "event_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    summary: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class ConfigKV(Base):
    """Community configuration and small installation facts, one JSON value per key.

    Seeded once from community.yaml (app/community.py), then managed by admins.
    """

    __tablename__ = "config_kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("member.id"))


class SigningKey(Base):
    """Public verification keys; the private key stays in the mounted data volume."""

    __tablename__ = "signing_key"

    key_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    public_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class DecisionLog(Base):
    """Append-only signed audit chain."""

    __tablename__ = "decision_log"
    __table_args__ = (
        CheckConstraint(
            "payload_json IS NOT NULL AND jsonb_typeof(payload_json) = 'object'",
            name="ck_decision_log_payload_object",
        ),
        CheckConstraint(
            "prev_hash ~ '^[0-9a-f]{64}$'",
            name="ck_decision_log_prev_hash_hex",
        ),
        CheckConstraint(
            "entry_hash ~ '^[0-9a-f]{64}$'",
            name="ck_decision_log_entry_hash_hex",
        ),
        CheckConstraint(
            "signature ~ '^[0-9a-f]{128}$'",
            name="ck_decision_log_signature_hex",
        ),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    kind: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)
    signature: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str] = mapped_column(ForeignKey("signing_key.key_id"))


class Invite(Base):
    """A sign-up invite: one MAS registration token, single use, short-lived.

    MAS checks the token at sign-up; COMMONS keeps only its SHA-256 (to recognise
    the /join/<code> link) and MAS' token id (to revoke it or ask whether it was
    used). The code itself is shown to the admin once and never stored.
    """

    __tablename__ = "invite"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    mas_token_id: Mapped[str] = mapped_column(String(64))
    # Optional reminder for admins ("for Nora"); never shown on the join page.
    note: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[int] = mapped_column(ForeignKey("member.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    creator: Mapped[Member] = relationship(lazy="joined")

    def is_open(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


# --- Resources and community config ------------------------------------------

KINDS = ("stock", "flow")


class ResourceCategory(Base):
    """Something the community keeps track of, e.g. water (l) or firewood (m³).

    Seeded once from community.yaml, then managed by admins. The level is never
    stored here: it is derived from the ledger entries (D2). Categories with
    history are archived, never deleted (the database refuses the delete as
    soon as an entry points at them).
    """

    __tablename__ = "resource_category"
    __table_args__ = (
        CheckConstraint("kind IN ('stock', 'flow')", name="ck_resource_category_kind"),
        CheckConstraint(
            "shortage_threshold IS NULL OR shortage_threshold >= 0",
            name="ck_resource_category_threshold",
        ),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_resource_category_capacity"),
        CheckConstraint(
            "forecast_horizon_days BETWEEN 1 AND 365", name="ck_resource_category_horizon"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Fixed id used by chat and code ("water"); never changes after creation.
    slug: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    unit: Mapped[str] = mapped_column(String(20))
    # stock: a level that is read and topped up; flow: only use and add events.
    kind: Mapped[str] = mapped_column(String(10))
    # Below this level the community is warned (D7). NULL = no warning.
    shortage_threshold: Mapped[float | None] = mapped_column(Float)
    # Full tank / full woodshed, for the level bar. NULL = unknown.
    capacity: Mapped[float | None] = mapped_column(Float)
    forecast_horizon_days: Mapped[int] = mapped_column(Integer, default=30)
    # Display order on the board and in settings (lower first).
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    aliases: Mapped[list[ResourceAlias]] = relationship(
        back_populates="category",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="ResourceAlias.alias",
    )

    @property
    def is_active(self) -> bool:
        return self.archived_at is None

    @property
    def alias_names(self) -> list[str]:
        return [a.alias for a in self.aliases]


class ForecastRun(Base):
    """One shortage forecast for a resource (app/forecast.py, migration 0015).

    Keeps the data basis (days and entries used, the period), the parameters the
    model picked and the result, so any forecast shown can be traced back.
    `flagged`: this run's shortage was posted to the COMMONS room.
    """

    __tablename__ = "forecast_run"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'insufficient_data')", name="ck_forecast_run_status"),
        CheckConstraint("NOT flagged OR shortage_on IS NOT NULL", name="ck_forecast_run_flag"),
        Index("ix_forecast_run_category", "category_id", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("resource_category.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20))
    points: Mapped[int] = mapped_column(Integer)  # days in the series
    entries: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    method: Mapped[str | None] = mapped_column(String(20))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    shortage_on: Mapped[date | None] = mapped_column(Date)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ResourceAlias(Base):
    """Another word members use in chat for a category ("wood" -> firewood).

    Stored normalized (lower case, single spaces); the primary key makes an
    alias unique across all categories.
    """

    __tablename__ = "resource_alias"

    alias: Mapped[str] = mapped_column(String(40), primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("resource_category.id", ondelete="CASCADE"), index=True
    )

    category: Mapped[ResourceCategory] = relationship(back_populates="aliases")


# --- Ledger ------------------------------------------------------------------

ENTRY_TYPES = ("reading", "use", "add", "correction")
ENTRY_CHANNELS = ("web", "matrix", "sensor")
EDIT_WINDOW_MINUTES = 15


class ResourceEntry(Base):
    """One thing a member logged: a reading, a use, a refill, or a correction.

    Never deleted. The author may edit it for 15 minutes; after that only a
    `correction` row pointing at the original changes what it counts for. Both
    rules are also enforced by a PostgreSQL trigger (see below), so no code path
    can rewrite history. A `void` correction undoes an entry: it no longer counts.
    Levels are derived from these rows (app/ledger.py).
    """

    __tablename__ = "resource_entry"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('reading', 'use', 'add', 'correction')",
            name="ck_resource_entry_type",
        ),
        CheckConstraint("channel IN ('web', 'matrix', 'sensor')", name="ck_resource_entry_channel"),
        CheckConstraint("amount >= 0", name="ck_resource_entry_amount"),
        # A correction always points at the entry it fixes; nothing else does.
        CheckConstraint(
            "(entry_type = 'correction') = (corrects_id IS NOT NULL)",
            name="ck_resource_entry_corrects",
        ),
        # Only a correction can say "this entry didn't happen" (undo).
        CheckConstraint("NOT void OR entry_type = 'correction'", name="ck_resource_entry_void"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("resource_category.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10))
    entry_type: Mapped[str] = mapped_column(String(12))
    amount: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(140))
    # When it happened (members may log a reading from this morning).
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # When it was written; the edit window counts from here.
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    corrects_id: Mapped[int | None] = mapped_column(ForeignKey("resource_entry.id"), index=True)
    # On a correction: the entry it points at no longer counts at all (undo).
    void: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    category: Mapped[ResourceCategory] = relationship(lazy="joined")
    member: Mapped[Member] = relationship(lazy="joined")
    corrects: Mapped[ResourceEntry | None] = relationship(remote_side=[id], lazy="joined")


# The database itself refuses to rewrite history: no DELETE at all, and UPDATE
# only inside the edit window (EDIT_WINDOW_MINUTES). The trigger is created by
# migration 0002_resources (resource_entry_guard) and extended in 0003_entry_void.


# --- Governance ----------------------------------------------------------------

PROPOSAL_STATUSES = ("open", "closed", "cancelled")
VOTE_CHANNELS = ("web", "matrix")
MIN_OPTIONS, MAX_OPTIONS = 2, 6
# Passing share: the proposer may set it per vote, never below a majority (with
# "more than 50 %" at most one option can pass).
MIN_THRESHOLD_PCT, MAX_THRESHOLD_PCT = 50, 100


class Proposal(Base):
    """A question any member puts to the others, with 2-6 explicit options.

    No discussion: question, options, votes, result. The vote type (from the
    voting config) sets the voting period and whether counts show while voting;
    it and the rule set (quorum, threshold) are copied when the proposal opens
    (the proposer may raise the threshold, never below 50 %),
    so a later config change can't alter a live vote. Never deleted; the
    database refuses changing the question, amount or rules, and status only
    leaves `open` once (trigger `proposal_guard`, migration 0005_governance).
    """

    __tablename__ = "proposal"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_proposal_status"),
        CheckConstraint("closes_at > opens_at", name="ck_proposal_window"),
        CheckConstraint(
            "quorum_pct IS NULL OR quorum_pct BETWEEN 0 AND 100", name="ck_proposal_quorum"
        ),
        CheckConstraint(
            "threshold_pct >= 50 AND threshold_pct < 100", name="ck_proposal_threshold"
        ),
        CheckConstraint("results IN ('live', 'after_close')", name="ck_proposal_results"),
        CheckConstraint(
            "has_amount OR (amount IS NULL AND currency IS NULL)", name="ck_proposal_no_amount"
        ),
        CheckConstraint(
            "NOT has_amount OR coalesce(currency ~ '^[A-Z]{3}$', false)",
            name="ck_proposal_currency",
        ),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_proposal_amount"),
        CheckConstraint("length(btrim(question)) > 0", name="ck_proposal_question"),
        CheckConstraint("(status = 'open') = (ended_at IS NULL)", name="ck_proposal_ended"),
        CheckConstraint("(status = 'closed') = (outcome IS NOT NULL)", name="ck_proposal_outcome"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('decided', 'no_votes', 'no_quorum', 'tie', 'below_threshold')",
            name="ck_proposal_outcome_kind",
        ),
        CheckConstraint(
            "coalesce(outcome = 'decided', false) = (outcome_option_id IS NOT NULL)",
            name="ck_proposal_outcome_option",
        ),
        CheckConstraint(
            "(status = 'cancelled') = (coalesce(length(btrim(cancel_reason)), 0) > 0)",
            name="ck_proposal_cancel_reason",
        ),
        ForeignKeyConstraint(
            ["id", "outcome_option_id"],
            ["proposal_option.proposal_id", "proposal_option.id"],
            name="fk_proposal_outcome_option",
            use_alter=True,
        ),
    )

    # Explicit: the outcome FK (id, outcome_option_id) would otherwise hide the sequence.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(140))
    why: Mapped[str] = mapped_column(String(1000), default="")
    vote_type: Mapped[str] = mapped_column(String(40))  # slug from the voting config
    # live: anonymous counts while voting; after_close: turnout only until it closes.
    results: Mapped[str] = mapped_column(String(12), default="live")
    # The type asks for an amount; amount NULL then means "amount unknown".
    has_amount: Mapped[bool] = mapped_column(Boolean, default=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    created_by: Mapped[int] = mapped_column(ForeignKey("member.id"))
    # Members and the decision log aren't told who proposed it; `created_by` still is.
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    opens_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closes_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Share of active members who must vote; NULL = no quorum.
    quorum_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # The winning option needs MORE than this share of the votes cast.
    threshold_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    # When and by whom it was closed or cancelled (NULL by = closed by the scheduler).
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_by: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    # On a closed proposal: decided, no_votes, no_quorum, tie or below_threshold.
    outcome: Mapped[str | None] = mapped_column(String(20))
    outcome_option_id: Mapped[int | None] = mapped_column(Integer)
    cancel_reason: Mapped[str | None] = mapped_column(String(500))

    creator: Mapped[Member] = relationship(lazy="joined", foreign_keys=[created_by])
    options: Mapped[list[ProposalOption]] = relationship(
        back_populates="proposal",
        lazy="selectin",
        order_by="ProposalOption.position",
        primaryjoin="Proposal.id == ProposalOption.proposal_id",
        foreign_keys="ProposalOption.proposal_id",
    )


class ProposalOption(Base):
    """One answer to a proposal, at position 1-6. Fixed once written."""

    __tablename__ = "proposal_option"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 6", name="ck_proposal_option_position"),
        CheckConstraint("length(btrim(label)) > 0", name="ck_proposal_option_label"),
        UniqueConstraint("proposal_id", "position", name="uq_proposal_option_position"),
        UniqueConstraint("proposal_id", "id", name="uq_proposal_option_proposal_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id"))
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200))

    proposal: Mapped[Proposal] = relationship(
        back_populates="options",
        primaryjoin="Proposal.id == ProposalOption.proposal_id",
        foreign_keys=[proposal_id],
    )


class ChatNotification(Base):
    """A vote message for the Matrix bot to send (outbox, migration 0006)."""

    __tablename__ = "chat_notification"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('opened', 'reminder', 'closed', 'cancelled', 'welcome')",
            name="ck_chat_notification_kind",
        ),
        CheckConstraint(
            "(kind = 'welcome') = (member_id IS NOT NULL AND proposal_id IS NULL)",
            name="ck_chat_notification_subject",
        ),
        Index(
            "uq_chat_notification_welcome",
            "member_id",
            unique=True,
            postgresql_where=text("kind = 'welcome'"),
        ),
        UniqueConstraint(
            "proposal_id", "kind", "reminder_minute", name="uq_chat_notification_proposal_kind"
        ),
        Index(
            "ix_chat_notification_pending",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # A vote message names its vote; a welcome (migration 0016) names the member.
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposal.id"))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    kind: Mapped[str] = mapped_column(String(10))
    # Reminders: local time as minutes after midnight (540 = 09:00). 0 for other kinds.
    reminder_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The model's checked opening line. NULL: not written yet; empty: use the fixed one.
    intro: Mapped[str | None] = mapped_column(String(200))
    # Cancelled votes: the model's clearer wording. The member's words stay on the proposal.
    reworded_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ProposalMessage(Base):
    """A Matrix message that shows a vote (migration 0015).

    `room`: the announcement in the COMMONS room; results reply to it.
    `direct`: the vote message in one member's direct chat with the bot; a tap
    on an option there is that member's ballot, and only theirs.
    """

    __tablename__ = "proposal_message"
    __table_args__ = (
        CheckConstraint("kind IN ('room', 'direct')", name="ck_proposal_message_kind"),
        CheckConstraint(
            "(kind = 'direct') = (member_id IS NOT NULL)", name="ck_proposal_message_member"
        ),
        UniqueConstraint("event_id", name="uq_proposal_message_event"),
        Index("ix_proposal_message_proposal", "proposal_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id"))
    kind: Mapped[str] = mapped_column(String(10))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    room_id: Mapped[str] = mapped_column(String(255))
    event_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Vote(Base):
    """A member's secret ballot: never shown with a name, deleted or rewritten.

    `option_id` NULL is an abstention (turnout, not a vote cast). This row is the
    only link between a member and their choice; the decision log records only
    that they voted.

    Changing a vote sets `superseded_at` on the current row and adds a new one
    (in that order, in one transaction). The database keeps one current vote
    per (proposal, member), lets `superseded_at` be set only once, and checks
    the option belongs to the proposal (migration 0005_governance).
    """

    __tablename__ = "vote"
    __table_args__ = (
        CheckConstraint("channel IN ('web', 'matrix')", name="ck_vote_channel"),
        CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= cast_at", name="ck_vote_superseded"
        ),
        ForeignKeyConstraint(
            ["proposal_id", "option_id"],
            ["proposal_option.proposal_id", "proposal_option.id"],
            name="fk_vote_option_of_proposal",
        ),
        Index(
            "uq_vote_current",
            "proposal_id",
            "member_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id"))
    option_id: Mapped[int | None] = mapped_column(Integer)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10))
    cast_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    member: Mapped[Member] = relationship(lazy="joined")
    option: Mapped[ProposalOption | None] = relationship(
        primaryjoin="and_(Vote.option_id == ProposalOption.id, "
        "Vote.proposal_id == ProposalOption.proposal_id)",
        foreign_keys=[proposal_id, option_id],
        viewonly=True,
        lazy="joined",
    )


# --- Bookable and bookings ------------------------------------------------

# What a bookable thing is; decides what a booking of it asks for (a vehicle: where to).
THING_TYPES = ("space", "vehicle", "tool")
BOOKING_CHANNELS = ("web", "matrix")
OVERLAP_CONSTRAINT = "ex_booking_overlap"


class Bookable(Base):
    """One bookable thing members can book: the theatre, the car, the saw.

    Where it is belongs to the thing, never to a booking. Archived, never
    deleted once it has bookings.
    """

    __tablename__ = "bookable"
    __table_args__ = (
        CheckConstraint("type IN ('space', 'vehicle', 'tool')", name="ck_bookable_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Made from the name when created, then fixed (the decision log refers to it).
    slug: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(10))
    # Where it is, e.g. "the barn".
    location: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def is_active(self) -> bool:
        return self.archived_at is None


class Booking(Base):
    """Who has a bookable thing from when, and (as a hint) until when.

    `ends_at` is optional and only a hint: COMMONS can't make anyone give a
    thing back, it reminds them. It stays what the member said; `returned_at`
    is when the thing really came back. `blocks_until` is the end the overlap
    check uses: `ends_at`, or for a booking without an end its start plus the
    community's open-booking window (2 hours by default); a return moves it to
    the return time if that is earlier, so the rest is free again. The PostgreSQL
    exclusion constraint `ex_booking_overlap` rejects two bookings of one thing
    whose [starts_at, blocks_until) overlap unless one is cancelled, also when
    two members book at the same moment.

    Never deleted. A trigger (migration 0005_bookings) allows changing the
    times, the note and the destination while the booking is neither cancelled
    nor given back, and setting `returned_at` and the cancel fields once.
    """

    __tablename__ = "booking"
    __table_args__ = (
        CheckConstraint("channel IN ('web', 'matrix')", name="ck_booking_channel"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="ck_booking_times"),
        CheckConstraint(
            "blocks_until > starts_at AND (ends_at IS NULL OR blocks_until = ends_at "
            "OR returned_at IS NOT NULL)",
            name="ck_booking_blocks",
        ),
        CheckConstraint(
            "(cancelled_at IS NULL) = (cancelled_by IS NULL)", name="ck_booking_cancelled_by"
        ),
        CheckConstraint(
            "cancelled_at IS NULL OR returned_at IS NULL", name="ck_booking_cancel_or_return"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bookable_id: Mapped[int] = mapped_column(ForeignKey("bookable.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    blocks_until: Mapped[datetime] = mapped_column(UTCDateTime)
    # "cutting firewood"; shown to everyone, like the booking itself.
    note: Mapped[str | None] = mapped_column(String(200))
    # Vehicles only: where the member is going, so others can ask for a lift or an errand.
    destination: Mapped[str | None] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    returned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("member.id"))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))

    bookable: Mapped[Bookable] = relationship(lazy="joined")
    member: Mapped[Member] = relationship(lazy="joined", foreign_keys=[member_id])

    @property
    def is_active(self) -> bool:
        return self.cancelled_at is None


# --- Phone calendar ------------------------------------------------------------


class CalendarToken(Base):
    """A member's secret phone-calendar link (ICS). Only the SHA-256 of the token is stored.

    One active link per member (partial unique index); a new one revokes the old.
    The link stops working when it is revoked or the member is deactivated.
    """

    __tablename__ = "calendar_token"
    __table_args__ = (
        Index(
            "uq_calendar_token_active",
            "member_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    member: Mapped[Member] = relationship(lazy="joined")


class BadgeRule(Base):
    """A participation badge: what it is called and the rule that earns it.

    Seeded from the `badges:` section of community.yaml (app/community.py);
    `rule_json` is `{"count": <counter>, "min": <n>}` with a counter from
    `app.impact.COUNTERS`, all of which count taking part, never resource use.
    Archived, never deleted: badges already earned stay with their members.
    """

    __tablename__ = "badge_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(500), default="")
    rule_json: Mapped[Any] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class MemberBadge(Base):
    """A badge a member earned; `awarded_at` is when they reached the rule, not when seen."""

    __tablename__ = "member_badge"

    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), primary_key=True)
    badge_rule_id: Mapped[int] = mapped_column(ForeignKey("badge_rule.id"), primary_key=True)
    awarded_at: Mapped[datetime] = mapped_column(UTCDateTime)
