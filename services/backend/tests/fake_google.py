"""An in-process fake of the Google endpoints Agenda uses, behind httpx.MockTransport.

It keeps a change log per calendar so sync tokens behave like Google's: a token returns only
what changed after it, deletions come back as status=cancelled, and expired tokens get 410.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


@dataclass
class Calendar:
    id: str
    summary: str
    timezone: str = "America/New_York"
    primary: bool = False
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)  # event ids, in change order
    expired_before: int = 0  # tokens below this position get 410


class FakeGoogle:
    def __init__(self) -> None:
        self.calendars: dict[str, Calendar] = {}
        self.pending_codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: set[str] = set()
        self.revoked: list[str] = []
        self.page_size = 2500
        self.fail_next: list[httpx.Response] = []  # injected errors for event listing
        self.order_reversed = False
        self.event_requests = 0
        self.subject = "google-sub-1"
        self.email = "pete@example.com"

    # ------------------------------------------------------------------ test helpers

    def add_calendar(
        self, cal_id: str, summary: str, *, primary: bool = False, tz: str = "America/New_York"
    ) -> Calendar:
        self.calendars[cal_id] = Calendar(cal_id, summary, tz, primary)
        return self.calendars[cal_id]

    def put(self, cal_id: str, item: dict[str, Any]) -> None:
        cal = self.calendars[cal_id]
        item = {"status": "confirmed", **item, "etag": f'"{secrets.token_hex(4)}"'}
        cal.events[item["id"]] = item
        cal.log.append(item["id"])

    def delete(self, cal_id: str, event_id: str) -> None:
        cal = self.calendars[cal_id]
        old = cal.events[event_id]
        minimal = {"id": event_id, "status": "cancelled"}
        if "recurringEventId" in old:  # cancelled instances keep their series link
            minimal |= {k: old[k] for k in ("recurringEventId", "originalStartTime")}
        cal.events[event_id] = {**minimal, "etag": f'"{secrets.token_hex(4)}"'}
        cal.log.append(event_id)

    def expire_sync_tokens(self, cal_id: str) -> None:
        cal = self.calendars[cal_id]
        cal.expired_before = len(cal.log) + 1

    def authorize(self, url: str) -> str:
        """Play the user consenting in the browser; returns the code Google would redirect with."""
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert query["code_challenge_method"] == "S256"
        assert query["access_type"] == "offline"
        code = secrets.token_hex(8)
        self.pending_codes[code] = query
        return code

    # ------------------------------------------------------------------ transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            return self._token(dict(parse_qs(request.content.decode())))
        if url.host == "oauth2.googleapis.com" and url.path == "/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        if not request.headers.get("Authorization", "").startswith("Bearer access-"):
            return httpx.Response(401)
        if url.path == "/calendar/v3/users/me/calendarList":
            return httpx.Response(
                200, json={"items": [self._calendar_entry(c) for c in self.calendars.values()]}
            )
        if url.path.startswith("/calendar/v3/calendars/") and url.path.endswith("/events"):
            return self._events(url.path.split("/")[4], dict(url.params))
        return httpx.Response(404)

    def _calendar_entry(self, cal: Calendar) -> dict[str, Any]:
        return {
            "id": cal.id,
            "summary": cal.summary,
            "timeZone": cal.timezone,
            "primary": cal.primary,
            "accessRole": "owner",
            "backgroundColor": "#039be5",
        }

    def _token(self, form: dict[str, list[str]]) -> httpx.Response:
        grant = form["grant_type"][0]
        if grant == "authorization_code":
            pending = self.pending_codes.pop(form["code"][0], None)
            if pending is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != pending["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})  # PKCE mismatch
            if form["redirect_uri"][0] != pending["redirect_uri"]:
                return httpx.Response(400, json={"error": "redirect_uri_mismatch"})
            refresh = f"refresh-{secrets.token_hex(4)}"
            self.refresh_tokens.add(refresh)
            id_token = f"{_b64({'alg': 'none'})}.{_b64({'sub': self.subject, 'email': self.email})}.sig"
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{secrets.token_hex(4)}",
                    "expires_in": 3600,
                    "refresh_token": refresh,
                    "scope": pending["scope"],
                    "id_token": id_token,
                },
            )
        if grant == "refresh_token":
            if form["refresh_token"][0] not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": f"access-{secrets.token_hex(4)}", "expires_in": 3600}
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _events(self, cal_id: str, params: dict[str, str]) -> httpx.Response:
        self.event_requests += 1
        if self.fail_next:
            return self.fail_next.pop(0)
        cal = self.calendars.get(cal_id)
        if cal is None:
            return httpx.Response(404)
        assert params.get("singleEvents") == "false"
        if "syncToken" in params:
            position = int(params["syncToken"].removeprefix("tok-"))
            if position < cal.expired_before:
                return httpx.Response(
                    410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}}
                )
            changed_ids = list(dict.fromkeys(cal.log[position:]))
            items = [cal.events[i] for i in changed_ids]
        else:
            items = [e for e in cal.events.values() if e["status"] != "cancelled"]
        if self.order_reversed:
            items = list(reversed(items))
        start = int(params.get("pageToken", "0"))
        page = items[start : start + self.page_size]
        body: dict[str, Any] = {"items": page}
        if start + self.page_size < len(items):
            body["nextPageToken"] = str(start + self.page_size)
        else:
            body["nextSyncToken"] = f"tok-{len(cal.log)}"
        return httpx.Response(200, json=body)
