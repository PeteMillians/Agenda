"""Microsoft identity platform (OAuth) and Microsoft Graph calendar calls. No persistence here.

Agenda is a public client: PKCE, no client secret, and a loopback redirect to
`http://localhost:<port>/...`. Entra ignores the port of a localhost redirect URI, so the owner
registers `http://localhost/api/v1/integrations/microsoft/callback` once under "Mobile and
desktop applications" and it matches whatever port the sidecar got (ADR 0005).

Graph's calendarView/delta returns concrete occurrences, never series masters, inside a fixed
window. The cursor is our own JSON: the deltaLink plus the window's end, so the window can be
slid forward with a full resync before it runs out.
"""

from __future__ import annotations

import contextlib
import json
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.domain.clock import iso, now
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

LOGIN = "https://login.microsoftonline.com"
API = "https://graph.microsoft.com/v1.0"
GRAPH = "https://graph.microsoft.com/"
READ_SCOPE = GRAPH + "Calendars.Read"
WRITE_SCOPE = GRAPH + "Calendars.ReadWrite"
SCOPES = ["openid", "email", "profile", "offline_access"]
PAGE_SIZE = 100
# what Agenda imports: two months back and eighteen ahead; resynced once less than a year is left
WINDOW_BACK = timedelta(days=60)
WINDOW_AHEAD = timedelta(days=540)
WINDOW_MIN_AHEAD = timedelta(days=365)
PREFER_READ = 'outlook.timezone="UTC", outlook.body-content-type="text"'
SYNC_STATE_ERRORS = {"syncstatenotfound", "syncstateinvalid", "resyncrequired"}
AUTH_DEAD = {"invalid_grant", "interaction_required", "consent_required"}


def _error_code(response: httpx.Response) -> str:
    with contextlib.suppress(ValueError, AttributeError):
        body = response.json()
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("code", "")).lower()
        return str(error or "").lower()
    return ""


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    status, code = response.status_code, _error_code(response)
    if status == 401:
        raise AccessExpired()
    if status == 410 or code in SYNC_STATE_ERRORS:
        raise SyncTokenExpired()
    if status in (429, 503, 504) or status >= 500:
        category = "rate_limited" if status == 429 else "provider_unavailable"
        raise ProviderError(category, retryable=True, retry_after=retry_after(response))
    if status == 404:
        raise ProviderError("calendar_not_found", retryable=False)
    if status == 403:
        raise ProviderError("access_denied", retryable=False)
    raise ProviderError(f"provider_error_{status}", retryable=False)


def _raise_for_event(response: httpx.Response) -> None:
    if response.status_code in (404, 410):
        raise Gone()
    if response.status_code == 412 or _error_code(response) == "errorirresolvableconflict":
        raise PreconditionFailed()
    _raise_for(response)


def etag_of(item: dict[str, Any]) -> str | None:
    if item.get("@odata.etag"):
        return item["@odata.etag"]
    return f'W/"{item["changeKey"]}"' if item.get("changeKey") else None


def graph_time(value: str) -> str:
    """Graph sends seven fractional digits ("2026-10-21T18:30:00.0000000"); keep it parseable."""
    head, dot, fraction = value.partition(".")
    return f"{head}.{fraction[:6]}" if dot else head


