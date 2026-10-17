"""Table definitions (spec §05). Timestamps are ISO 8601 UTC text; ids are UUID text."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _id() -> Column:
    return Column("id", String(36), primary_key=True)


def _revision() -> Column:
    return Column("revision", Integer, nullable=False, server_default=text("1"))


def _stamps() -> list[Column]:
    return [Column("created_at", String, nullable=False), Column("updated_at", String, nullable=False)]


users = Table(
    "users",
    metadata,
    _id(),
    Column("display_name", Text, nullable=False),
    Column("timezone", Text, nullable=False),  # IANA name, e.g. America/New_York
    Column("app_lock_hash", Text),  # reserved; app lock is deferred (ADR 0001)
    *_stamps(),
)

spaces = Table(
    "spaces",
    metadata,
    _id(),
    Column("owner_user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("color", Text, nullable=False),
    Column("archived_at", String),
    _revision(),
    *_stamps(),
    CheckConstraint("type IN ('personal', 'work', 'family', 'custom')", name="type"),
)

space_memberships = Table(
    "space_memberships",
    metadata,
    Column("space_id", String(36), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    PrimaryKeyConstraint("space_id", "user_id"),
    CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="role"),
)

people = Table(
    "people",
    metadata,
    _id(),
    Column("space_id", String(36), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("display_name", Text, nullable=False),
    Column("color", Text, nullable=False),
    _revision(),
    *_stamps(),
)

tasks = Table(
    "tasks",
    metadata,
    _id(),
    Column("space_id", String(36), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
    Column("created_by", String(36), ForeignKey("users.id"), nullable=False),
    Column("assignee_person_id", String(36), ForeignKey("people.id", ondelete="SET NULL")),
    Column("title", Text, nullable=False),
    Column("notes", Text, nullable=False, server_default=""),
    Column("due_date", String),  # YYYY-MM-DD, or
    Column("due_at", String),  # UTC timestamp
    Column("repeat_mode", Text, nullable=False, server_default="none"),
    Column("rrule", Text),
    Column("repeat_interval_days", Integer),
    Column("chain_id", String(36), nullable=False),  # shared by every occurrence of a chore
    Column("previous_task_id", String(36), ForeignKey("tasks.id", ondelete="SET NULL")),
    Column("status", Text, nullable=False, server_default="open"),
    Column("completed_at", String),
    Column("sort_key", Text, nullable=False),  # due day (or 9999-12-31) + created_at, for keyset paging
    _revision(),
    *_stamps(),
    CheckConstraint("status IN ('open', 'done', 'archived')", name="status"),
    CheckConstraint("NOT (due_date IS NOT NULL AND due_at IS NOT NULL)", name="one_due"),
    CheckConstraint(
        "(repeat_mode = 'none' AND rrule IS NULL AND repeat_interval_days IS NULL)"
        " OR (repeat_mode = 'schedule' AND rrule IS NOT NULL AND repeat_interval_days IS NULL"
        "     AND (due_date IS NOT NULL OR due_at IS NOT NULL))"
        " OR (repeat_mode = 'after_completion' AND rrule IS NULL AND repeat_interval_days >= 1)",
        name="repeat",
    ),
    Index(None, "space_id", "status", "sort_key"),
)

lists = Table(
    "lists",
    metadata,
    _id(),
    Column("space_id", String(36), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("name", Text, nullable=False),
    Column("archived_at", String),
    _revision(),
    *_stamps(),
)

list_items = Table(
    "list_items",
    metadata,
    _id(),
    Column("list_id", String(36), ForeignKey("lists.id", ondelete="CASCADE"), nullable=False),
    Column("text", Text, nullable=False),
    Column("checked", Boolean, nullable=False, server_default=text("0")),
    Column("position", Float, nullable=False),
    _revision(),
    *_stamps(),
    Index(None, "list_id", "position"),
)

events = Table(
    "events",
    metadata,
    _id(),
    Column("space_id", String(36), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("location", Text, nullable=False, server_default=""),
    Column("start_at", String),
    Column("end_at", String),
    Column("start_date", String),
    Column("end_date", String),  # exclusive
    Column("timezone", Text, nullable=False),
    Column("rrule", Text),
    Column("series_id", String(36), ForeignKey("events.id", ondelete="CASCADE")),
    Column("original_start", String),
    Column("status", Text, nullable=False, server_default="confirmed"),
    Column("deleted_at", String),
    _revision(),
    *_stamps(),
    CheckConstraint("status IN ('confirmed', 'cancelled')", name="status"),
    CheckConstraint(
        "(start_at IS NOT NULL AND end_at IS NOT NULL AND start_date IS NULL AND end_date IS NULL)"
        " OR (start_date IS NOT NULL AND end_date IS NOT NULL AND start_at IS NULL AND end_at IS NULL)",
        name="timing",
    ),
    CheckConstraint("(series_id IS NULL) = (original_start IS NULL)", name="exception_key"),
    CheckConstraint("NOT (series_id IS NOT NULL AND rrule IS NOT NULL)", name="exception_no_rule"),
    Index(None, "space_id", "start_at"),
    Index(None, "space_id", "start_date"),
    Index(
        "uq_events_series_original_start",
        "series_id",
        "original_start",
        unique=True,
        sqlite_where=text("series_id IS NOT NULL"),
    ),
)

jobs = Table(
    "jobs",
    metadata,
    _id(),
    Column("kind", Text, nullable=False),
    Column("payload", Text, nullable=False),  # JSON
    Column("schema_version", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", String, nullable=False),
    Column("lease_until", String),
    Column("idempotency_key", Text),
    Column("last_error_category", Text),
    *_stamps(),
    CheckConstraint("state IN ('queued', 'leased', 'succeeded', 'dead_letter', 'cancelled')", name="state"),
    Index("ix_jobs_claim", "state", "available_at"),
    Index(
        "uq_jobs_idempotency",
        "kind",
        "idempotency_key",
        unique=True,
        sqlite_where=text("idempotency_key IS NOT NULL"),
    ),
)

outbox = Table(
    "outbox",
    metadata,
    _id(),
    Column("event_type", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("envelope_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("processed_at", String),
    Index("ix_outbox_pending", "created_at", sqlite_where=text("processed_at IS NULL")),
)

audit_log = Table(
    "audit_log",
    metadata,
    _id(),
    Column("actor_user_id", String(36), nullable=False),
    Column("space_id", String(36)),
    Column("action", Text, nullable=False),
    Column("object_type", Text, nullable=False),
    Column("object_id", String(36), nullable=False),
    Column("timestamp", String, nullable=False),
)
