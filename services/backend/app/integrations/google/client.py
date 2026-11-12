"""Google OAuth and Calendar API calls. No persistence here; see ../sync.py and ../push.py.

Only safe error categories leave this module: never tokens or raw provider responses.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.domain.clock import now
from app.integrations.base import (
    AccessExpired,
    AuthRevoked,
    CalendarInfo,
    Gone,
    Grant,
    PreconditionFailed,
    ProviderError,
    SyncTokenExpired,
    Written,
    id_token_claims,
    retry_after,
)

__all__ = ["AccessExpired", "AuthRevoked", "GoogleClient", "Grant", "ProviderError", "SyncTokenExpired"]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
# Read-only import; the event write scope is requested only when two-way sync is enabled.
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/calendar.readonly"]
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
PAGE_SIZE = 2500


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    if status == 401:
        raise AccessExpired()
    if status == 410:
        raise SyncTokenExpired()
    reason = ""
    with contextlib.suppress(ValueError, AttributeError, IndexError):
        reason = response.json().get("error", {}).get("errors", [{}])[0].get("reason", "")
    if status == 429 or (status == 403 and "rateLimit" in reason):
        raise ProviderError("rate_limited", retryable=True, retry_after=retry_after(response))
    if status >= 500:
        raise ProviderError("provider_unavailable", retryable=True, retry_after=retry_after(response))
    if status == 404:
        raise ProviderError("calendar_not_found", retryable=False)
    if status == 403:
        raise ProviderError("access_denied", retryable=False)
    raise ProviderError(f"provider_error_{status}", retryable=False)


def _raise_for_event(response: httpx.Response) -> None:
    """Single-event calls: 404/410 mean the event is gone, 412 that it changed since our ETag."""
    if response.status_code in (404, 410):
        raise Gone()
    if response.status_code == 412:
        raise PreconditionFailed()
    _raise_for(response)


ROLES = {"owner": "owner", "writer": "writer"}


class GoogleClient:
    name = "google"
    label = "Google"

    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, *, write: bool = False) -> str:
        return f"{AUTH_URL}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES + ([WRITE_SCOPE] if write else [])),
                "include_granted_scopes": "true",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",  # we need a refresh token
                "prompt": "consent",  # and Google only re-issues one on consent
            }
        )

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.http.post(TOKEN_URL, data=form)
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        if response.status_code == 400 and response.json().get("error") == "invalid_grant":
            raise AuthRevoked()
        _raise_for(response)
        return response.json()

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Grant:
        body = await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        claims = id_token_claims(body.get("id_token"))
        return Grant(
            access_token=body["access_token"],
            expires_at=now() + timedelta(seconds=int(body.get("expires_in", 3600))),
            refresh_token=body.get("refresh_token"),
            scopes=body.get("scope", "").split(),
            subject=claims.get("sub"),
            email=claims.get("email"),
        )

    async def refresh(self, refresh_token: str) -> Grant:
        body = await self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        return Grant(
            access_token=body["access_token"],
            expires_at=now() + timedelta(seconds=int(body.get("expires_in", 3600))),
            refresh_token=body.get("refresh_token"),
            scopes=body.get("scope", "").split(),
        )

    async def revoke(self, token: str) -> None:
        """Best effort: disconnecting must work even when Google is unreachable."""
        with contextlib.suppress(httpx.TransportError):
            await self.http.post(REVOKE_URL, data={"token": token})

    def has_write_scope(self, scopes: list[str]) -> bool:
        return WRITE_SCOPE in scopes or "https://www.googleapis.com/auth/calendar" in scopes

    async def _get(self, access_token: str, url: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.http.get(
                url, params=params, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        _raise_for(response)
        return response.json()

    async def list_calendars(self, access_token: str) -> list[CalendarInfo]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {"pageToken": page} if page else {}
            body = await self._get(access_token, f"{API}/users/me/calendarList", params)
            items += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                break
        return [
            CalendarInfo(
                provider_calendar_id=cal["id"],
                name=cal.get("summaryOverride") or cal.get("summary") or cal["id"],
                color=cal.get("backgroundColor", "#64748b"),
                timezone=cal.get("timeZone", "UTC"),
                access_role=ROLES.get(cal.get("accessRole", ""), "reader"),
                is_primary=bool(cal.get("primary")),
            )
            for cal in items
        ]

    async def list_events(
        self, access_token: str, calendar_id: str, sync_token: str | None
    ) -> tuple[list[dict[str, Any]], str]:
        """Every page of changes since `sync_token` (or everything, without one), plus the next
        sync token. Series come back as masters + exceptions (singleEvents=false)."""
        url = f"{API}/calendars/{quote(calendar_id, safe='')}/events"
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {"singleEvents": "false", "maxResults": str(PAGE_SIZE)}
            if sync_token:
                params["syncToken"] = sync_token
            else:
                params["showDeleted"] = "false"
            if page:
                params["pageToken"] = page
            body = await self._get(access_token, url, params)
            items += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                return items, body["nextSyncToken"]

    # ------------------------------------------------------------------ writes (two-way sync)

    async def _send(
        self,
        method: str,
        access_token: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        etag: str | None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        if etag:
            headers["If-Match"] = etag
        try:
            return await self.http.request(method, url, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc

    def _event_url(self, calendar_id: str, event_id: str | None = None) -> str:
        url = f"{API}/calendars/{quote(calendar_id, safe='')}/events"
        return f"{url}/{quote(event_id, safe='')}" if event_id else url

    @staticmethod
    def _written(item: dict[str, Any]) -> Written:
        return Written(item["id"], item.get("etag"), item)

    async def get_event(self, access_token: str, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        """The provider's current copy, or None once it is deleted (cancelled)."""
        response = await self._send("GET", access_token, self._event_url(calendar_id, event_id), etag=None)
        try:
            _raise_for_event(response)
        except Gone:
            return None
        item = response.json()
        return None if item.get("status") == "cancelled" else item

    async def insert_event(
        self, access_token: str, calendar_id: str, body: dict[str, Any], *, client_key: str
    ) -> Written:
        """`client_key` becomes the Google event id, so a retried insert can't create a duplicate:
        Google answers 409 and we adopt (or restore) the event the first attempt created."""
        response = await self._send(
            "POST", access_token, self._event_url(calendar_id), body={**body, "id": client_key}, etag=None
        )
        if response.status_code == 409:
            existing = await self._send(
                "GET", access_token, self._event_url(calendar_id, client_key), etag=None
            )
            _raise_for_event(existing)
            item = existing.json()
            if item.get("status") != "cancelled":
                return self._written(item)
            # deleted earlier (e.g. a conflict resolved in Agenda's favour): bring it back
            return await self.patch_event(
                access_token, calendar_id, client_key, {**body, "status": "confirmed"}, item.get("etag")
            )
        _raise_for_event(response)
        return self._written(response.json())

    async def patch_event(
        self, access_token: str, calendar_id: str, event_id: str, body: dict[str, Any], etag: str | None
    ) -> Written:
        response = await self._send(
            "PATCH", access_token, self._event_url(calendar_id, event_id), body=body, etag=etag
        )
        _raise_for_event(response)
        return self._written(response.json())

    async def delete_event(
        self, access_token: str, calendar_id: str, event_id: str, etag: str | None
    ) -> None:
        """Already gone counts as done."""
        response = await self._send("DELETE", access_token, self._event_url(calendar_id, event_id), etag=etag)
        try:
            _raise_for_event(response)
        except Gone:
            return