class MicrosoftClient:
    name = "microsoft"
    label = "Microsoft"

    def __init__(self, http: httpx.AsyncClient, client_id: str, tenant: str = "common"):
        self.http = http
        self.client_id = client_id
        self.tenant = tenant

    @property
    def _token_url(self) -> str:
        return f"{LOGIN}/{self.tenant}/oauth2/v2.0/token"

    # ------------------------------------------------------------------ OAuth

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, *, write: bool = False) -> str:
        return f"{LOGIN}/{self.tenant}/oauth2/v2.0/authorize?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "response_mode": "query",
                "scope": " ".join([*SCOPES, WRITE_SCOPE if write else READ_SCOPE]),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )

    def has_write_scope(self, scopes: list[str]) -> bool:
        return any(s.lower().endswith("calendars.readwrite") for s in scopes)

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.http.post(self._token_url, data={**form, "client_id": self.client_id})
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        if response.status_code in (400, 401) and _error_code(response) in AUTH_DEAD:
            raise AuthRevoked()
        if response.status_code in (400, 401):
            # misregistered app (wrong platform, account type, redirect URI): not retryable
            raise ProviderError("sign_in_rejected", retryable=False)
        _raise_for(response)
        return response.json()

    @staticmethod
    def _grant(body: dict[str, Any]) -> Grant:
        claims = id_token_claims(body.get("id_token"))
        return Grant(
            access_token=body["access_token"],
            expires_at=now() + timedelta(seconds=int(body.get("expires_in", 3600))),
            refresh_token=body.get("refresh_token"),
            # Entra reports Graph scopes without the resource prefix ("Calendars.Read")
            scopes=[s if "/" in s or s in SCOPES else GRAPH + s for s in body.get("scope", "").split()],
            subject=claims.get("oid") or claims.get("sub"),
            email=claims.get("email") or claims.get("preferred_username"),
        )

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Grant:
        return self._grant(
            await self._token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                }
            )
        )

    async def refresh(self, refresh_token: str) -> Grant:
        # `.default` asks for whatever the user already consented to, read or read-write
        return self._grant(
            await self._token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": f"{GRAPH}.default offline_access",
                }
            )
        )

    async def revoke(self, token: str) -> None:
        """Entra has no per-app revocation for public clients; the vault entry is deleted and the
        user can remove Agenda at account.live.com/consent/Manage or myapps.microsoft.com."""

    # ------------------------------------------------------------------ reads

    async def _request(
        self,
        method: str,
        access_token: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        etag: str | None = None,
        prefer: str = PREFER_READ,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}", "Prefer": prefer}
        if etag:
            headers["If-Match"] = etag
        try:
            return await self.http.request(method, url, params=params, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc

    async def list_calendars(self, access_token: str) -> list[CalendarInfo]:
        found: list[dict[str, Any]] = []
        url: str | None = f"{API}/me/calendars"
        while url:
            response = await self._request("GET", access_token, url)
            _raise_for(response)
            body = response.json()
            found += body.get("value", [])
            url = body.get("@odata.nextLink")
        return [
            CalendarInfo(
                provider_calendar_id=cal["id"],
                name=cal.get("name") or "Calendar",
                color=cal.get("hexColor") or "#0f6cbd",
                timezone="",  # calendars carry none; the owner's time zone is used
                access_role=("owner" if cal.get("isDefaultCalendar") else "writer")
                if cal.get("canEdit")
                else "reader",
                is_primary=bool(cal.get("isDefaultCalendar")),
            )
            for cal in found
        ]

    async def list_events(
        self, access_token: str, calendar_id: str, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str]:
        """Every page of changes since `cursor` (or the whole window, without one) and the next
        cursor. Raises SyncTokenExpired when the window is running out, to start a fresh one."""
        if cursor:
            state = json.loads(cursor)
            if state["until"] < iso(now() + WINDOW_MIN_AHEAD):
                raise SyncTokenExpired()
            url: str = state["link"]
            params: dict[str, str] | None = None
            until = state["until"]
        else:
            start, end = now() - WINDOW_BACK, now() + WINDOW_AHEAD
            url = f"{API}/me/calendars/{quote(calendar_id, safe='')}/calendarView/delta"
            params = {
                "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            until = iso(end)
        items: list[dict[str, Any]] = []
        while True:
            response = await self._request(
                "GET",
                access_token,
                url,
                params=params,
                prefer=f"{PREFER_READ}, odata.maxpagesize={PAGE_SIZE}",
            )
            _raise_for(response)
            body = response.json()
            items += body.get("value", [])
            params = None  # the links carry the query
            if "@odata.nextLink" in body:
                url = body["@odata.nextLink"]
                continue
            return items, json.dumps({"link": body["@odata.deltaLink"], "until": until})

    # ------------------------------------------------------------------ writes (two-way sync)

    @staticmethod
    def _written(item: dict[str, Any]) -> Written:
        return Written(item["id"], etag_of(item), item)

    async def get_event(self, access_token: str, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", access_token, f"{API}/me/events/{quote(event_id, safe='')}")
        try:
            _raise_for_event(response)
        except Gone:
            return None
        item = response.json()
        return None if item.get("isCancelled") else item

    async def insert_event(
        self, access_token: str, calendar_id: str, body: dict[str, Any], *, client_key: str
    ) -> Written:
        """`transactionId` makes Graph drop a retried POST instead of creating a duplicate."""
        response = await self._request(
            "POST",
            access_token,
            f"{API}/me/calendars/{quote(calendar_id, safe='')}/events",
            body={**body, "transactionId": client_key},
        )
        _raise_for_event(response)
        return self._written(response.json())

    async def patch_event(
        self, access_token: str, calendar_id: str, event_id: str, body: dict[str, Any], etag: str | None
    ) -> Written:
        url = f"{API}/me/events/{quote(event_id, safe='')}"
        if "body" in body:
            # Agenda holds the description as text; don't flatten an unchanged rich (HTML) body
            current = await self._request("GET", access_token, url)
            _raise_for_event(current)
            item = current.json()
            if etag and etag_of(item) != etag:
                raise PreconditionFailed()
            if (item.get("body") or {}).get("content", "").strip() == body["body"]["content"].strip():
                body = {k: v for k, v in body.items() if k != "body"}
        response = await self._request("PATCH", access_token, url, body=body, etag=etag)
        _raise_for_event(response)
        return self._written(response.json())

    async def delete_event(
        self, access_token: str, calendar_id: str, event_id: str, etag: str | None
    ) -> None:
        response = await self._request(
            "DELETE", access_token, f"{API}/me/events/{quote(event_id, safe='')}", etag=etag
        )
        try:
            _raise_for_event(response)
        except Gone:
            return
