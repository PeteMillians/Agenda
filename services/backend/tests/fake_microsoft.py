"""An in-process fake of the Microsoft identity platform and Graph calendar endpoints Agenda uses.

Series behave like Outlook's: a master with a patternedRecurrence, occurrences generated from it
(each with an id of its own), exceptions that replace an occurrence, and cancelled occurrences
that simply stop being returned. Times come back in UTC, as Agenda asks with `Prefer`. Writes
honour If-Match with 412. Pattern expansion here is written independently of Agenda's.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
from dateutil import rrule as du
from tzlocal.windows_tz import win_tz

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
INDEX = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _etag() -> str:
    return f'W/"{secrets.token_hex(6)}"'


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(win_tz.get(name, name))  # Outlook uses Windows names as often as IANA ones


def _parse_local(value: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(value["dateTime"].split(".")[0]).replace(tzinfo=_zone(value["timeZone"]))


def _graph_time(moment: datetime) -> dict[str, str]:
    return {"dateTime": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


@dataclass
class Calendar:
    id: str
    name: str
    default: bool = False
    can_edit: bool = True
    events: dict[str, dict[str, Any]] = field(default_factory=dict)


class FakeMicrosoft:
    def __init__(self) -> None:
        self.calendars: dict[str, Calendar] = {}
        self.pending_codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: set[str] = set()
        self.page_size = 100
        self.fail_next: list[httpx.Response] = []  # injected errors for event listing
        self.offline = False
        self.before_write: Callable[[str, str], None] | None = None
        self.subject = "ms-oid-1"
        self.email = "pete@outlook.com"
        self.event_requests = 0

    # ------------------------------------------------------------------ test helpers

    def add_calendar(
        self, cal_id: str, name: str, *, default: bool = False, can_edit: bool = True
    ) -> Calendar:
        self.calendars[cal_id] = Calendar(cal_id, name, default, can_edit)
        return self.calendars[cal_id]

    def put(self, cal_id: str, item: dict[str, Any]) -> None:
        """Create or replace a single event or series master (Graph-shaped, local times)."""
        cal = self.calendars[cal_id]
        old = cal.events.get(item["id"], {})
        stored = {
            "type": "seriesMaster" if item.get("recurrence") else "singleInstance",
            "isAllDay": False,
            "body": {"contentType": "text", "content": ""},
            "location": {"displayName": ""},
            "isCancelled": False,
            **item,
            "@odata.etag": _etag(),
            "_cancelled": old.get("_cancelled", set()),
            "_exceptions": old.get("_exceptions", {}),
        }
        stored["originalStartTimeZone"] = stored["start"]["timeZone"]
        cal.events[item["id"]] = stored
        if stored["type"] == "seriesMaster":
            valid = set(self._keys(stored))
            stored["_cancelled"] = {k for k in stored["_cancelled"] if k in valid}
            stored["_exceptions"] = {k: v for k, v in stored["_exceptions"].items() if k in valid}

    def edit(self, cal_id: str, event_id: str, **changes: Any) -> None:
        cal = self.calendars[cal_id]
        if event_id in cal.events:
            master = cal.events[event_id]
            self.put(cal_id, {k: v for k, v in {**master, **changes}.items() if not k.startswith(("_", "@"))})
            return
        master, key = self._locate(cal, event_id)
        assert master is not None and key is not None, event_id
        current = master["_exceptions"].get(key) or self._occurrence(master, key)
        master["_exceptions"][key] = {**current, **changes, "type": "exception", "@odata.etag": _etag()}
        master["@odata.etag"] = _etag()  # Exchange changes the master when an occurrence changes

    def delete(self, cal_id: str, event_id: str) -> None:
        cal = self.calendars[cal_id]
        if event_id in cal.events:
            del cal.events[event_id]
            return
        master, key = self._locate(cal, event_id)
        assert master is not None and key is not None, event_id
        master["_exceptions"].pop(key, None)
        master["_cancelled"].add(key)
        master["@odata.etag"] = _etag()

    def get(self, cal_id: str, event_id: str) -> dict[str, Any] | None:
        cal = self.calendars[cal_id]
        if event_id in cal.events:
            return self._public(cal.events[event_id])
        master, key = self._locate(cal, event_id)
        if master is None or key is None or key in master["_cancelled"] or key not in self._keys(master):
            return None
        return self._public(master["_exceptions"].get(key) or self._occurrence(master, key))

    def occurrence_id(self, master_id: str, key: str) -> str:
        return f"{master_id}.occ.{key}"

    def authorize(self, url: str) -> str:
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert query["code_challenge_method"] == "S256"
        assert "offline_access" in query["scope"]
        code = secrets.token_hex(8)
        self.pending_codes[code] = query
        return code

    # ------------------------------------------------------------------ series

    def _locate(self, cal: Calendar, event_id: str) -> tuple[dict[str, Any] | None, str | None]:
        master_id, sep, key = event_id.partition(".occ.")
        if not sep:
            return None, None
        return cal.events.get(master_id), key

    def _rule(self, master: dict[str, Any]) -> du.rrule:
        rec, start = master["recurrence"], _parse_local(master["start"])
        pattern, span = rec["pattern"], rec["range"]
        kind = pattern["type"]
        kwargs: dict[str, Any] = {
            "interval": pattern.get("interval", 1),
            "dtstart": start.replace(tzinfo=None),
        }
        days = [getattr(du, d[:2].upper()) for d in pattern.get("daysOfWeek", [])]
        if kind == "daily":
            freq = du.DAILY
        elif kind == "weekly":
            freq, kwargs["byweekday"] = du.WEEKLY, days
            kwargs["wkst"] = getattr(du, pattern.get("firstDayOfWeek", "sunday")[:2].upper())
        elif kind == "absoluteMonthly":
            freq, kwargs["bymonthday"] = du.MONTHLY, pattern["dayOfMonth"]
        elif kind == "relativeMonthly":
            freq = du.MONTHLY
            kwargs["byweekday"] = [d(INDEX[pattern.get("index", "first")]) for d in days]
        elif kind == "absoluteYearly":
            freq = du.YEARLY
            kwargs |= {"bymonth": pattern["month"], "bymonthday": pattern["dayOfMonth"]}
        else:
            raise AssertionError(kind)
        if span["type"] == "endDate":
            kwargs["until"] = datetime.combine(date.fromisoformat(span["endDate"]), datetime.max.time())
        elif span["type"] == "numbered":
            kwargs["count"] = span["numberOfOccurrences"]
        return du.rrule(freq, **kwargs)

    def _keys(
        self, master: dict[str, Any], lo: datetime | None = None, hi: datetime | None = None
    ) -> list[str]:
        tz = _zone(master["start"]["timeZone"])
        rule = self._rule(master)
        lo_local = (lo or datetime(1970, 1, 1, tzinfo=UTC)).astimezone(tz).replace(tzinfo=None) - timedelta(
            days=1
        )
        hi_local = (hi or datetime(2100, 1, 1, tzinfo=UTC)).astimezone(tz).replace(tzinfo=None) + timedelta(
            days=1
        )
        keys = []
        for local in rule.between(lo_local, hi_local, inc=True):
            if master["isAllDay"]:
                keys.append(local.date().isoformat())
            else:
                keys.append(local.replace(tzinfo=tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return keys

    def _occurrence(self, master: dict[str, Any], key: str) -> dict[str, Any]:
        tz = _zone(master["start"]["timeZone"])
        length = _parse_local(master["end"]) - _parse_local(master["start"])
        if master["isAllDay"]:
            start = datetime.combine(date.fromisoformat(key), datetime.min.time(), tz)
            length = timedelta(days=round(length / timedelta(days=1)))
        else:
            start = datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(tz)
        return {
            "id": self.occurrence_id(master["id"], key),
            "type": "occurrence",
            "seriesMasterId": master["id"],
            "subject": master["subject"],
            "body": master["body"],
            "location": master["location"],
            "isAllDay": master["isAllDay"],
            "isCancelled": False,
            "start": {
                "dateTime": start.replace(tzinfo=None).isoformat(),
                "timeZone": master["start"]["timeZone"],
            },
            "end": {
                "dateTime": (start + length).replace(tzinfo=None).isoformat(),
                "timeZone": master["start"]["timeZone"],
            },
            "originalStartTimeZone": master["start"]["timeZone"],
            "_key": key,
            "@odata.etag": f'W/"occ-{key}"',
        }

    def _public(self, item: dict[str, Any]) -> dict[str, Any]:
        """As Graph returns it with Prefer: outlook.timezone="UTC"."""
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        if item.get("isAllDay"):
            # all-day events come back as midnight to midnight in the requested zone
            out["start"] = {
                "dateTime": item["start"]["dateTime"][:10] + "T00:00:00.0000000",
                "timeZone": "UTC",
            }
            out["end"] = {"dateTime": item["end"]["dateTime"][:10] + "T00:00:00.0000000", "timeZone": "UTC"}
        else:
            out["start"] = _graph_time(_parse_local(item["start"]))
            out["end"] = _graph_time(_parse_local(item["end"]))
        if "_key" in item:
            key = item["_key"]
            out["originalStart"] = f"{key}T00:00:00Z" if len(key) == 10 else key
        return out

    def _instances(self, master: dict[str, Any], lo: datetime, hi: datetime) -> list[dict[str, Any]]:
        found = []
        for key in self._keys(master, lo, hi):
            if key in master["_cancelled"]:
                continue
            item = master["_exceptions"].get(key) or self._occurrence(master, key)
            start, end = _parse_local(item["start"]), _parse_local(item["end"])
            if start < hi and end > lo:
                found.append(self._public(item))
        return found

    # ------------------------------------------------------------------ transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "login.microsoftonline.com" and url.path == "/consumers/oauth2/v2.0/token":
            return self._token({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if url.host != "graph.microsoft.com":
            return httpx.Response(404)
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        if not request.headers.get("Authorization", "").startswith("Bearer access-"):
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        parts = [unquote(p) for p in url.path.removeprefix("/v1.0/me/").split("/")]
        params = dict(url.params)
        if parts == ["calendars"]:
            return self._page([self._calendar_entry(c) for c in self.calendars.values()], url, params)
        if len(parts) == 3 and parts[0] == "calendars" and parts[2] == "events":
            cal = self.calendars.get(parts[1])
            if cal is None:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            if request.method == "POST":
                return self._create(cal, json.loads(request.content))
            self.event_requests += 1
            if self.fail_next:
                return self.fail_next.pop(0)
            return self._page([self._public(e) for e in cal.events.values()], url, params)
        if parts[0] == "events" and len(parts) == 3 and parts[2] == "instances":
            cal, master = self._find_master(parts[1])
            if master is None:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            lo = datetime.fromisoformat(params["startDateTime"].replace("Z", "+00:00"))
            hi = datetime.fromisoformat(params["endDateTime"].replace("Z", "+00:00"))
            return self._page(self._instances(master, lo, hi), url, params)
        if parts[0] == "events" and len(parts) == 2:
            return self._event(request, parts[1])
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})

    def _page(self, items: list[dict[str, Any]], url: httpx.URL, params: dict[str, str]) -> httpx.Response:
        skip = int(params.get("$skip", "0"))
        size = min(int(params.get("$top", str(self.page_size))), self.page_size)
        body: dict[str, Any] = {"value": items[skip : skip + size]}
        if skip + size < len(items):
            query = {k: v for k, v in params.items() if k != "$skip"} | {"$skip": str(skip + size)}
            body["@odata.nextLink"] = f"https://graph.microsoft.com{url.path}?{urlencode(query)}"
        return httpx.Response(200, json=body)

    def _calendar_entry(self, cal: Calendar) -> dict[str, Any]:
        return {
            "id": cal.id,
            "name": cal.name,
            "hexColor": "#0078d4",
            "canEdit": cal.can_edit,
            "isDefaultCalendar": cal.default,
        }

    def _find_master(self, event_id: str) -> tuple[Calendar | None, dict[str, Any] | None]:
        for cal in self.calendars.values():
            if event_id in cal.events:
                return cal, cal.events[event_id]
        return None, None

    def _create(self, cal: Calendar, body: dict[str, Any]) -> httpx.Response:
        event_id = f"AAMk{secrets.token_hex(5)}"
        item = {**body, "id": event_id}
        if not item.get("recurrence"):
            item.pop("recurrence", None)
        self.put(cal.id, item)
        return httpx.Response(201, json=self._public(cal.events[event_id]))

    def _event(self, request: httpx.Request, event_id: str) -> httpx.Response:
        cal = next((c for c in self.calendars.values() if self.get(c.id, event_id) is not None), None)
        if cal is None:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        if request.method == "GET":
            return httpx.Response(200, json=self.get(cal.id, event_id))
        if self.before_write:
            self.before_write(cal.id, event_id)
            if self.get(cal.id, event_id) is None:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        current = self.get(cal.id, event_id)
        assert current is not None
        expected = request.headers.get("If-Match")
        if expected and expected != current["@odata.etag"]:
            return httpx.Response(412, json={"error": {"code": "ErrorIrresolvableConflict"}})
        if request.method == "DELETE":
            self.delete(cal.id, event_id)
            return httpx.Response(204)
        if request.method == "PATCH":
            changes = json.loads(request.content)
            if event_id in cal.events:
                if changes.get("recurrence") is None:
                    changes.pop("recurrence", None)
                self.edit(cal.id, event_id, **changes)
            else:
                self.edit(cal.id, event_id, **{k: v for k, v in changes.items() if k != "recurrence"})
            return httpx.Response(200, json=self.get(cal.id, event_id))
        return httpx.Response(405)

    def _token(self, form: dict[str, str]) -> httpx.Response:
        grant = form["grant_type"]
        if grant == "authorization_code":
            pending = self.pending_codes.pop(form["code"], None)
            if pending is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != pending["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if form["redirect_uri"] != pending["redirect_uri"] or "client_secret" in form:
                return httpx.Response(400, json={"error": "invalid_request"})
            refresh = f"refresh-{secrets.token_hex(4)}"
            self.refresh_tokens.add(refresh)
            id_token = f"{_b64({'alg': 'none'})}.{_b64({'oid': self.subject, 'email': self.email})}.sig"
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
            if form["refresh_token"] not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            rotated = f"refresh-{secrets.token_hex(4)}"  # Microsoft rotates refresh tokens
            self.refresh_tokens.add(rotated)
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{secrets.token_hex(4)}",
                    "expires_in": 3600,
                    "refresh_token": rotated,
                },
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})
