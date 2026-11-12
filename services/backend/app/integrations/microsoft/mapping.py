"""Microsoft Graph event JSON <-> Agenda's canonical event model (ADR 0002, ADR 0005). Pure functions.

Graph describes recurrence as a `patternedRecurrence` rather than an RRULE, names time zones in
Windows form ("Eastern Standard Time") as often as IANA form, and gives instances an
`originalStart` in UTC. Patterns translate both ways for the rules Agenda creates; anything
else is skipped on import and refused on edit, never approximated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal.windows_tz import win_tz

from app.application.events import Timing
from app.domain import recurrence
from app.domain.clock import iso
from app.integrations.errors import Unrepresentable
from app.integrations.model import Incoming, Outgoing, Unsupported

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
INDEX = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
INDEX_NAME = {v: k for k, v in INDEX.items()}


def zone(name: str | None, fallback: str = "UTC") -> str:
    """An IANA name for a Graph time zone, which may be IANA or Windows."""
    for candidate in (name, win_tz.get(name or ""), fallback, "UTC"):
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return "UTC"


def _moment(value: dict[str, Any]) -> datetime:
    """A Graph dateTimeTimeZone. Its 7-digit fractions are cut to what Python parses."""
    text = value["dateTime"].rstrip("Z")
    whole, _, fraction = text.partition(".")
    parsed = datetime.fromisoformat(whole + (f".{fraction[:6]}" if fraction else ""))
    return parsed.replace(tzinfo=ZoneInfo(zone(value.get("timeZone"))))


def _day(value: dict[str, Any], tz: str) -> date:
    """All-day events are midnight to midnight; Graph may express that midnight in UTC."""
    moment = _moment(value)
    if moment.time() == time():
        return moment.date()
    return moment.astimezone(ZoneInfo(tz)).date()


def occurrence_key(original_start: str, all_day: bool, tz: str) -> str:
    moment = datetime.fromisoformat(original_start.rstrip("Z").split(".")[0]).replace(tzinfo=UTC)
    if all_day:
        return (
            moment.date() if moment.time() == time() else moment.astimezone(ZoneInfo(tz)).date()
        ).isoformat()
    return iso(moment)


# --------------------------------------------------------------------------- recurrence


def pattern_to_rrule(rec: dict[str, Any], all_day: bool) -> str:
    pattern, span = rec.get("pattern") or {}, rec.get("range") or {}
    kind = pattern.get("type")
    parts: dict[str, str] = {}
    days = ",".join(CODES[DAYS.index(d)] for d in pattern.get("daysOfWeek") or [])
    index = INDEX.get(pattern.get("index") or "first", 1)
    if kind == "daily":
        parts["FREQ"] = "DAILY"
    elif kind == "weekly":
        parts["FREQ"] = "WEEKLY"
        if days:
            parts["BYDAY"] = days
        first_day = pattern.get("firstDayOfWeek", "sunday")
        if int(pattern.get("interval") or 1) > 1:
            parts["WKST"] = CODES[DAYS.index(first_day)]
    elif kind == "absoluteMonthly":
        parts |= {"FREQ": "MONTHLY", "BYMONTHDAY": str(pattern["dayOfMonth"])}
    elif kind == "relativeMonthly":
        parts |= {"FREQ": "MONTHLY", "BYDAY": days, "BYSETPOS": str(index)}
    elif kind == "absoluteYearly":
        parts |= {
            "FREQ": "YEARLY",
            "BYMONTH": str(pattern["month"]),
            "BYMONTHDAY": str(pattern["dayOfMonth"]),
        }
    elif kind == "relativeYearly":
        parts |= {"FREQ": "YEARLY", "BYMONTH": str(pattern["month"]), "BYDAY": days, "BYSETPOS": str(index)}
    else:
        raise Unsupported(f"recurrence pattern {kind!r} is not supported")
    interval = int(pattern.get("interval") or 1)
    if interval > 1:
        parts["INTERVAL"] = str(interval)
    if span.get("type") == "endDate":
        end = date.fromisoformat(span["endDate"])
        parts["UNTIL"] = end.strftime("%Y%m%d") if all_day else end.strftime("%Y%m%dT235959")
    elif span.get("type") == "numbered":
        parts["COUNT"] = str(span["numberOfOccurrences"])
    elif span.get("type") not in ("noEnd", None):
        raise Unsupported(f"recurrence range {span.get('type')!r} is not supported")
    return recurrence.format_parts(parts)


def _byday(value: str) -> list[tuple[int | None, str]]:
    found = []
    for entry in filter(None, value.split(",")):
        code, ordinal = entry[-2:], entry[:-2]
        if code not in CODES:
            raise Unrepresentable(f"BYDAY {entry}")
        found.append((int(ordinal) if ordinal else None, DAYS[CODES.index(code)]))
    return found


def rrule_to_pattern(rule: str, first: Timing, tz: str) -> dict[str, Any]:
    """Raises Unrepresentable for a rule a patternedRecurrence cannot express."""
    parts = recurrence.parse_parts(rule)
    start_local = None if first.all_day else first.start_at.astimezone(ZoneInfo(tz))  # type: ignore[union-attr]
    start_day: date = first.start_date if start_local is None else start_local.date()  # type: ignore[assignment]
    allowed = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "COUNT", "UNTIL", "WKST"}
    if extra := set(parts) - allowed:
        raise Unrepresentable(f"{', '.join(sorted(extra))} cannot be stored in Outlook")
    freq = parts["FREQ"]
    pattern: dict[str, Any] = {"interval": int(parts.get("INTERVAL", "1"))}
    days = _byday(parts.get("BYDAY", ""))
    ordinals = {o for o, _ in days}
    setpos = parts.get("BYSETPOS")
    if setpos is not None and (setpos not in ("1", "2", "3", "4", "-1") or not days):
        raise Unrepresentable("BYSETPOS")
    relative_index = int(setpos) if setpos else next(iter(ordinals)) if days and ordinals != {None} else None
    if days and len(ordinals) > 1:
        raise Unrepresentable("mixed BYDAY ordinals")
    if relative_index is not None and relative_index not in INDEX_NAME:
        raise Unrepresentable("BYDAY ordinal")
    month_day = parts.get("BYMONTHDAY")
    if month_day is not None and (not month_day.isdigit() or "," in month_day):
        raise Unrepresentable("BYMONTHDAY")
    if freq == "DAILY":
        if days or month_day or "BYMONTH" in parts:
            raise Unrepresentable("a daily rule with BY parts")
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        if ordinals - {None} or month_day or "BYMONTH" in parts:
            raise Unrepresentable("a weekly rule with ordinals")
        pattern |= {
            "type": "weekly",
            "daysOfWeek": [d for _, d in days] or [DAYS[start_day.weekday()]],
            "firstDayOfWeek": DAYS[CODES.index(parts.get("WKST", "MO"))],
        }
    elif freq in ("MONTHLY", "YEARLY"):
        yearly = freq == "YEARLY"
        if yearly:
            month = parts.get("BYMONTH", str(start_day.month))
            if not month.isdigit():
                raise Unrepresentable("BYMONTH")
            pattern["month"] = int(month)
        elif "BYMONTH" in parts:
            raise Unrepresentable("BYMONTH on a monthly rule")
        if days:
            if relative_index is None or month_day:
                raise Unrepresentable("weekdays without a position")
            pattern |= {
                "type": "relativeYearly" if yearly else "relativeMonthly",
                "daysOfWeek": [d for _, d in days],
                "index": INDEX_NAME[relative_index],
            }
        else:
            pattern |= {
                "type": "absoluteYearly" if yearly else "absoluteMonthly",
                "dayOfMonth": int(month_day or start_day.day),
            }
    else:
        raise Unrepresentable(f"FREQ={freq}")
    span: dict[str, Any] = {"startDate": start_day.isoformat(), "recurrenceTimeZone": tz}
    if "COUNT" in parts:
        span |= {"type": "numbered", "numberOfOccurrences": int(parts["COUNT"])}
    elif "UNTIL" in parts:
        until = parts["UNTIL"]
        end = datetime.strptime(until[:8], "%Y%m%d").date()
        if "T" in until and start_local is not None:
            # UNTIL just before an occurrence's time (a split) means the day before is the last
            last = datetime.strptime(until[:15], recurrence.LOCAL_FORMAT).time()
            if last < start_local.time():
                end -= timedelta(days=1)
        span |= {"type": "endDate", "endDate": end.isoformat()}
    else:
        span["type"] = "noEnd"
    return {"pattern": pattern, "range": span}


# --------------------------------------------------------------------------- Graph -> Agenda


def parse(item: dict[str, Any], fallback_tz: str, *, series_all_day: bool | None = None) -> Incoming:
    """A single event, series master, or instance (exception or occurrence)."""
    tz = zone(item.get("originalStartTimeZone"), fallback_tz)
    all_day = bool(item.get("isAllDay"))
    kind = item.get("type", "singleInstance")
    series = item.get("seriesMasterId") if kind in ("exception", "occurrence") else None
    original = None
    if series and item.get("originalStart"):
        original = occurrence_key(
            item["originalStart"], all_day if series_all_day is None else series_all_day, tz
        )
    if all_day:
        timing: dict[str, str | None] = {
            "start_at": None,
            "end_at": None,
            "start_date": _day(item["start"], tz).isoformat(),
            "end_date": _day(item["end"], tz).isoformat(),
        }
    else:
        timing = {
            "start_at": iso(_moment(item["start"])),
            "end_at": iso(_moment(item["end"])),
            "start_date": None,
            "end_date": None,
        }
    rule = None
    if kind == "seriesMaster":
        if not item.get("recurrence"):
            raise Unsupported("series without a pattern")
        try:
            rule = recurrence.normalize(pattern_to_rrule(item["recurrence"], all_day), ZoneInfo(tz))
        except recurrence.InvalidRule as exc:
            raise Unsupported(f"rule not supported: {exc}") from exc
    body = item.get("body") or {}
    return Incoming(
        provider_id=item["id"],
        etag=item.get("@odata.etag") or item.get("changeKey"),
        cancelled=bool(item.get("isCancelled")),
        series_provider_id=series,
        original_key=original,
        content={
            "title": item.get("subject") or "(No title)",
            "description": body.get("content", "") if body.get("contentType", "text") == "text" else "",
            "location": (item.get("location") or {}).get("displayName", ""),
            "timezone": tz,
        },
        timing=timing,
        rrule=rule,
    )


# --------------------------------------------------------------------------- Agenda -> Graph


def _local(moment: datetime, tz: str) -> str:
    return moment.astimezone(ZoneInfo(tz)).replace(tzinfo=None).isoformat(timespec="seconds")


def body(out: Outgoing, *, instance: bool = False) -> dict[str, Any]:
    t = out.timing
    if t.all_day:
        start = f"{t.start_date.isoformat()}T00:00:00"  # type: ignore[union-attr]
        end = f"{t.end_date.isoformat()}T00:00:00"  # type: ignore[union-attr]
    else:
        start, end = _local(t.start_at, out.timezone), _local(t.end_at, out.timezone)  # type: ignore[arg-type]
    result: dict[str, Any] = {
        "subject": out.title,
        "body": {"contentType": "text", "content": out.description},
        "location": {"displayName": out.location},
        "isAllDay": t.all_day,
        "start": {"dateTime": start, "timeZone": out.timezone},
        "end": {"dateTime": end, "timeZone": out.timezone},
    }
    if not instance:
        result["recurrence"] = rrule_to_pattern(out.rrule, t, out.timezone) if out.rrule else None
    return result
