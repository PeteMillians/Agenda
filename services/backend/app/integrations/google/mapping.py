"""Google Calendar event JSON -> Agenda's canonical event model (ADR 0002). Pure functions.

Google sends recurring events as a master (with `recurrence` lines) plus instance overrides
(`recurringEventId` + `originalStartTime`), which map onto series masters and exception rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain import recurrence
from app.domain.clock import iso

log = logging.getLogger(__name__)


class Unsupported(Exception):
    """An event Agenda cannot represent faithfully; it is skipped and counted, not guessed at."""


@dataclass(frozen=True)
class Incoming:
    provider_id: str
    etag: str | None
    cancelled: bool
    series_provider_id: str | None = None  # set on instance overrides
    original_key: str | None = None  # occurrence key the override replaces
    content: dict[str, str] = field(default_factory=dict)  # title, description, location, timezone
    timing: dict[str, str | None] | None = None  # start_at/end_at or start_date/end_date
    rrule: str | None = None
    exdate_keys: tuple[str, ...] = ()

    @property
    def is_instance(self) -> bool:
        return self.series_provider_id is not None


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
        return Incoming(provider_id, item.get("etag"), True, series, original)

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
    )
