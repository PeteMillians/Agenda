from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

Provider = Literal["google", "microsoft"]
Resolution = Literal["keep_local", "keep_remote", "keep_both"]


class OAuthClientStatus(BaseModel):
    configured: bool
    client_id: str | None
    vault: str | None = Field(
        description="Where refresh tokens are kept; null means accounts can't be connected"
    )
    redirect_uri: str | None = Field(
        None, description="Microsoft only: the redirect URI to register for the app in Azure"
    )


class OAuthClientIn(BaseModel):
    """Google: an OAuth client of type "Desktop app" (ID and secret). Microsoft: the application
    (client) ID of an app registration for personal accounts; it has no secret."""

    client_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    client_secret: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class ConnectStart(BaseModel):
    authorization_url: str = Field(description="Open this in the system browser")


class ExternalAccount(BaseModel):
    id: UUID
    provider: Provider
    email: str
    status: Literal["active", "needs_reauth"]
    granted_scopes: list[str]
    revision: int
    created_at: datetime


class CalendarSource(BaseModel):
    id: UUID
    external_account_id: UUID
    account_email: str
    provider: Provider
    name: str
    color: str
    timezone: str
    access_role: str
    is_primary: bool
    enabled: bool
    space_id: UUID | None
    sync_mode: Literal["read_only", "two_way"]
    can_write: bool = Field(description="The account has granted event write access")
    sync_state: Literal["idle", "syncing", "ok", "error", "needs_reauth"]
    last_error: str | None
    last_synced_at: datetime | None
    pending_changes: int = Field(description="Local changes not yet sent to the provider")
    open_conflicts: int
    revision: int


class CalendarSourcePatch(BaseModel):
    """Attach to a space and turn import on or off. Moving to another space moves its events;
    turning it off removes the imported copies (the provider keeps the originals).
    `sync_mode=two_way` lets its events be edited here; switching back discards unpushed edits."""

    space_id: UUID | None = None
    enabled: bool | None = None
    sync_mode: Literal["read_only", "two_way"] | None = None


class SyncStarted(BaseModel):
    job_ids: list[UUID]


class SyncConflict(BaseModel):
    """The same event changed here and at the provider (spec §10). Snapshots hold the fields
    being compared: title, description, location, timezone, timing, rrule, status; or
    `{"deleted": true}` when that side deleted it."""

    id: UUID
    event_id: UUID
    calendar_source_id: UUID
    calendar_name: str
    calendar_color: str
    provider: Provider
    title: str
    kind: Literal["single", "series", "occurrence"]
    local: dict[str, Any]
    remote: dict[str, Any]
    choices: list[Resolution] = Field(description="keep_both is offered only when neither side deleted it")
    detected_at: datetime
    status: Literal["open", "resolved", "superseded"]
    resolution: Resolution | None
    resolved_at: datetime | None


class ResolveConflict(BaseModel):
    resolution: Resolution
