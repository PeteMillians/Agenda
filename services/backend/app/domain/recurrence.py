"""RFC 5545 recurrence helpers (docs/adr/0002-recurrence-model.md). Pure functions, no I/O.

Timed rules are evaluated in the event's local wall time (naive datetimes) and converted to UTC
afterwards, so "every Tuesday 09:00" stays at 09:00 across DST changes. UNTIL is stored as local
wall time, never with a trailing Z.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

ALLOWED_FREQ = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
MAX_OCCURRENCES = 1000
LOCAL_FORMAT = "%Y%m%dT%H%M%S"


class InvalidRule(ValueError):
    pass


# --------------------------------------------------------------------------- rule text


def parse_parts(rule: str) -> dict[str, str]:
    text = rule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :]
    parts: dict[str, str] = {}
    for chunk in filter(None, text.split(";")):
        key, sep, value = chunk.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise InvalidRule(f"malformed rule part {chunk!r}")
        parts[key.strip().upper()] = value.strip().upper()
    return parts


def format_parts(parts: dict[str, str]) -> str:
    ordered = {"FREQ": parts["FREQ"], **{k: v for k, v in parts.items() if k != "FREQ"}}
    return ";".join(f"{k}={v}" for k, v in ordered.items())


def normalize(rule: str, tz: ZoneInfo) -> str:
    """Validate a user-supplied RRULE and return its canonical stored form."""
    parts = parse_parts(rule)
    if parts.get("FREQ") not in ALLOWED_FREQ:
        raise InvalidRule(f"FREQ must be one of {', '.join(ALLOWED_FREQ)}")
    if "COUNT" in parts and "UNTIL" in parts:
        raise InvalidRule("COUNT and UNTIL cannot both be set")
    if "COUNT" in parts and not (parts["COUNT"].isdigit() and 1 <= int(parts["COUNT"]) <= MAX_OCCURRENCES):
        raise InvalidRule(f"COUNT must be between 1 and {MAX_OCCURRENCES}")
    until = parts.get("UNTIL")
    if until and until.endswith("Z"):
        try:
            utc = datetime.strptime(until, LOCAL_FORMAT + "Z").replace(tzinfo=UTC)
        except ValueError as exc:
            raise InvalidRule(f"bad UNTIL {until!r}") from exc
        parts["UNTIL"] = utc.astimezone(tz).strftime(LOCAL_FORMAT)
    canonical = format_parts(parts)
    try:
        rrulestr(canonical, dtstart=datetime(2000, 1, 1))
    except (ValueError, TypeError) as exc:
        raise InvalidRule(str(exc)) from exc
    return canonical


def _rule(rule: str, dtstart: datetime) -> rrule:
    parsed = rrulestr(rule, dtstart=dtstart)
    assert isinstance(parsed, rrule)
    return parsed


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time())


def to_local(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz).replace(tzinfo=None)


def to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    return local.replace(tzinfo=tz).astimezone(UTC)


# --------------------------------------------------------------------------- expansion


def expand_timed(
    rule: str,
    start: datetime,
    tz: ZoneInfo,
    duration: timedelta,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """UTC starts of the occurrences that overlap [window_start, window_end)."""
    generator = _rule(rule, to_local(start, tz))
    # pad by a day on each side: local/UTC offsets shift the window edges
    lo = to_local(window_start - duration, tz) - timedelta(days=1)
    hi = to_local(window_end, tz) + timedelta(days=1)
    starts = []
    for local in generator.between(lo, hi, inc=True):
        utc = to_utc(local, tz)
        if utc < window_end and utc + duration > window_start:
            starts.append(utc)
            if len(starts) >= MAX_OCCURRENCES:
                break
    return starts


def expand_dates(rule: str, start: date, days: int, window_start: date, window_end: date) -> list[date]:
    """Start dates of the all-day occurrences that overlap [window_start, window_end)."""
    generator = _rule(rule, _midnight(start))
    lo = _midnight(window_start - timedelta(days=days))
    found = []
    for moment in generator.between(lo, _midnight(window_end), inc=True):
        day = moment.date()
        if day < window_end and day + timedelta(days=days) > window_start:
            found.append(day)
            if len(found) >= MAX_OCCURRENCES:
                break
    return found


def is_timed_occurrence(rule: str, start: datetime, tz: ZoneInfo, candidate: datetime) -> bool:
    local = to_local(candidate, tz)
    return bool(_rule(rule, to_local(start, tz)).between(local, local, inc=True))


def is_date_occurrence(rule: str, start: date, candidate: date) -> bool:
    moment = _midnight(candidate)
    return bool(_rule(rule, _midnight(start)).between(moment, moment, inc=True))


# --------------------------------------------------------------------------- splitting


def _count_before(generator: rrule, split: datetime) -> int:
    count = 0
    for occurrence in generator:
        if occurrence >= split:
            break
        count += 1
    return count


def _split(rule: str, generator: rrule, split: datetime, until: str) -> tuple[str, str]:
    parts = parse_parts(rule)
    head, tail = dict(parts), dict(parts)
    if "COUNT" in parts:
        before = _count_before(generator, split)
        head["COUNT"] = str(before)
        tail["COUNT"] = str(int(parts["COUNT"]) - before)
    else:
        head["UNTIL"] = until  # the tail keeps the original UNTIL, if there was one
    return format_parts(head), format_parts(tail)


def split_timed(rule: str, start: datetime, tz: ZoneInfo, split_at: datetime) -> tuple[str, str]:
    """(rule ending just before split_at, rule for the series starting at split_at)."""
    local_split = to_local(split_at, tz)
    until = (local_split - timedelta(seconds=1)).strftime(LOCAL_FORMAT)
    return _split(rule, _rule(rule, to_local(start, tz)), local_split, until)


def split_dates(rule: str, start: date, split_on: date) -> tuple[str, str]:
    until = (split_on - timedelta(days=1)).strftime("%Y%m%d")
    return _split(rule, _rule(rule, _midnight(start)), _midnight(split_on), until)


# --------------------------------------------------------------------------- chores


def next_date(rule: str, anchor: date, after: date) -> date | None:
    """First occurrence strictly after `after`, for a rule anchored at `anchor`."""
    found = _rule(rule, _midnight(anchor)).after(_midnight(after), inc=False)
    return found.date() if found else None


def next_datetime(rule: str, anchor: datetime, tz: ZoneInfo, after: datetime) -> datetime | None:
    found = _rule(rule, to_local(anchor, tz)).after(to_local(after, tz), inc=False)
    return to_utc(found, tz) if found else None
