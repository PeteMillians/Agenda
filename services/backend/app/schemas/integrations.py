from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

Provider = Literal["google", "microsoft"]


class OAuthClientStatus(BaseModel):
    configured: bool
    client_id: str | None
    vault: str | None = Field(
        description="Where refresh tokens are kept; null means accounts can't be connected"
    )
    tenant: str | None = Field(default=None, description="Microsoft only: the sign-in authority")
    redirect_uri: str = Field(description="The redirect URI to register with the provider")


class OAuthClientIn(BaseModel):
    """From Google Cloud Console: an OAuth client of type "Desktop app"."""

    client_id: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[\w.-]+\.apps\.googleusercontent\.com$")
    ]
    client_secret: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class MicrosoftClientIn(BaseModel):
    """From the Microsoft Entra admin center: App registrations > your app > Overview."""

    client_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        ),
    ] = Field(description='"Application (client) ID"')
    tenant: Annotated[
        str,
        StringConstraints(strip_whitespace=True, pattern=r"^[\w.-]{1,100}$"),
    ] = Field(
        default="common",
        description="common (work, school, and personal accounts), consumers (personal only), "
        "organizations, or a directory (tenant) id for a single-tenant app",
    )


class ConnectStart(BaseModel):
    authorization_url: str = Field(description="Open this in the system browser")


class ExternalAccount(BaseModel):
    id: UUID
    provider: Provider
    email: str
    status: Literal["active", "needs_reauth"]
    granted_scopes: list[str]
    can_write: bool = Field(description="Event write access was granted (needed for two-way sync)")
    revision: int
    created_at: datetime


class CalendarSource(BaseModel):
    id: UUID
    external_account_id: UUID
    provider: Provider
    account_email: str
    name: str
    color: str
    timezone: str
    access_role: str
    is_primary: bool
    enabled: bool
    space_id: UUID | None
    sync_mode: Literal["read_only", "two_way"]
    sync_state: Literal["idle", "syncing", "ok", "error", "needs_reauth"]
    last_error: str | None
    last_synced_at: datetime | None
    pending_changes: int = Field(description="Agenda edits not yet accepted by the provider")
    open_conflicts: int
    revision: int


class CalendarSourcePatch(BaseModel):
    """Attach to a space and turn import on or off. Moving to another space moves its events;
    turning it off removes the imported copies (the provider keeps the originals). `two_way`
    lets Agenda edit the calendar's events; it needs write access at the provider."""

    space_id: UUID | None = None
    enabled: bool | None = None
    sync_mode: Literal["read_only", "two_way"] | None = None


class EventVersion(BaseModel):
    title: str
    description: str
    location: str
    all_day: bool
    start_at: datetime | None
    end_at: datetime | None
    start_date: date | None
    end_date: date | None
    timezone: str
    rrule: str | None
    deleted: bool = False


class SyncConflict(BaseModel):
    """Agenda and the provider both changed an event. Nothing is overwritten until it's resolved."""

    id: UUID
    event_id: UUID
    calendar_source_id: UUID
    calendar_name: str
    provider: Provider
    detected_at: datetime
    local: EventVersion = Field(description="Agenda's version, as it is now")
    remote: EventVersion | None = Field(description="The provider's version; null if deleted there")
    can_keep_both: bool


class ConflictResolution(BaseModel):
    resolution: Literal["keep_local", "keep_remote", "keep_both"]


class SyncStarted(BaseModel):
    job_ids: list[UUID]
