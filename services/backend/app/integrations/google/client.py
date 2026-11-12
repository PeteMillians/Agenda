"""Google OAuth and Calendar API calls (spec §10). No persistence here; see ../sync.py.

Only safe error categories leave this module: never tokens or raw provider responses.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy.engine import RowMapping

from app.application.events import Timing
from app.domain.clock import now
from app.integrations.errors import AuthRevoked, Gone, ProviderError, Unrepresentable, raise_for
from app.integrations.google import mapping
from app.integrations.model import CalendarInfo, Changes, Grant, Incoming, Outgoing, Unsupported, Written

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
FULL_SCOPE = "https://www.googleapis.com/auth/calendar"
# The narrowest scopes per stage: read-only import, plus event writes once two-way sync is wanted.
SCOPES = ["openid", "email", READ_SCOPE]
WRITE_SCOPES = [*SCOPES, WRITE_SCOPE]
PAGE_SIZE = 2500


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


class GoogleCalendar:
    name = "google"

    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret

    def instance_window(self) -> None:
        return None  # Google lists every override and EXDATE

    # ------------------------------------------------------------------ OAuth

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, *, write: bool = False) -> str:
        return f"{AUTH_URL}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(WRITE_SCOPES if write else SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",  # we need a refresh token
                "prompt": "consent",  # and Google only re-issues one on consent
                "include_granted_scopes": "true",
            }
        )

    @staticmethod
    def can_write(scopes: Sequence[str]) -> bool:
        return WRITE_SCOPE in scopes or FULL_SCOPE in scopes

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.http.post(TOKEN_URL, data=form)
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        if response.status_code == 400 and response.json().get("error") == "invalid_grant":
            raise AuthRevoked()
        raise_for(response)
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

    # ------------------------------------------------------------------ HTTP

    async def _send(
        self,
        method: str,
        token: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        etag: str | None = None,
        event: bool = False,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        if etag:
            headers["If-Match"] = etag
        try:
            response = await self.http.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        raise_for(response, event=event)
        return response.json() if response.content else {}

    def _events_url(self, source: RowMapping, event_id: str | None = None) -> str:
        url = f"{API}/calendars/{quote(source['provider_calendar_id'], safe='')}/events"
        return f"{url}/{quote(event_id, safe='')}" if event_id else url

    # ------------------------------------------------------------------ reading

    async def list_calendars(self, token: str) -> list[CalendarInfo]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {"pageToken": page} if page else {}
            body = await self._send("GET", token, f"{API}/users/me/calendarList", params=params)
            items += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                break
        return [
            CalendarInfo(
                provider_id=cal["id"],
                name=cal.get("summaryOverride") or cal.get("summary") or cal["id"],
                color=cal.get("backgroundColor", "#64748b"),
                timezone=cal.get("timeZone", "UTC"),
                access_role=cal.get("accessRole", "reader"),
                is_primary=bool(cal.get("primary")),
            )
            for cal in items
        ]

    async def changes(self, token: str, source: RowMapping, cursor: str | None) -> Changes:
        """Every page of changes since the sync token (or everything, without one). Series come back
        as masters + exceptions (singleEvents=false). Raises CursorExpired on 410."""
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {"singleEvents": "false", "maxResults": str(PAGE_SIZE)}
            if cursor:
                params["syncToken"] = cursor
            else:
                params["showDeleted"] = "false"
            if page:
                params["pageToken"] = page
            body = await self._send("GET", token, self._events_url(source), params=params)
            items += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                break
        parsed: list[Incoming] = []
        dropped: list[str] = []
        skipped = 0
        for item in items:
            try:
                parsed.append(mapping.parse(item, source["timezone"]))
            except Unsupported as reason:
                skipped += 1
                dropped.append(item["id"])  # it may have been importable before
                log.warning("skipping provider event: %s", reason)
            except (KeyError, ValueError):
                skipped += 1
                log.warning("skipping malformed provider event")
        return Changes(
            parsed, body["nextSyncToken"], full=cursor is None, skipped=skipped, dropped=tuple(dropped)
        )

    async def fetch(self, token: str, source: RowMapping, provider_id: str) -> Incoming | None:
        try:
            item = await self._send("GET", token, self._events_url(source, provider_id), event=True)
        except Gone:
            return None
        try:
            incoming = mapping.parse(item, source["timezone"])
        except Unsupported as exc:
            raise Unrepresentable(str(exc)) from exc
        return None if incoming.cancelled and not incoming.is_instance else incoming

    # ------------------------------------------------------------------ writing

    def _written(self, item: dict[str, Any], fallback_id: str | None = None) -> Written:
        return Written(item.get("id") or fallback_id or "", item.get("etag"))

    async def insert(self, token: str, source: RowMapping, out: Outgoing) -> Written:
        item = await self._send("POST", token, self._events_url(source), json_body=mapping.body(out))
        return self._written(item)

    async def update(
        self, token: str, source: RowMapping, provider_id: str, etag: str | None, out: Outgoing
    ) -> Written:
        item = await self._send(
            "PATCH",
            token,
            self._events_url(source, provider_id),
            json_body=mapping.body(out),
            etag=etag,
            event=True,
        )
        return self._written(item, provider_id)

    async def delete(self, token: str, source: RowMapping, provider_id: str, etag: str | None) -> None:
        with contextlib.suppress(Gone):  # already gone is what we wanted
            await self._send("DELETE", token, self._events_url(source, provider_id), etag=etag, event=True)

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
        assert out.original_key is not None
        target = instance_id or mapping.instance_id(master_id, out.original_key, master.timing.all_day)
        url = self._events_url(source, target)
        if out.cancelled:
            with contextlib.suppress(Gone):
                await self._send("DELETE", token, url, etag=etag, event=True)
            return Written(target, None)
        item = await self._send(
            "PATCH", token, url, json_body=mapping.body(out, instance=True), etag=etag, event=True
        )
        return self._written(item, target)

    def check_rule(self, rule: str, first: Timing, timezone: str) -> None:
        return None  # Google stores any RFC 5545 rule Agenda accepts
