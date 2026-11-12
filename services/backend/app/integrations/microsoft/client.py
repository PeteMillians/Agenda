"""Microsoft identity platform and Graph calendar calls for personal Microsoft accounts (ADR 0005).

A public client: the app registration has no secret, so PKCE alone protects the code exchange.
Graph's delta query only returns expanded occurrences, which cannot rebuild a series, so each
sync lists the calendar's events and, for each series, its instances in a window (ADR 0005).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.engine import RowMapping

from app.application.events import Timing
from app.domain import recurrence
from app.domain.clock import now
from app.integrations.errors import AuthRevoked, Gone, ProviderError, Unrepresentable, raise_for
from app.integrations.microsoft import mapping
from app.integrations.model import CalendarInfo, Changes, Grant, Incoming, Outgoing, Unsupported, Written

log = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"  # personal accounts
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["openid", "email", "offline_access", "User.Read", "Calendars.Read"]
WRITE_SCOPES = ["openid", "email", "offline_access", "User.Read", "Calendars.ReadWrite"]
PAGE_SIZE = 100
# Instances are read in this window around today; cancellations and overrides outside it keep
# whatever was last seen.
LOOK_BACK = timedelta(days=180)
LOOK_AHEAD = timedelta(days=400)
FIELDS = (
    "id,subject,body,location,start,end,isAllDay,isCancelled,recurrence,type,seriesMasterId,"
    "originalStart,originalStartTimeZone"
)
PREFER = 'outlook.timezone="UTC", outlook.body-content-type="text"'


def _claims(id_token: str | None) -> dict[str, Any]:
    """Read straight from the token endpoint over TLS, so the signature is not checked."""
    if not id_token:
        return {}
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def _scopes(text: str) -> list[str]:
    return [s.removeprefix("https://graph.microsoft.com/") for s in text.split()]


class MicrosoftCalendar:
    name = "microsoft"

    def __init__(self, http: httpx.AsyncClient, client_id: str):
        self.http = http
        self.client_id = client_id

    # ------------------------------------------------------------------ OAuth

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, *, write: bool = False) -> str:
        return f"{AUTHORITY}/authorize?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "response_mode": "query",
                "scope": " ".join(WRITE_SCOPES if write else SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )

    @staticmethod
    def can_write(scopes: Sequence[str]) -> bool:
        return "Calendars.ReadWrite" in scopes

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.http.post(f"{AUTHORITY}/token", data={**form, "client_id": self.client_id})
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        if response.status_code in (400, 401):
            with contextlib.suppress(ValueError):
                if response.json().get("error") in ("invalid_grant", "interaction_required"):
                    raise AuthRevoked()
        raise_for(response)
        return response.json()

    def _grant(self, body: dict[str, Any]) -> Grant:
        claims = _claims(body.get("id_token"))
        return Grant(
            access_token=body["access_token"],
            expires_at=now() + timedelta(seconds=int(body.get("expires_in", 3600))),
            refresh_token=body.get("refresh_token"),
            scopes=_scopes(body.get("scope", "")),
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
        return self._grant(await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token}))

    async def revoke(self, token: str) -> None:
        """Personal accounts have no revocation endpoint; forgetting the token is all we can do.
        The user can remove Agenda at https://account.live.com/consent/Manage."""
        return None

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
        headers = {"Authorization": f"Bearer {token}", "Prefer": PREFER}
        if etag:
            headers["If-Match"] = etag
        try:
            response = await self.http.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderError("provider_unreachable", retryable=True) from exc
        raise_for(response, event=event)
        return response.json() if response.content else {}

    async def _pages(self, token: str, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        query: dict[str, str] | None = params
        while next_url:
            body = await self._send("GET", token, next_url, params=query)
            items += body.get("value", [])
            next_url, query = body.get("@odata.nextLink"), None  # the link carries its own query
        return items

    @staticmethod
    def _event_url(event_id: str) -> str:
        return f"{GRAPH}/me/events/{quote(event_id, safe='')}"

    # ------------------------------------------------------------------ reading

    async def list_calendars(self, token: str) -> list[CalendarInfo]:
        items = await self._pages(token, f"{GRAPH}/me/calendars", {"$top": str(PAGE_SIZE)})
        return [
            CalendarInfo(
                provider_id=cal["id"],
                name=cal.get("name") or "Calendar",
                color=cal.get("hexColor") or "#0078d4",
                timezone="UTC",  # Graph has no per-calendar zone; every event carries its own
                access_role="writer" if cal.get("canEdit") else "reader",
                is_primary=bool(cal.get("isDefaultCalendar")),
            )
            for cal in items
        ]

    def instance_window(self) -> tuple[date, date]:
        today = now().date()
        return today - LOOK_BACK, today + LOOK_AHEAD

    async def _instances(
        self, token: str, master_id: str, lo: datetime, hi: datetime
    ) -> list[dict[str, Any]]:
        return await self._pages(
            token,
            f"{self._event_url(master_id)}/instances",
            {
                "startDateTime": lo.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": hi.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "$select": FIELDS,
                "$top": str(PAGE_SIZE),
            },
        )

    def _expected(self, master: Incoming, lo_day: date, hi_day: date) -> set[str]:
        assert master.timing is not None and master.rrule is not None
        first = Timing.of(master.timing)
        tz = ZoneInfo(master.content["timezone"])
        if first.all_day:
            return {
                d.isoformat()
                for d in recurrence.expand_dates(
                    master.rrule, first.start_date, first.length.days, lo_day, hi_day
                )  # type: ignore[arg-type]
            }
        lo = datetime.combine(lo_day, time(), tz)
        hi = datetime.combine(hi_day, time(), tz)
        return {
            Timing(False, start_at=s, end_at=s).key
            for s in recurrence.expand_timed(master.rrule, first.start_at, tz, first.length, lo, hi)  # type: ignore[arg-type]
        }

    async def _series(
        self, token: str, master: Incoming, lo_day: date, hi_day: date
    ) -> tuple[Incoming, list[Incoming]]:
        """The master with its cancelled occurrences as EXDATE keys, plus its overrides."""
        assert master.timing is not None
        all_day = master.timing["start_date"] is not None
        tz = master.content["timezone"]
        lo = datetime.combine(lo_day, time(), ZoneInfo(tz))
        hi = datetime.combine(hi_day, time(), ZoneInfo(tz))
        seen: set[str] = set()
        overrides: list[Incoming] = []
        for item in await self._instances(token, master.provider_id, lo, hi):
            if not item.get("originalStart"):
                continue
            seen.add(mapping.occurrence_key(item["originalStart"], all_day, tz))
            if item.get("type") == "exception":
                overrides.append(mapping.parse(item, tz, series_all_day=all_day))
        cancelled = sorted(self._expected(master, lo_day, hi_day) - seen)
        return replace(master, exdate_keys=tuple(cancelled)), overrides

    async def changes(self, token: str, source: RowMapping, cursor: str | None) -> Changes:
        lo_day, hi_day = self.instance_window()
        url = f"{GRAPH}/me/calendars/{quote(source['provider_calendar_id'], safe='')}/events"
        items = await self._pages(token, url, {"$select": FIELDS, "$top": str(PAGE_SIZE)})
        parsed: list[Incoming] = []
        dropped: list[str] = []
        skipped = 0
        for item in items:
            try:
                incoming = mapping.parse(item, source["timezone"])
            except Unsupported as reason:
                skipped += 1
                dropped.append(item["id"])
                log.warning("skipping provider event: %s", reason)
                continue
            except (KeyError, ValueError):
                skipped += 1
                log.warning("skipping malformed provider event")
                continue
            if incoming.rrule is None:
                parsed.append(incoming)
                continue
            master, overrides = await self._series(token, incoming, lo_day, hi_day)
            parsed += [master, *overrides]
        return Changes(
            parsed, None, full=True, skipped=skipped, dropped=tuple(dropped), window=(lo_day, hi_day)
        )

    async def fetch(self, token: str, source: RowMapping, provider_id: str) -> Incoming | None:
        try:
            item = await self._send(
                "GET", token, self._event_url(provider_id), params={"$select": FIELDS}, event=True
            )
        except Gone:
            return None
        try:
            incoming = mapping.parse(item, source["timezone"])
        except Unsupported as exc:
            raise Unrepresentable(str(exc)) from exc
        if incoming.rrule is not None:
            incoming, _ = await self._series(token, incoming, *self.instance_window())
        return incoming

    # ------------------------------------------------------------------ writing

    async def _etag(self, token: str, event_id: str) -> str | None:
        item = await self._send("GET", token, self._event_url(event_id), params={"$select": "id"}, event=True)
        return item.get("@odata.etag")

    async def _find(self, token: str, master_id: str, master: Outgoing, key: str) -> dict[str, Any] | None:
        """The occurrence of `master_id` whose original start is `key`."""
        tz = ZoneInfo(master.timezone)
        if master.timing.all_day:
            day = date.fromisoformat(key[:10])
            lo = datetime.combine(day, time(), tz) - timedelta(days=1)
            hi = datetime.combine(day + master.timing.length, time(), tz) + timedelta(days=1)
        else:
            start = datetime.fromisoformat(key)
            lo, hi = start - timedelta(hours=1), start + master.timing.length + timedelta(hours=1)
        for item in await self._instances(token, master_id, lo, hi):
            original = item.get("originalStart")
            if original and mapping.occurrence_key(original, master.timing.all_day, master.timezone) == key:
                return item
        return None

    async def _cancel_occurrences(self, token: str, master_id: str, out: Outgoing) -> bool:
        """Graph has no EXDATE: each cancelled occurrence is deleted on its own."""
        removed = False
        for key in out.exdate_keys:
            found = await self._find(token, master_id, out, key)
            if found:
                with contextlib.suppress(Gone):
                    await self._send("DELETE", token, self._event_url(found["id"]), event=True)
                removed = True
        return removed

    async def insert(self, token: str, source: RowMapping, out: Outgoing) -> Written:
        url = f"{GRAPH}/me/calendars/{quote(source['provider_calendar_id'], safe='')}/events"
        item = await self._send("POST", token, url, json_body=mapping.body(out))
        etag = item.get("@odata.etag")
        if out.exdate_keys and await self._cancel_occurrences(token, item["id"], out):
            etag = await self._etag(token, item["id"])
        return Written(item["id"], etag)

    async def update(
        self, token: str, source: RowMapping, provider_id: str, etag: str | None, out: Outgoing
    ) -> Written:
        item = await self._send(
            "PATCH", token, self._event_url(provider_id), json_body=mapping.body(out), etag=etag, event=True
        )
        new_etag = item.get("@odata.etag")
        if out.exdate_keys and await self._cancel_occurrences(token, provider_id, out):
            new_etag = await self._etag(token, provider_id)
        return Written(provider_id, new_etag)

    async def delete(self, token: str, source: RowMapping, provider_id: str, etag: str | None) -> None:
        with contextlib.suppress(Gone):
            await self._send("DELETE", token, self._event_url(provider_id), etag=etag, event=True)

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
        if instance_id is None:
            found = await self._find(token, master_id, master, out.original_key)
            if found is None:
                if out.cancelled:
                    return Written("", None)  # nothing left to cancel
                raise Gone()
            instance_id, etag = found["id"], None
        url = self._event_url(instance_id)
        if out.cancelled:
            with contextlib.suppress(Gone):
                await self._send("DELETE", token, url, etag=etag, event=True)
            return Written(instance_id, None)
        item = await self._send(
            "PATCH", token, url, json_body=mapping.body(out, instance=True), etag=etag, event=True
        )
        return Written(instance_id, item.get("@odata.etag"))

    def check_rule(self, rule: str, first: Timing, timezone: str) -> None:
        mapping.rrule_to_pattern(rule, first, timezone)
