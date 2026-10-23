"""An in-process fake of the Google endpoints Agenda uses, behind httpx.MockTransport.

It keeps a change log per calendar so sync tokens behave like Google's: a token returns only
what changed after it, deletions come back as status=cancelled, and expired tokens get 410.
Writes honour If-Match (412 on a stale ETag), client-chosen ids (409 when taken), and instance
ids of the form `<series id>_<original start>`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
        self.writes: list[tuple[str, str, str | None]] = []  # (method, event id, If-Match)
        self.fail_writes: list[httpx.Response] = []
        self.access_role = "owner"
        self.granted_scope = ""

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
        if query.get("include_granted_scopes") == "true" and self.granted_scope:
            query["scope"] = " ".join(dict.fromkeys(self.granted_scope.split() + query["scope"].split()))
        self.granted_scope = query["scope"]
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
        parts = url.path.split("/")
        if url.path.startswith("/calendar/v3/calendars/") and url.path.endswith("/events"):
            if request.method == "POST":
                return self._insert(parts[4], json.loads(request.content))
            return self._events(parts[4], dict(url.params))
        if url.path.startswith("/calendar/v3/calendars/") and len(parts) == 7 and parts[5] == "events":
            return self._one(request, parts[4], parts[6])
        return httpx.Response(404)

    # ------------------------------------------------------------------ writes

    def _stamp(self, cal: Calendar, item: dict[str, Any]) -> dict[str, Any]:
        item = {**item, "etag": f'"{secrets.token_hex(4)}"'}
        cal.events[item["id"]] = item
        cal.log.append(item["id"])
        return item

    def _insert(self, cal_id: str, body: dict[str, Any]) -> httpx.Response:
        cal = self.calendars[cal_id]
        if self.fail_writes:
            return self.fail_writes.pop(0)
        self.writes.append(("POST", body["id"], None))
        if body["id"] in cal.events:
            return httpx.Response(409, json={"error": {"code": 409, "message": "duplicate"}})
        return httpx.Response(200, json=self._stamp(cal, {"status": "confirmed", **body}))

    def _instance_base(self, cal: Calendar, event_id: str) -> dict[str, Any] | None:
        """An unmodified occurrence, synthesised from its series like Google does."""
        master_id, _, stamp = event_id.rpartition("_")
        master = cal.events.get(master_id)
        if not master_id or master is None or master.get("status") == "cancelled":
            return None
        if len(stamp) == 8:
            original = {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"}
        else:
            moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            original = {"dateTime": moment.isoformat(), "timeZone": master["start"].get("timeZone", "UTC")}
        return {
            "id": event_id,
            "status": "confirmed",
            "summary": master.get("summary"),
            "recurringEventId": master_id,
            "originalStartTime": original,
            "start": original,
            "end": original,
            "etag": master["etag"],
        }

    def _one(self, request: httpx.Request, cal_id: str, event_id: str) -> httpx.Response:
        cal = self.calendars[cal_id]
        current = cal.events.get(event_id) or self._instance_base(cal, event_id)
        if request.method == "GET":
            return httpx.Response(200, json=current) if current else httpx.Response(404)
        if self.fail_writes:
            return self.fail_writes.pop(0)
        if_match = request.headers.get("If-Match")
        self.writes.append((request.method, event_id, if_match))
        if current is None:
            return httpx.Response(404)
        if if_match and if_match != current["etag"]:
            return httpx.Response(412, json={"error": {"code": 412, "message": "Precondition Failed"}})
        if request.method == "DELETE":
            if current.get("status") == "cancelled":
                return httpx.Response(410)
            if event_id not in cal.events:
                cal.events[event_id] = current
            self.delete(cal_id, event_id)
            return httpx.Response(204)
        body = json.loads(request.content)
        merged = {**current, **body}
        if "recurringEventId" in current:
            merged["recurringEventId"] = current["recurringEventId"]
            merged["originalStartTime"] = current["originalStartTime"]
        return httpx.Response(200, json=self._stamp(cal, merged))

    def _calendar_entry(self, cal: Calendar) -> dict[str, Any]:
        return {
            "id": cal.id,
            "summary": cal.summary,
            "timeZone": cal.timezone,
            "primary": cal.primary,
            "accessRole": self.access_role,
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
            # what was consented to is what the token carries
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
