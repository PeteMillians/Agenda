from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints


class OAuthClientStatus(BaseModel):
    configured: bool
    client_id: str | None


class OAuthClientIn(BaseModel):
    """From Google Cloud Console: an OAuth client of type "Desktop app"."""

    client_id: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[\w.-]+\.apps\.googleusercontent\.com$")
    ]
    client_secret: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ConnectStart(BaseModel):
    authorization_url: str = Field(description="Open this in the system browser")


class ExternalAccount(BaseModel):
    id: UUID
    provider: Literal["google", "microsoft"]
    email: str
    status: Literal["active", "needs_reauth"]
    granted_scopes: list[str]
    revision: int
    created_at: datetime


class CalendarSource(BaseModel):
    id: UUID
    external_account_id: UUID
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
    revision: int


class CalendarSourcePatch(BaseModel):
    """Attach to a space and turn import on or off. Moving to another space moves its events;
    turning it off removes the imported copies (Google keeps the originals)."""

    space_id: UUID | None = None
    enabled: bool | None = None


class SyncStarted(BaseModel):
    job_ids: list[UUID]
