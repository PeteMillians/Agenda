"""What every provider adapter shares: safe error categories, token grants, and the interface the
sync and write engines call (spec §10). Provider-specific behaviour stays in google/ and microsoft/.

Only safe error categories leave an adapter: never tokens or raw provider responses.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx


class ProviderError(Exception):
    def __init__(self, category: str, *, retryable: bool, retry_after: timedelta | None = None):
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.retry_after = retry_after


class AuthRevoked(ProviderError):
    """invalid_grant: the refresh token is dead (revoked, or expired after 7 days in Testing)."""

    def __init__(self) -> None:
        super().__init__("needs_reauth", retryable=False)


class AccessExpired(ProviderError):
    def __init__(self) -> None:
        super().__init__("access_expired", retryable=True)


class SyncTokenExpired(ProviderError):
    """Google 410 Gone / Graph sync state not found: discard the cursor and run a full sync."""

    def __init__(self) -> None:
        super().__init__("sync_token_expired", retryable=True)


class PreconditionFailed(ProviderError):
    """412: the provider's copy changed since our ETag. Never retried blindly: it is a conflict."""

    def __init__(self) -> None:
        super().__init__("precondition_failed", retryable=False)


class Gone(ProviderError):
    """404/410 on a single event: it was deleted at the provider."""

    def __init__(self) -> None:
        super().__init__("event_gone", retryable=False)


class Unsupported(Exception):
    """An event Agenda cannot represent faithfully; it is skipped and counted, not guessed at."""


@dataclass(frozen=True)
class Incoming:
    """One provider event, translated to Agenda's canonical model (ADR 0002)."""

    provider_id: str
    etag: str | None
    cancelled: bool
    series_provider_id: str | None = None  # set on instance overrides
    original_key: str | None = None  # occurrence key the override replaces
    content: dict[str, str] = field(default_factory=dict)  # title, description, location, timezone
    timing: dict[str, str | None] | None = None  # start_at/end_at or start_date/end_date
    rrule: str | None = None
    exdate_keys: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)  # kept for conflict snapshots

    @property
    def is_instance(self) -> bool:
        return self.series_provider_id is not None


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
    provider_calendar_id: str
    name: str
    color: str
    timezone: str
    access_role: str  # owner | writer | reader (normalized across providers)
    is_primary: bool


@dataclass(frozen=True)
class Written:
    provider_event_id: str
    etag: str | None
    item: dict[str, Any]  # the provider's copy after the write


def retry_after(response: httpx.Response) -> timedelta | None:
    value = response.headers.get("Retry-After", "")
    return timedelta(seconds=int(value)) if value.isdigit() else None


def id_token_claims(id_token: str | None) -> dict[str, Any]:
    """The id_token came straight from the provider's token endpoint over TLS, so reading its
    claims without verifying the signature is acceptable (OpenID Connect core §3.1.3.7)."""
    if not id_token:
        return {}
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


WRITABLE_ROLES = ("owner", "writer")


class ProviderClient(Protocol):
    """One provider's HTTP calls. `write` asks for event write scopes (two-way sync)."""

    name: str
    label: str

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, *, write: bool) -> str: ...
    def has_write_scope(self, scopes: list[str]) -> bool: ...
    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Grant: ...
    async def refresh(self, refresh_token: str) -> Grant: ...
    async def revoke(self, token: str) -> None: ...
    async def list_calendars(self, access_token: str) -> list[CalendarInfo]: ...
    async def list_events(
        self, access_token: str, calendar_id: str, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str]: ...
    async def get_event(
        self, access_token: str, calendar_id: str, event_id: str
    ) -> dict[str, Any] | None: ...
    async def insert_event(
        self, access_token: str, calendar_id: str, body: dict[str, Any], *, client_key: str
    ) -> Written: ...
    async def patch_event(
        self, access_token: str, calendar_id: str, event_id: str, body: dict[str, Any], etag: str | None
    ) -> Written: ...
    async def delete_event(
        self, access_token: str, calendar_id: str, event_id: str, etag: str | None
    ) -> None: ...
