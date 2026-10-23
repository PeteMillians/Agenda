"""Google Calendar event JSON -> Agenda's canonical event model (ADR 0002). Pure functions.

Google sends recurring events as a master (with `recurrence` lines) plus instance overrides
(`recurringEventId` + `originalStartTime`), which map onto series masters and exception rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain import recurrence
from app.domain.clock import iso
from app.integrations.base import Incoming, Unsupported

__all__ = ["Incoming", "Unsupported", "client_key", "instance_id", "local_id", "parse", "to_google"]

log = logging.getLogger(__name__)


def _zone(name: str | None, fallback: str) -> str:
    for candidate in (name, fallback, "UTC"):
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return "UTC"


def _key(moment: dict[str, Any]) -> str:
    """Occurrence key of a Google start/originalStartTime: UTC ISO, or the date for all-day."""
    if "date" in moment:
        return date.fromisoformat(moment["date"]).isoformat()
    return iso(datetime.fromisoformat(moment["dateTime"]))


def _timing(item: dict[str, Any]) -> dict[str, str | None]:
    start, end = item["start"], item["end"]
    if "date" in start:
        return {
            "start_at": None,
            "end_at": None,
            "start_date": date.fromisoformat(start["date"]).isoformat(),
            "end_date": date.fromisoformat(end["date"]).isoformat(),  # Google's end date is exclusive too
        }
    return {
        "start_at": iso(datetime.fromisoformat(start["dateTime"])),
        "end_at": iso(datetime.fromisoformat(end["dateTime"])),
        "start_date": None,
        "end_date": None,
    }


def _exdate_keys(line: str, tz: ZoneInfo, all_day: bool) -> list[str]:
    """EXDATE;TZID=America/New_York:20261027T090000,20261103T090000 | EXDATE;VALUE=DATE:20261027"""
    head, _, values = line.partition(":")
    params = dict(p.split("=", 1) for p in head.split(";")[1:] if "=" in p)
    zone = ZoneInfo(params["TZID"]) if "TZID" in params else tz
    keys = []
    for value in filter(None, values.split(",")):
        if len(value) == 8 or params.get("VALUE") == "DATE":
            day = datetime.strptime(value[:8], "%Y%m%d").date()
            keys.append(day.isoformat())
        elif value.endswith("Z"):
            keys.append(iso(datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)))
        else:
            keys.append(iso(datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone)))
    if all_day:  # a timed EXDATE on an all-day series still names a day
        keys = [k[:10] for k in keys]
    return keys


def parse(item: dict[str, Any], calendar_timezone: str) -> Incoming:
    provider_id = item["id"]
    cancelled = item.get("status") == "cancelled"
    series = item.get("recurringEventId")
    original = _key(item["originalStartTime"]) if "originalStartTime" in item else None
    if cancelled and "start" not in item:
        # deletions in incremental sync carry little more than the id
        return Incoming(provider_id, item.get("etag"), True, series, original, raw=item)

    tz_name = _zone(item.get("start", {}).get("timeZone"), calendar_timezone)
    timing = _timing(item)
    content = {
        "title": item.get("summary") or "(No title)",
        "description": item.get("description", ""),
        "location": item.get("location", ""),
        "timezone": tz_name,
    }
    rule: str | None = None
    exdates: list[str] = []
    for line in item.get("recurrence", []):
        kind = line.split(":", 1)[0].split(";", 1)[0].upper()
        if kind == "RRULE":
            if rule is not None:
                raise Unsupported("more than one RRULE")
            try:
                rule = recurrence.normalize(line.split(":", 1)[1], ZoneInfo(tz_name))
            except recurrence.InvalidRule as exc:
                raise Unsupported(f"rule not supported: {exc}") from exc
        elif kind == "EXDATE":
            exdates += _exdate_keys(line, ZoneInfo(tz_name), timing["start_date"] is not None)
        else:
            raise Unsupported(f"{kind} is not supported")
    return Incoming(
        provider_id=provider_id,
        etag=item.get("etag"),
        cancelled=cancelled,
        series_provider_id=series,
        original_key=original,
        content=content,
        timing=timing,
        rrule=rule,
        exdate_keys=tuple(exdates),
        raw=item,
    )


# --------------------------------------------------------------------------- Agenda -> Google


def _google_rule(rule: str, tz: ZoneInfo, all_day: bool) -> str:
    """Agenda keeps UNTIL as local wall time; RFC 5545 (and Google) want UTC, or a date."""
    parts = recurrence.parse_parts(rule)
    until = parts.get("UNTIL")
    if until:
        if all_day:
            parts["UNTIL"] = until[:8]
        else:
            local = datetime.strptime(until, recurrence.LOCAL_FORMAT)
            parts["UNTIL"] = recurrence.to_utc(local, tz).strftime("%Y%m%dT%H%M%SZ")
    return "RRULE:" + recurrence.format_parts(parts)


def _moment(row: dict[str, Any], which: str, tz: ZoneInfo) -> dict[str, str]:
    if row["start_date"]:
        return {"date": row[f"{which}_date"]}
    local = datetime.fromisoformat(row[f"{which}_at"]).astimezone(tz)
    return {"dateTime": local.isoformat(), "timeZone": tz.key}


def to_google(row: dict[str, Any]) -> dict[str, Any]:
    """The event body for a single event, series master, or (with series_id) instance override."""
    tz = ZoneInfo(row["timezone"])
    body: dict[str, Any] = {
        "summary": row["title"],
        "description": row["description"],
        "location": row["location"],
        "start": _moment(row, "start", tz),
        "end": _moment(row, "end", tz),
        "status": "confirmed",
    }
    if row["series_id"] is None:
        body["recurrence"] = (
            [_google_rule(row["rrule"], tz, row["start_date"] is not None)] if row["rrule"] else []
        )
    return body


def instance_id(master_provider_id: str, key: str) -> str:
    """Google's id for one occurrence of a series: `<master>_<original start, UTC basic format>`
    (or `_<YYYYMMDD>` for all-day series)."""
    if len(key) == 10:
        return f"{master_provider_id}_{key.replace('-', '')}"
    return f"{master_provider_id}_{datetime.fromisoformat(key).astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def client_key(event_id: str) -> str:
    """A Google-valid event id (base32hex, 5-1024 chars) derived from Agenda's UUID."""
    return event_id.replace("-", "")


def local_id(provider_id: str) -> str | None:
    """The Agenda UUID behind a `client_key`, if `provider_id` is one."""
    if len(provider_id) != 32 or any(c not in "0123456789abcdef" for c in provider_id):
        return None
    p = provider_id
    return f"{p[:8]}-{p[8:12]}-{p[12:16]}-{p[16:20]}-{p[20:]}"
