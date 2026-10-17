"""Events: single events, recurring series, exceptions, and scoped edits (ADR 0002).

Every scoped edit reduces to three primitives:
  _edit_one     create/update the exception for one occurrence           (scope=this)
  _edit_series  update the master, move the series by the same calendar
                shift as the edited occurrence, realign exceptions        (scope=all)
  _split        end the master before an occurrence, start a new master
                there, move later exceptions across                       (scope=following, then _edit_series)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.context import (
    Ctx,
    audit,
    check_revision,
    load_in_space,
    record_event,
    require_space,
    update_revisioned,
)
from app.db.tables import events
from app.domain import recurrence
from app.domain.clock import iso, new_id, now_iso
from app.domain.errors import Invalid, NotFound
from app.domain.policy import Action
from app.schemas.events import EventCreate, EventPatch, Scope

MAX_WINDOW_DAYS = 400
CONTENT = ("title", "description", "location")


# --------------------------------------------------------------------------- timing


@dataclass(frozen=True)
class Timing:
    all_day: bool
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> Timing:
        if row["start_date"]:
            return cls(
                True,
                start_date=date.fromisoformat(row["start_date"]),
                end_date=date.fromisoformat(row["end_date"]),
            )
        return cls(
            False,
            start_at=datetime.fromisoformat(row["start_at"]),
            end_at=datetime.fromisoformat(row["end_at"]),
        )

    @property
    def start(self) -> date | datetime:
        return self.start_date if self.all_day else self.start_at  # type: ignore[return-value]

    @property
    def length(self) -> timedelta:
        if self.all_day:
            return self.end_date - self.start_date  # type: ignore[operator]
        return self.end_at - self.start_at  # type: ignore[operator]

    @property
    def key(self) -> str:
        """The occurrence key: UTC ISO for timed, YYYY-MM-DD for all-day."""
        return self.start_date.isoformat() if self.all_day else iso(self.start_at)  # type: ignore[union-attr,arg-type]

    def starting(self, start: date | datetime) -> Timing:
        if self.all_day:
            return Timing(True, start_date=start, end_date=start + self.length)  # type: ignore[arg-type,operator]
        return Timing(False, start_at=start, end_at=start + self.length)  # type: ignore[arg-type,operator]

    def local_date(self, tz: ZoneInfo) -> date:
        return self.start_date if self.all_day else self.start_at.astimezone(tz).date()  # type: ignore[return-value,union-attr]

    def sort_moment(self, tz: ZoneInfo) -> datetime:
        return self.start_at if not self.all_day else datetime.combine(self.start_date, time(), tz)  # type: ignore[return-value,arg-type]

    def columns(self) -> dict[str, str | None]:
        return {
            "start_at": None if self.all_day else iso(self.start_at),  # type: ignore[arg-type]
            "end_at": None if self.all_day else iso(self.end_at),  # type: ignore[arg-type]
            "start_date": self.start_date.isoformat() if self.all_day else None,  # type: ignore[union-attr]
            "end_date": self.end_date.isoformat() if self.all_day else None,  # type: ignore[union-attr]
        }

    def overlaps(self, lo: datetime, hi: datetime, lo_day: date, hi_day: date) -> bool:
        if self.all_day:
            return self.start_date < hi_day and self.end_date > lo_day  # type: ignore[operator]
        return self.start_at < hi and self.end_at > lo  # type: ignore[operator]


def _check_order(timing: Timing) -> Timing:
    if timing.length <= timedelta(0):
        raise Invalid("An event must end after it starts.", code="INVALID_TIMING")
    return timing


def _tz(row: Mapping[str, Any]) -> ZoneInfo:
    return ZoneInfo(row["timezone"])


def _rule(text: str, tz: ZoneInfo) -> str:
    try:
        return recurrence.normalize(text, tz)
    except recurrence.InvalidRule as exc:
        raise Invalid(f"Invalid recurrence rule: {exc}", code="INVALID_RRULE") from exc


def _is_occurrence(rule: str, first: Timing, tz: ZoneInfo, start: date | datetime) -> bool:
    if first.all_day:
        return recurrence.is_date_occurrence(rule, first.start_date, start)  # type: ignore[arg-type]
    return recurrence.is_timed_occurrence(rule, first.start_at, tz, start)  # type: ignore[arg-type]


def _require_on_pattern(rule: str | None, first: Timing, tz: ZoneInfo) -> None:
    """RFC 5545 treats DTSTART as the first occurrence; dateutil silently skips it if it is off-pattern."""
    if rule and not _is_occurrence(rule, first, tz, first.start):
        raise Invalid(
            "The start must fall on the recurrence pattern (e.g. a Tuesday for BYDAY=TU).",
            code="INVALID_RRULE",
        )


def _parse_key(master: Mapping[str, Any], key: str) -> Timing:
    """Timing of the occurrence `key` of `master`, or 404 if the series has no such occurrence."""
    first, tz = Timing.of(master), _tz(master)
    try:
        start: date | datetime = date.fromisoformat(key) if first.all_day else datetime.fromisoformat(key)
    except ValueError:
        raise NotFound("Occurrence") from None
    if isinstance(start, datetime) and start.tzinfo is None:
        raise NotFound("Occurrence")
    if not _is_occurrence(master["rrule"], first, tz, start):
        raise NotFound("Occurrence")
    return first.starting(start)


def _merge(base: Mapping[str, Any], timing: Timing, patch: EventPatch) -> tuple[dict[str, Any], Timing]:
    """Apply a patch to content + timing. Moving only the start keeps the length."""
    sent = patch.model_fields_set
    values = {f: getattr(patch, f) for f in CONTENT if f in sent and getattr(patch, f) is not None}
    values["timezone"] = patch.timezone if "timezone" in sent and patch.timezone else base["timezone"]
    all_day = patch.all_day if "all_day" in sent and patch.all_day is not None else timing.all_day
    same_kind = all_day == timing.all_day
    if all_day:
        start = patch.start_date if "start_date" in sent else (timing.start_date if same_kind else None)
        end = patch.end_date if "end_date" in sent else (timing.end_date if same_kind else None)
        if same_kind and "start_date" in sent and "end_date" not in sent and start:
            end = start + timing.length
        if not (start and end):
            raise Invalid("All-day events need start_date and end_date.", code="INVALID_TIMING")
        return values, _check_order(Timing(True, start_date=start, end_date=end))
    start_at = patch.start_at if "start_at" in sent else (timing.start_at if same_kind else None)
    end_at = patch.end_at if "end_at" in sent else (timing.end_at if same_kind else None)
    if same_kind and "start_at" in sent and "end_at" not in sent and start_at:
        end_at = start_at + timing.length
    if not (start_at and end_at):
        raise Invalid("Timed events need start_at and end_at.", code="INVALID_TIMING")
    return values, _check_order(
        Timing(False, start_at=start_at.astimezone(UTC), end_at=end_at.astimezone(UTC))
    )


def _shift(first: Timing, edited: Timing, result: Timing, old_tz: ZoneInfo, new_tz: ZoneInfo) -> Timing:
    """Move `first` by the calendar shift that turned `edited` into `result`, keeping
    `result`'s local time of day and length (DST-safe: shifts are in local days)."""
    day = first.local_date(old_tz) + (result.local_date(new_tz) - edited.local_date(old_tz))
    if result.all_day:
        return Timing(True, start_date=day, end_date=day + result.length)
    local_time = result.start_at.astimezone(new_tz).time()  # type: ignore[union-attr]
    start = recurrence.to_utc(datetime.combine(day, local_time), new_tz)
    return Timing(False, start_at=start, end_at=start + result.length)


