"""An in-process fake of the Microsoft identity platform and the Graph calendar endpoints Agenda
uses, behind httpx.MockTransport.

Like Graph, calendarView/delta returns occurrences (never series masters), pages through
@odata.nextLink, ends a round with @odata.deltaLink, reports deletions as `@removed`, and answers
an expired delta token with 410 SyncStateNotFound. Writes honour If-Match (412) and
transactionId (a retried POST returns the event the first one made).
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
from zoneinfo import ZoneInfo

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


@dataclass
class Calendar:
    id: str
    name: str
    default: bool = False
    can_edit: bool = True
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    expired_before: int = 0


def _as_utc(body: dict[str, Any]) -> dict[str, Any]:
    """Graph stores what it's sent and, with Prefer outlook.timezone="UTC", reads it back in UTC."""
    if body.get("isAllDay"):
        return body
    out = dict(body)
    for key in ("start", "end"):
        if key in body and body[key]["timeZone"] != "UTC":
            local = datetime.fromisoformat(body[key]["dateTime"]).replace(
                tzinfo=ZoneInfo(body[key]["timeZone"])
            )
            out[key] = {
                "dateTime": local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "timeZone": "UTC",
            }
    return out


class FakeMicrosoft:
    def __init__(self) -> None:
        self.calendars: dict[str, Calendar] = {}
        self.pending_codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: set[str] = set()
        self.page_size = 100
        self.fail_next: list[httpx.Response] = []
        self.fail_writes: list[httpx.Response] = []
        self.writes: list[tuple[str, str, str | None]] = []
        self.event_requests = 0
        self.oid = "00000000-0000-0000-1234-56789abcdef0"
        self.email = "pete@outlook.com"
        self.granted: list[str] = []
        self.transactions: dict[str, str] = {}
        self.token_error: tuple[int, dict[str, Any]] | None = None
        self.lose_responses = 0  # writes that happen but whose response never arrives
        self.last_patch: dict[str, Any] = {}

    # ------------------------------------------------------------------ test helpers

    def add_calendar(
        self, cal_id: str, name: str, *, default: bool = False, can_edit: bool = True
    ) -> Calendar:
        self.calendars[cal_id] = Calendar(cal_id, name, default, can_edit)
        return self.calendars[cal_id]

    def put(self, cal_id: str, item: dict[str, Any]) -> dict[str, Any]:
        cal = self.calendars[cal_id]
        change = secrets.token_hex(4)
        item = {
            "type": "singleInstance",
            "isAllDay": False,
            "isCancelled": False,
            "body": {"contentType": "text", "content": ""},
            "location": {"displayName": ""},
            "originalStartTimeZone": "Eastern Standard Time",
            **item,
            "changeKey": change,
            "@odata.etag": f'W/"{change}"',
        }
        cal.events[item["id"]] = item
        cal.log.append(item["id"])
        return item

    def delete(self, cal_id: str, event_id: str) -> None:
        cal = self.calendars[cal_id]
        del cal.events[event_id]
        cal.log.append(event_id)

    def expire_delta_tokens(self, cal_id: str) -> None:
        cal = self.calendars[cal_id]
        cal.expired_before = len(cal.log) + 1

    def authorize(self, url: str) -> str:
        """Play the user consenting in the browser; returns the code Entra would redirect with."""
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert parsed.netloc == "login.microsoftonline.com"
        assert query["code_challenge_method"] == "S256"
        assert "offline_access" in query["scope"].split()
        code = secrets.token_hex(8)
        self.pending_codes[code] = query
        return code

    def _calendar_of(self, event_id: str) -> Calendar | None:
        return next((c for c in self.calendars.values() if event_id in c.events), None)

    # ------------------------------------------------------------------ transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "login.microsoftonline.com" and url.path.endswith("/oauth2/v2.0/token"):
            return self._token(dict(parse_qs(request.content.decode())))
        if url.host != "graph.microsoft.com":
            return httpx.Response(404)
        if not request.headers.get("Authorization", "").startswith("Bearer ms-access-"):
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        assert 'outlook.timezone="UTC"' in request.headers.get("Prefer", "")
        parts = url.path.removeprefix("/v1.0").split("/")
        if parts == ["", "me", "calendars"]:
            return httpx.Response(
                200, json={"value": [self._calendar_entry(c) for c in self.calendars.values()]}
            )
        if len(parts) == 6 and parts[1:3] == ["me", "calendars"] and parts[4:] == ["calendarView", "delta"]:
            return self._delta(parts[3], dict(url.params))
        if (
            len(parts) == 5
            and parts[1:3] == ["me", "calendars"]
            and parts[4] == "events"
            and request.method == "POST"
        ):
            return self._insert(parts[3], json.loads(request.content))
        if len(parts) == 4 and parts[1:3] == ["me", "events"]:
            return self._one(request, parts[3])
        return httpx.Response(404)

    def _calendar_entry(self, cal: Calendar) -> dict[str, Any]:
        return {
            "id": cal.id,
            "name": cal.name,
            "hexColor": "#0f6cbd",
            "canEdit": cal.can_edit,
            "isDefaultCalendar": cal.default,
        }

    def _token(self, form: dict[str, list[str]]) -> httpx.Response:
        assert "client_secret" not in form  # a public client
        if self.token_error:
            status, body = self.token_error
            return httpx.Response(status, json=body)
        grant = form["grant_type"][0]
        if grant == "authorization_code":
            pending = self.pending_codes.pop(form["code"][0], None)
            if pending is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != pending["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if form["redirect_uri"][0] != pending["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_request", "error_codes": [50011]})
            graph = [s.rsplit("/", 1)[1] for s in pending["scope"].split() if s.startswith("https://")]
            self.granted = list(dict.fromkeys(self.granted + graph))
            refresh = f"ms-refresh-{secrets.token_hex(4)}"
            self.refresh_tokens.add(refresh)
            claims = {
                "oid": self.oid,
                "tid": "9188040d-6c67-4c5b-b112-36a304b66dad",
                "preferred_username": self.email,
            }
            return httpx.Response(
                200,
                json={
                    "access_token": f"ms-access-{secrets.token_hex(4)}",
                    "expires_in": 3600,
                    "refresh_token": refresh,
                    "scope": " ".join(
                        [*self.granted, "openid", "profile", "email"]
                    ),  # short names, like Entra
                    "id_token": f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig",
                },
            )
        if grant == "refresh_token":
            assert "https://graph.microsoft.com/.default" in form["scope"][0]
            old = form["refresh_token"][0]
            if old not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh_tokens.discard(old)  # Entra rotates refresh tokens
            new = f"ms-refresh-{secrets.token_hex(4)}"
            self.refresh_tokens.add(new)
            return httpx.Response(
                200,
                json={
                    "access_token": f"ms-access-{secrets.token_hex(4)}",
                    "expires_in": 3600,
                    "refresh_token": new,
                    "scope": " ".join(self.granted),
                },
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _delta(self, cal_id: str, params: dict[str, str]) -> httpx.Response:
        self.event_requests += 1
        if self.fail_next:
            return self.fail_next.pop(0)
        cal = self.calendars.get(cal_id)
        if cal is None:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        base = f"{GRAPH}/me/calendars/{cal_id}/calendarView/delta"
        if "$deltatoken" in params:
            position = int(params["$deltatoken"])
            if position < cal.expired_before:
                return httpx.Response(410, json={"error": {"code": "SyncStateNotFound", "message": "gone"}})
            ids = list(dict.fromkeys(cal.log[position:]))
            items = [cal.events.get(i) or {"id": i, "@removed": {"reason": "deleted"}} for i in ids]
            start = 0
        elif "$skiptoken" in params:
            kind, _, rest = params["$skiptoken"].partition(":")
            start_text, _, position_text = rest.partition(":")
            start = int(start_text)
            if kind == "d":
                ids = list(dict.fromkeys(cal.log[int(position_text) :]))
                items = [cal.events.get(i) or {"id": i, "@removed": {"reason": "deleted"}} for i in ids]
            else:
                items = list(cal.events.values())
        else:
            assert "startDateTime" in params and "endDateTime" in params
            items = list(cal.events.values())
            start = 0
        kind = "d" if "$deltatoken" in params or params.get("$skiptoken", "").startswith("d:") else "f"
        origin = params.get("$deltatoken") or params.get("$skiptoken", "::0").split(":")[2]
        page = items[start : start + self.page_size]
        body: dict[str, Any] = {"value": page}
        if start + self.page_size < len(items):
            body["@odata.nextLink"] = f"{base}?$skiptoken={kind}:{start + self.page_size}:{origin}"
        else:
            body["@odata.deltaLink"] = f"{base}?$deltatoken={len(cal.log)}"
        return httpx.Response(200, json=body)

    def _insert(self, cal_id: str, body: dict[str, Any]) -> httpx.Response:
        if self.fail_writes:
            return self.fail_writes.pop(0)
        self.writes.append(("POST", body.get("transactionId", ""), None))
        existing = self.transactions.get(body.get("transactionId", ""))
        cal = self.calendars[cal_id]
        if existing and existing in cal.events:
            return httpx.Response(201, json=cal.events[existing])
        event_id = f"AAMk{secrets.token_hex(8)}"
        self.transactions[body["transactionId"]] = event_id
        created = self.put(cal_id, {**_as_utc(body), "id": event_id})
        if self.lose_responses:
            self.lose_responses -= 1
            return httpx.Response(504)
        return httpx.Response(201, json=created)

    def _one(self, request: httpx.Request, event_id: str) -> httpx.Response:
        cal = self._calendar_of(event_id)
        if request.method == "GET":
            return httpx.Response(200, json=cal.events[event_id]) if cal else httpx.Response(404)
        if self.fail_writes:
            return self.fail_writes.pop(0)
        if_match = request.headers.get("If-Match")
        self.writes.append((request.method, event_id, if_match))
        if cal is None:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        current = cal.events[event_id]
        if if_match and if_match != current["@odata.etag"]:
            return httpx.Response(412, json={"error": {"code": "ErrorIrresolvableConflict"}})
        if request.method == "DELETE":
            self.delete(cal.id, event_id)
            return httpx.Response(204)
        patch = _as_utc(json.loads(request.content))
        self.last_patch = patch
        return httpx.Response(200, json=self.put(cal.id, {**current, **patch}))
