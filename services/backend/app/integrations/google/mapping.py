"""Google Calendar event JSON <-> Agenda's canonical event model (ADR 0002). Pure functions.

Google sends recurring events as a master (with `recurrence` lines) plus instance overrides
(`recurringEventId` + `originalStartTime`), which map onto series masters and exception rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain import recurrence
from app.domain.clock import iso
from app.integrations.model import Incoming, Outgoing, Unsupported


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
    if "date" in moment and moment["date"]:
        return date.fromisoformat(moment["date"]).isoformat()
    return iso(datetime.fromisoformat(moment["dateTime"]))


def _timing(item: dict[str, Any]) -> dict[str, str | None]:
    start, end = item["start"], item["end"]
    if start.get("date"):
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
    for line in item.get("recurrence") or []:
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


# --------------------------------------------------------------------------- Agenda -> Google


def _basic(key: str, all_day: bool, tz: ZoneInfo | None = None) -> str:
    """An occurrence key in iCalendar basic form: 20261027, 20261027T130000Z, or local with `tz`."""
    if all_day:
        return date.fromisoformat(key[:10]).strftime("%Y%m%d")
    moment = datetime.fromisoformat(key)
    if tz is not None:
        return moment.astimezone(tz).strftime("%Y%m%dT%H%M%S")
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def instance_id(master_id: str, key: str, all_day: bool) -> str:
    """Google names an occurrence `<series id>_<original start>`, in UTC for timed events."""
    return f"{master_id}_{_basic(key, all_day)}"


def _moments(out: Outgoing) -> dict[str, Any]:
    t = out.timing
    if t.all_day:
        return {
            "start": {"date": t.start_date.isoformat(), "dateTime": None, "timeZone": None},  # type: ignore[union-attr]
            "end": {"date": t.end_date.isoformat(), "dateTime": None, "timeZone": None},  # type: ignore[union-attr]
        }
    tz = ZoneInfo(out.timezone)
    return {
        "start": {"dateTime": t.start_at.astimezone(tz).isoformat(), "timeZone": out.timezone, "date": None},  # type: ignore[union-attr]
        "end": {"dateTime": t.end_at.astimezone(tz).isoformat(), "timeZone": out.timezone, "date": None},  # type: ignore[union-attr]
    }


def recurrence_lines(out: Outgoing) -> list[str]:
    if out.rrule is None:
        return []
    parts = recurrence.parse_parts(out.rrule)
    until = parts.get("UNTIL")
    if until and not out.timing.all_day and "T" in until:
        # stored as local wall time; RFC 5545 wants UTC when DTSTART has a time zone
        local = datetime.strptime(until, recurrence.LOCAL_FORMAT).replace(tzinfo=ZoneInfo(out.timezone))
        parts["UNTIL"] = local.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [f"RRULE:{recurrence.format_parts(parts)}"]
    if out.exdate_keys:
        if out.timing.all_day:
            lines.append("EXDATE;VALUE=DATE:" + ",".join(_basic(k, True) for k in out.exdate_keys))
        else:
            tz = ZoneInfo(out.timezone)
            values = ",".join(_basic(k, False, tz) for k in out.exdate_keys)
            lines.append(f"EXDATE;TZID={out.timezone}:{values}")
    return lines


def body(out: Outgoing, *, instance: bool = False) -> dict[str, Any]:
    """PATCH/insert body. Only the fields Agenda owns, so attendees, reminders, and colours set in
    Google survive an edit made here."""
    result: dict[str, Any] = {
        "summary": out.title,
        "description": out.description,
        "location": out.location,
        **_moments(out),
    }
    if instance:
        result["status"] = "confirmed"
    else:
        result["recurrence"] = recurrence_lines(out)
    return result
