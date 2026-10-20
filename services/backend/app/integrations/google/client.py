"""Google OAuth and Calendar API calls. No persistence here; see sync.py and accounts.py.

Only safe error categories leave this module: never tokens or raw provider responses.
"""

from __future__ import annotations

import base64
import contextlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.domain.clock import now

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
# Read-only import (Phase 3); write scopes are requested only when two-way sync is enabled.
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/calendar.readonly"]
PAGE_SIZE = 2500


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
    """410 Gone: discard the sync token and run a full sync."""

    def __init__(self) -> None:
        super().__init__("sync_token_expired", retryable=True)


@dataclass(frozen=True)
class Grant:
    access_token: str
    expires_at: datetime
    refresh_token: str | None
    scopes: list[str]
    subject: str | None = None
    email: str | None = None


def _retry_after(response: httpx.Response) -> timedelta | None:
    value = response.headers.get("Retry-After", "")
    return timedelta(seconds=int(value)) if value.isdigit() else None


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
        raise ProviderError("rate_limited", retryable=True, retry_after=_retry_after(response))
    if status >= 500:
        raise ProviderError("provider_unavailable", retryable=True, retry_after=_retry_after(response))
    if status == 404:
        raise ProviderError("calendar_not_found", retryable=False)
    if status == 403:
        raise ProviderError("access_denied", retryable=False)
    raise ProviderError(f"provider_error_{status}", retryable=False)


def _id_token_claims(id_token: str | None) -> dict[str, Any]:
    """The id_token came straight from Google's token endpoint over TLS, so reading its claims
    without verifying the signature is acceptable (Google's OpenID Connect guidance)."""
    if not id_token:
        return {}
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class GoogleClient:
    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret

    def authorization_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        return f"{AUTH_URL}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
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
        claims = _id_token_claims(body.get("id_token"))
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

    async def _get(self, access_token: str, url: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.http.get(
                url, params=params, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        _raise_for(response)
        return response.json()

    async def list_calendars(self, access_token: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {"pageToken": page} if page else {}
            body = await self._get(access_token, f"{API}/users/me/calendarList", params)
            items += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                return items

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