# --------------------------------------------------------------------------- output


def to_output(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("deleted_at", None)
    out["all_day"] = row["start_date"] is not None
    return out


def _occurrence(
    master: Mapping[str, Any],
    content: Mapping[str, Any],
    timing: Timing,
    *,
    key: str | None,
    is_exception: bool,
) -> dict[str, Any]:
    return {
        "event_id": master["id"],
        "occurrence_start": key,
        "is_recurring": master["rrule"] is not None,
        "is_exception": is_exception,
        "revision": master["revision"],
        **{f: content[f] for f in CONTENT},
        "all_day": timing.all_day,
        **timing.columns(),
        "timezone": content["timezone"],
        "rrule": master["rrule"],
    }


# --------------------------------------------------------------------------- queries


async def list_occurrences(ctx: Ctx, space_id: str, start: date, end: date) -> list[dict[str, Any]]:
    """Everything on the calendar between `start` and `end` (exclusive), in the owner's time zone."""
    if end <= start or (end - start).days > MAX_WINDOW_DAYS:
        raise Invalid(f"The window must be 1 to {MAX_WINDOW_DAYS} days.", code="INVALID_WINDOW")
    lo = datetime.combine(start, time(), ctx.timezone).astimezone(UTC)
    hi = datetime.combine(end, time(), ctx.timezone).astimezone(UTC)
    live = and_(events.c.space_id == space_id, events.c.deleted_at.is_(None))
    async with ctx.read() as conn:
        await require_space(conn, ctx, space_id, Action.READ)
        singles = await conn.execute(
            select(events).where(
                live,
                events.c.series_id.is_(None),
                events.c.rrule.is_(None),
                or_(
                    and_(events.c.start_at < iso(hi), events.c.end_at > iso(lo)),
                    and_(events.c.start_date < end.isoformat(), events.c.end_date > start.isoformat()),
                ),
            )
        )
        masters = list(
            (await conn.execute(select(events).where(live, events.c.rrule.is_not(None)))).mappings()
        )
        exceptions = (
            await conn.execute(
                select(events).where(
                    events.c.deleted_at.is_(None), events.c.series_id.in_([m["id"] for m in masters])
                )
            )
        ).mappings()

    found = [
        (Timing.of(r), _occurrence(r, r, Timing.of(r), key=None, is_exception=False))
        for r in singles.mappings()
    ]
    by_series: dict[str, dict[str, RowMapping]] = {}
    for ex in exceptions:
        by_series.setdefault(ex["series_id"], {})[ex["original_start"]] = ex
    for master in masters:
        first, tz, overrides = Timing.of(master), _tz(master), by_series.get(master["id"], {})
        if first.all_day:
            starts: list[Any] = recurrence.expand_dates(
                master["rrule"], first.start_date, first.length.days, start, end
            )  # type: ignore[arg-type]
        else:
            starts = recurrence.expand_timed(master["rrule"], first.start_at, tz, first.length, lo, hi)  # type: ignore[arg-type]
        for occurrence_start in starts:
            timing = first.starting(occurrence_start)
            if timing.key not in overrides:
                found.append(
                    (timing, _occurrence(master, master, timing, key=timing.key, is_exception=False))
                )
        for key, ex in overrides.items():  # moved exceptions can land in the window from outside it
            timing = Timing.of(ex)
            if ex["status"] == "confirmed" and timing.overlaps(lo, hi, start, end):
                found.append((timing, _occurrence(master, ex, timing, key=key, is_exception=True)))
    found.sort(key=lambda pair: pair[0].sort_moment(ctx.timezone))
    return [occurrence for _, occurrence in found]


async def get_event(ctx: Ctx, event_id: str) -> dict[str, Any]:
    async with ctx.read() as conn:
        row = await load_in_space(conn, ctx, events, event_id, Action.READ, "Event")
    if row["deleted_at"]:
        raise NotFound("Event")
    return to_output(row)


# --------------------------------------------------------------------------- create


async def create_event(ctx: Ctx, space_id: str, data: EventCreate) -> dict[str, Any]:
    tz_name = data.timezone or ctx.timezone.key
    tz = ZoneInfo(tz_name)
    if data.all_day:
        timing = Timing(True, start_date=data.start_date, end_date=data.end_date)
    else:
        timing = Timing(False, start_at=data.start_at.astimezone(UTC), end_at=data.end_at.astimezone(UTC))  # type: ignore[union-attr]
    _check_order(timing)
    rule = _rule(data.rrule, tz) if data.rrule else None
    _require_on_pattern(rule, timing, tz)
    stamp = now_iso()
    event_id = new_id()
    async with ctx.write() as conn:
        await require_space(conn, ctx, space_id, Action.WRITE)
        row = (
            (
                await conn.execute(
                    insert(events)
                    .values(
                        id=event_id,
                        space_id=space_id,
                        title=data.title,
                        description=data.description,
                        location=data.location,
                        timezone=tz_name,
                        rrule=rule,
                        created_at=stamp,
                        updated_at=stamp,
                        **timing.columns(),
                    )
                    .returning(*events.c)
                )
            )
            .mappings()
            .one()
        )
        await record_event(conn, ctx, "event.created", ("event", event_id, 1), space_id)
    return to_output(row)


# --------------------------------------------------------------------------- update


async def _target(conn: AsyncConnection, ctx: Ctx, event_id: str) -> tuple[RowMapping, str | None]:
    """(single event or series master, implied occurrence key when an exception row was named)."""
    row = await load_in_space(conn, ctx, events, event_id, Action.WRITE, "Event")
    if row["deleted_at"]:
        raise NotFound("Event")
    if row["series_id"] is None:
        return row, None
    master = (await conn.execute(select(events).where(events.c.id == row["series_id"]))).mappings().one()
    return master, row["original_start"]


def _resolve(
    target: RowMapping, implied: str | None, scope: Scope | None, key: str | None
) -> tuple[Scope, str | None]:
    if implied is not None:
        if scope not in (None, "this"):
            raise Invalid("An exception can only be changed with scope=this.", code="INVALID_SCOPE")
        return "this", implied
    if target["rrule"] is None:
        if scope not in (None, "all"):
            raise Invalid("This event does not repeat; omit scope.", code="NOT_RECURRING")
        return "all", None
    if scope is None:
        raise Invalid("Recurring events need scope=this, following, or all.", code="SCOPE_REQUIRED")
    if key is None and scope != "all":
        raise Invalid("Send occurrence_start for scope=this or following.", code="OCCURRENCE_REQUIRED")
    if key is not None:
        key = _parse_key(target, key).key  # validates and canonicalizes
    if scope == "following" and key == Timing.of(target).key:
        return "all", key  # "this and following" from the first occurrence is the whole series
    return scope, key


async def update_event(
    ctx: Ctx,
    event_id: str,
    expected: int,
    patch: EventPatch,
    scope: Scope | None,
    occurrence_start: str | None,
) -> dict[str, Any]:
    async with ctx.write() as conn:
        target, implied = await _target(conn, ctx, event_id)
        check_revision(target["id"], target["revision"], expected)
        scope, key = _resolve(target, implied, scope, occurrence_start)
        if scope == "this":
            row = await _edit_one(conn, target, key, patch)  # type: ignore[arg-type]
        elif scope == "following":
            tail = await _split(conn, target, key, expected)  # type: ignore[arg-type]
            row = await _edit_series(conn, tail, patch, key, 1)
        else:
            row = await _edit_series(conn, target, patch, key, expected)
        await record_event(
            conn, ctx, "event.changed", ("event", target["id"], target["revision"] + 1), target["space_id"]
        )
    return to_output(row)


async def _edit_one(conn: AsyncConnection, master: RowMapping, key: str, patch: EventPatch) -> RowMapping:
    if "rrule" in patch.model_fields_set:
        raise Invalid("Change the rule with scope=all or following.", code="INVALID_SCOPE")
    occurrence = _parse_key(master, key)
    existing = await _exception(conn, master["id"], key)
    base = existing or master
    values, timing = _merge(base, Timing.of(existing) if existing else occurrence, patch)
    values.update(timing.columns(), status="confirmed")
    stamp = now_iso()
    if existing:
        stmt = (
            update(events)
            .where(events.c.id == existing["id"])
            .values(**values, revision=events.c.revision + 1, updated_at=stamp)
        )
        row_id = existing["id"]
    else:
        row_id = new_id()
        stmt = insert(events).values(
            id=row_id,
            space_id=master["space_id"],
            **{**{f: master[f] for f in CONTENT}, **values},  # patched content wins
            series_id=master["id"],
            original_start=key,
            created_at=stamp,
            updated_at=stamp,
        )
    await conn.execute(stmt)
    await update_revisioned(conn, events, master["id"], master["revision"], {})  # the series is the aggregate
    return (await conn.execute(select(events).where(events.c.id == row_id))).mappings().one()


async def _edit_series(
    conn: AsyncConnection, master: RowMapping, patch: EventPatch, key: str | None, expected: int
) -> RowMapping:
    first, old_tz = Timing.of(master), _tz(master)
    edited = _parse_key(master, key) if key and master["rrule"] else first
    values, result = _merge(master, edited, patch)
    new_tz = ZoneInfo(values["timezone"])
    new_first = _shift(first, edited, result, old_tz, new_tz)
    rule = master["rrule"]
    if "rrule" in patch.model_fields_set:
        rule = _rule(patch.rrule, new_tz) if patch.rrule else None
    _require_on_pattern(rule, new_first, new_tz)
    values.update(new_first.columns(), rrule=rule)
    row = await update_revisioned(conn, events, master["id"], expected, values)
    if master["rrule"]:
        await _realign(conn, master, row, first, new_first, old_tz, new_tz)
    return row


async def _realign(
    conn: AsyncConnection,
    master: RowMapping,
    updated: RowMapping,
    old_first: Timing,
    new_first: Timing,
    old_tz: ZoneInfo,
    new_tz: ZoneInfo,
) -> None:
    """After a series edit: re-key exceptions by the same shift, drop those that no longer
    land on the rule, and let unmoved exceptions follow the series' new time."""
    found = list(
        (
            await conn.execute(
                select(events).where(events.c.series_id == master["id"], events.c.deleted_at.is_(None))
            )
        ).mappings()
    )
    if not found:
        return
    if updated["rrule"] is None or old_first.all_day != new_first.all_day:
        await _tombstone(conn, [ex["id"] for ex in found])
        return
    # two passes so re-keying never trips the (series_id, original_start) unique index
    for ex in found:
        await conn.execute(
            update(events).where(events.c.id == ex["id"]).values(original_start=f"moving:{ex['id']}")
        )
    doomed = []
    for ex in found:
        old_occurrence = old_first.starting(
            date.fromisoformat(ex["original_start"])
            if old_first.all_day
            else datetime.fromisoformat(ex["original_start"])
        )
        new_occurrence = _shift(old_occurrence, old_first, new_first, old_tz, new_tz)
        if not _is_occurrence(updated["rrule"], new_first, new_tz, new_occurrence.start):
            doomed.append(ex["id"])
            continue
        values: dict[str, Any] = {"original_start": new_occurrence.key, "timezone": updated["timezone"]}
        if Timing.of(ex) == old_occurrence:  # never moved on its own: follow the series
            values.update(new_occurrence.columns())
        await conn.execute(
            update(events)
            .where(events.c.id == ex["id"])
            .values(**values, revision=events.c.revision + 1, updated_at=now_iso())
        )
    await _tombstone(conn, doomed)


async def _split(conn: AsyncConnection, master: RowMapping, key: str, expected: int) -> RowMapping:
    """End `master` just before occurrence `key`; return the new master that starts there."""
    occurrence = _parse_key(master, key)
    head, tail = _split_rule(master, occurrence)
    await update_revisioned(conn, events, master["id"], expected, {"rrule": head})
    stamp = now_iso()
    tail_id = new_id()
    await conn.execute(
        insert(events).values(
            id=tail_id,
            space_id=master["space_id"],
            **{f: master[f] for f in CONTENT},
            timezone=master["timezone"],
            rrule=tail,
            created_at=stamp,
            updated_at=stamp,
            **occurrence.columns(),
        )
    )
    await conn.execute(
        update(events)
        .where(events.c.series_id == master["id"], events.c.original_start >= key)
        .values(series_id=tail_id)
    )
    return (await conn.execute(select(events).where(events.c.id == tail_id))).mappings().one()


def _split_rule(master: Mapping[str, Any], occurrence: Timing) -> tuple[str, str]:
    first = Timing.of(master)
    if first.all_day:
        return recurrence.split_dates(master["rrule"], first.start_date, occurrence.start_date)  # type: ignore[arg-type]
    return recurrence.split_timed(master["rrule"], first.start_at, _tz(master), occurrence.start_at)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- delete


async def delete_event(
    ctx: Ctx, event_id: str, expected: int, scope: Scope | None, occurrence_start: str | None
) -> None:
    async with ctx.write() as conn:
        target, implied = await _target(conn, ctx, event_id)
        check_revision(target["id"], target["revision"], expected)
        scope, key = _resolve(target, implied, scope, occurrence_start)
        if scope == "this":
            await _cancel_one(conn, target, key)  # type: ignore[arg-type]
        elif scope == "following":
            head, _ = _split_rule(target, _parse_key(target, key))  # type: ignore[arg-type]
            await update_revisioned(conn, events, target["id"], expected, {"rrule": head})
            later = await conn.execute(
                select(events.c.id).where(events.c.series_id == target["id"], events.c.original_start >= key)
            )
            await _tombstone(conn, [r.id for r in later])
        else:
            series = await conn.execute(select(events.c.id).where(events.c.series_id == target["id"]))
            await _tombstone(conn, [target["id"], *(r.id for r in series)])
            await audit(conn, ctx, "event.deleted", "event", target["id"], target["space_id"])
        await record_event(
            conn,
            ctx,
            "event.deleted" if scope == "all" else "event.changed",
            ("event", target["id"], expected + 1),
            target["space_id"],
        )


async def _cancel_one(conn: AsyncConnection, master: RowMapping, key: str) -> None:
    occurrence = _parse_key(master, key)
    existing = await _exception(conn, master["id"], key)
    stamp = now_iso()
    if existing:
        await conn.execute(
            update(events)
            .where(events.c.id == existing["id"])
            .values(status="cancelled", revision=events.c.revision + 1, updated_at=stamp)
        )
    else:
        await conn.execute(
            insert(events).values(
                id=new_id(),
                space_id=master["space_id"],
                **{f: master[f] for f in CONTENT},
                timezone=master["timezone"],
                series_id=master["id"],
                original_start=key,
                status="cancelled",
                created_at=stamp,
                updated_at=stamp,
                **occurrence.columns(),
            )
        )
    await update_revisioned(conn, events, master["id"], master["revision"], {})


async def _exception(conn: AsyncConnection, series_id: str, key: str) -> RowMapping | None:
    return (
        (
            await conn.execute(
                select(events).where(
                    events.c.series_id == series_id,
                    events.c.original_start == key,
                    events.c.deleted_at.is_(None),
                )
            )
        )
        .mappings()
        .first()
    )


async def _tombstone(conn: AsyncConnection, ids: list[str]) -> None:
    if ids:
        await conn.execute(
            update(events)
            .where(events.c.id.in_(ids), events.c.deleted_at.is_(None))
            .values(deleted_at=now_iso(), revision=events.c.revision + 1, updated_at=now_iso())
        )
