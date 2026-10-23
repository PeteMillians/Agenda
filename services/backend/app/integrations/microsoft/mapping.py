"""Microsoft Graph event JSON <-> Agenda's event model. Pure functions.

calendarView/delta hands out concrete occurrences (singleInstance, occurrence, exception), never
series masters, so every Outlook item becomes one Agenda event without a rule (ADR 0005). Editing
one in Agenda changes that occurrence in Outlook; whole-series edits stay in Outlook.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.clock import iso
from app.integrations.base import Incoming, Unsupported
from app.integrations.microsoft.client import etag_of, graph_time

# Windows time zone names Outlook reports in originalStartTimeZone (CLDR windowsZones, 001 rows).
# Only used to label imported events; anything unknown falls back to the calendar's time zone.
WINDOWS_ZONES = {
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Egypt Standard Time": "Africa/Cairo",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "Iran Standard Time": "Asia/Tehran",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Nepal Standard Time": "Asia/Kathmandu",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "W. Australia Standard Time": "Australia/Perth",
    "Tasmania Standard Time": "Australia/Hobart",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time": "America/Los_Angeles",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indianapolis",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Eastern Standard Time": "America/Cayenne",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Pacific SA Standard Time": "America/Santiago",
}


def _zone(name: str | None, fallback: str) -> str:
    for candidate in (WINDOWS_ZONES.get(name or "", name), fallback, "UTC"):
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return "UTC"


def _moment(value: dict[str, Any]) -> datetime:
    moment = datetime.fromisoformat(graph_time(value["dateTime"]))
    if moment.tzinfo is None:
        zone = _zone(value.get("timeZone"), "UTC")
        moment = moment.replace(tzinfo=ZoneInfo(zone))
    return moment


def parse(item: dict[str, Any], calendar_timezone: str) -> Incoming:
    provider_id = item["id"]
    if "@removed" in item or item.get("isCancelled"):
        return Incoming(provider_id, etag_of(item), True, raw=item)
    if item.get("type") == "seriesMaster":
        raise Unsupported("series masters are imported as their occurrences")
    if item.get("isAllDay"):
        # all-day items are floating dates: midnight to midnight, whatever zone is attached
        timing: dict[str, str | None] = {
            "start_at": None,
            "end_at": None,
            "start_date": date.fromisoformat(item["start"]["dateTime"][:10]).isoformat(),
            "end_date": date.fromisoformat(item["end"]["dateTime"][:10]).isoformat(),
        }
    else:
        timing = {
            "start_at": iso(_moment(item["start"])),
            "end_at": iso(_moment(item["end"])),
            "start_date": None,
            "end_date": None,
        }
    content = {
        "title": item.get("subject") or "(No title)",
        "description": ((item.get("body") or {}).get("content") or "").strip(),
        "location": (item.get("location") or {}).get("displayName") or "",
        "timezone": _zone(item.get("originalStartTimeZone"), calendar_timezone),
    }
    return Incoming(provider_id, etag_of(item), False, content=content, timing=timing, raw=item)


def _graph_moment(row: dict[str, Any], which: str, tz: ZoneInfo) -> dict[str, str]:
    if row["start_date"]:
        return {"dateTime": f"{row[f'{which}_date']}T00:00:00", "timeZone": tz.key}
    local = datetime.fromisoformat(row[f"{which}_at"]).astimezone(tz)
    return {"dateTime": local.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": tz.key}


def to_graph(row: dict[str, Any]) -> dict[str, Any]:
    """Graph accepts IANA zone names as well as Windows ones in dateTimeTimeZone."""
    if row["rrule"] or row["series_id"]:
        raise Unsupported("repeating Outlook events are edited in Outlook")
    tz = ZoneInfo(row["timezone"])
    return {
        "subject": row["title"],
        "body": {"contentType": "text", "content": row["description"]},
        "location": {"displayName": row["location"]},
        "isAllDay": row["start_date"] is not None,
        "start": _graph_moment(row, "start", tz),
        "end": _graph_moment(row, "end", tz),
    }


def client_key(event_id: str, revision: int) -> str:
    return f"{event_id}:{revision}"
