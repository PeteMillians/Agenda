"""Provider-neutral shapes the sync engine works with. Each provider adapter translates its own
JSON to and from these, so pull, push, and conflict handling are written once (spec §10)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy.engine import RowMapping

from app.application.events import Timing

CONTENT_FIELDS = ("title", "description", "location", "timezone")
TIMING_FIELDS = ("start_at", "end_at", "start_date", "end_date")


class Unsupported(Exception):
    """An event Agenda cannot represent faithfully; it is skipped and counted, not guessed at."""


@dataclass(frozen=True)
class Grant:
    access_token: str
    expires_at: datetime
    refresh_token: str | None
    scopes: list[str]
    subject: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class CalendarInfo:
    provider_id: str
    name: str
    color: str
    timezone: str
    access_role: str  # owner | writer | reader | freeBusyReader
    is_primary: bool


@dataclass(frozen=True)
class Incoming:
    """One event as the provider has it now."""

    provider_id: str
    etag: str | None
    cancelled: bool
    series_provider_id: str | None = None  # set on instance overrides
    original_key: str | None = None  # occurrence key the override replaces
    content: dict[str, str] = field(default_factory=dict)  # title, description, location, timezone
    timing: dict[str, str | None] | None = None  # start_at/end_at or start_date/end_date
    rrule: str | None = None
    exdate_keys: tuple[str, ...] = ()

    @property
    def is_instance(self) -> bool:
        return self.series_provider_id is not None


@dataclass(frozen=True)
class Changes:
    items: list[Incoming]
    cursor: str | None  # store for the next incremental sync; null when the provider has none
    full: bool  # a complete listing: anything mapped but not listed is gone
    skipped: int = 0  # events the adapter could not represent
    dropped: tuple[str, ...] = ()  # provider ids now unrepresentable: remove any copy imported earlier
    # occurrences the listing covers (Microsoft lists instances in a window); null means all of them
    window: tuple[date, date] | None = None


@dataclass(frozen=True)
class Written:
    provider_id: str
    etag: str | None


@dataclass(frozen=True)
class Outgoing:
    """A local row as it should look at the provider."""

    title: str
    description: str
    location: str
    timezone: str
    timing: Timing
    rrule: str | None = None
    exdate_keys: tuple[str, ...] = ()  # cancelled occurrences of a series without an instance of their own
    cancelled: bool = False
    original_key: str | None = None

    @classmethod
    def of(cls, row: Mapping[str, Any], exdate_keys: Sequence[str] = ()) -> Outgoing:
        return cls(
            title=row["title"],
            description=row["description"],
            location=row["location"],
            timezone=row["timezone"],
            timing=Timing.of(row),
            rrule=row["rrule"],
            exdate_keys=tuple(sorted(exdate_keys)),
            cancelled=row["status"] == "cancelled",
            original_key=row["original_start"],
        )


class CalendarProvider(Protocol):
    """What the sync engine needs from a provider. Implementations raise the errors in errors.py."""

    name: str

    def instance_window(self) -> tuple[date, date] | None:
        """The days whose occurrences `changes` and `fetch` report, or None for all of them."""
        ...

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, *, write: bool) -> str: ...
    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Grant: ...
    async def refresh(self, refresh_token: str) -> Grant: ...
    async def revoke(self, token: str) -> None: ...
    @staticmethod
    def can_write(scopes: Sequence[str]) -> bool: ...

    async def list_calendars(self, token: str) -> list[CalendarInfo]: ...
    async def changes(self, token: str, source: RowMapping, cursor: str | None) -> Changes: ...
    async def fetch(self, token: str, source: RowMapping, provider_id: str) -> Incoming | None: ...
    async def insert(self, token: str, source: RowMapping, out: Outgoing) -> Written: ...
    async def update(
        self, token: str, source: RowMapping, provider_id: str, etag: str | None, out: Outgoing
    ) -> Written: ...
    async def delete(self, token: str, source: RowMapping, provider_id: str, etag: str | None) -> None: ...
    async def write_instance(
        self,
        token: str,
        source: RowMapping,
        master_id: str,
        master: Outgoing,
        out: Outgoing,
        instance_id: str | None,
        etag: str | None,
    ) -> Written:
        """Create or update the override of occurrence `out.original_key`. With `out.cancelled`,
        cancel that occurrence instead."""
        ...

    def check_rule(self, rule: str, first: Timing, timezone: str) -> None:
        """Raise Unrepresentable if the provider cannot store this recurrence rule."""
        ...


# --------------------------------------------------------------------------- snapshots


def _snapshot(
    values: Mapping[str, Any],
    *,
    cancelled: bool,
    deleted: bool,
    original_start: str | None,
    parent: str | None,
    exdates: Sequence[str],
) -> dict[str, Any]:
    """The content both sides must agree on. Version tags and bookkeeping are left out, so a new
    ETag with the same content is not a change, and a cancelled occurrence is just its key."""
    if deleted:
        return {"deleted": True}
    if cancelled:
        return {"status": "cancelled", "original_start": original_start, "parent": parent}
    return {
        **{f: values.get(f) for f in (*CONTENT_FIELDS, *TIMING_FIELDS, "rrule")},
        "status": "confirmed",
        "original_start": original_start,
        "parent": parent,
        "exdates": sorted(exdates),
    }


def row_snapshot(
    row: Mapping[str, Any], *, parent: str | None = None, exdates: Sequence[str] = ()
) -> dict[str, Any]:
    return _snapshot(
        row,
        cancelled=row["status"] == "cancelled",
        deleted=row["deleted_at"] is not None,
        original_start=row["original_start"],
        parent=parent,
        exdates=exdates,
    )


def incoming_snapshot(incoming: Incoming | None, *, original_start: str | None = None) -> dict[str, Any]:
    """`None` means the provider no longer has the event."""
    if incoming is None or (incoming.cancelled and not incoming.is_instance):
        return {"deleted": True}
    return _snapshot(
        {**incoming.content, **(incoming.timing or {}), "rrule": incoming.rrule},
        cancelled=incoming.cancelled,
        deleted=False,
        original_start=original_start if original_start is not None else incoming.original_key,
        parent=incoming.series_provider_id,
        exdates=incoming.exdate_keys,
    )


def canonical(snapshot: Mapping[str, Any]) -> str:
    """The stored, comparable form of a snapshot."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
