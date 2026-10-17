"""Tasks and repeating chores (ADR 0002): completing a repeating task spawns its successor."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select, tuple_
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.context import (
    Ctx,
    audit,
    load_in_space,
    record_event,
    require_space,
    update_revisioned,
)
from app.db.tables import people, tasks
from app.domain import recurrence
from app.domain.clock import iso, new_id, now, now_iso
from app.domain.errors import Conflict, Invalid
from app.domain.policy import Action
from app.schemas.tasks import Repeat, TaskCreate, TaskPatch

NO_DUE = "9999-12-31"
MAX_PAGE = 200


# --------------------------------------------------------------------------- shaping


def to_output(row: RowMapping) -> dict[str, Any]:
    out = dict(row)
    out["repeat"] = {
        "mode": out.pop("repeat_mode"),
        "rrule": out.pop("rrule"),
        "interval_days": out.pop("repeat_interval_days"),
    }
    out.pop("sort_key")
    out.pop("created_by")
    return out


def _sort_key(due_date: str | None, due_at: str | None, created_at: str) -> str:
    day = due_date or (due_at[:10] if due_at else NO_DUE)
    return f"{day}|{created_at}"


def _encode_cursor(row: RowMapping) -> str:
    return base64.urlsafe_b64encode(json.dumps([row["sort_key"], row["id"]]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        key, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(key), str(row_id)
    except (ValueError, TypeError) as exc:
        raise Invalid("Malformed cursor.", code="BAD_CURSOR") from exc


def _repeat_columns(ctx: Ctx, repeat: Repeat) -> dict[str, Any]:
    rule = None
    if repeat.rrule:
        try:
            rule = recurrence.normalize(repeat.rrule, ctx.timezone)
        except recurrence.InvalidRule as exc:
            raise Invalid(f"Invalid repeat rule: {exc}", code="INVALID_RRULE") from exc
        if "COUNT" in recurrence.parse_parts(rule):
            # each occurrence re-anchors the rule, so COUNT would never run out; UNTIL works
            raise Invalid("Chores cannot use COUNT; use UNTIL to stop repeating.", code="INVALID_RRULE")
    return {"repeat_mode": repeat.mode, "rrule": rule, "repeat_interval_days": repeat.interval_days}


async def _check_assignee(conn: AsyncConnection, person_id: Any, space_id: str) -> str | None:
    if person_id is None:
        return None
    found = await conn.scalar(
        select(people.c.id).where(people.c.id == str(person_id), people.c.space_id == space_id)
    )
    if found is None:
        raise Invalid("The assignee must be a person in the same space.", code="ASSIGNEE_NOT_IN_SPACE")
    return str(person_id)


# --------------------------------------------------------------------------- queries


async def list_tasks(
    ctx: Ctx,
    space_id: str,
    status: str,
    assignee_person_id: str | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Ordered by due day (undated last), then creation; keyset-paged."""
    query = select(tasks).where(tasks.c.space_id == space_id)
    if status != "all":
        query = query.where(tasks.c.status == status)
    if assignee_person_id:
        query = query.where(tasks.c.assignee_person_id == assignee_person_id)
    if cursor:
        query = query.where(tuple_(tasks.c.sort_key, tasks.c.id) > tuple_(*_decode_cursor(cursor)))
    limit = min(limit, MAX_PAGE)
    query = query.order_by(tasks.c.sort_key, tasks.c.id).limit(limit + 1)
    async with ctx.read() as conn:
        await require_space(conn, ctx, space_id, Action.READ)
        rows = list((await conn.execute(query)).mappings())
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return [to_output(r) for r in rows[:limit]], next_cursor


async def get_task(ctx: Ctx, task_id: str) -> dict[str, Any]:
    async with ctx.read() as conn:
        return to_output(await load_in_space(conn, ctx, tasks, task_id, Action.READ, "Task"))


# --------------------------------------------------------------------------- commands


async def create_task(ctx: Ctx, space_id: str, data: TaskCreate) -> dict[str, Any]:
    stamp = now_iso()
    task_id = new_id()
    due_date = data.due_date.isoformat() if data.due_date else None
    due_at = iso(data.due_at) if data.due_at else None
    if data.repeat.mode == "schedule" and not (due_date or due_at):
        raise Invalid("A scheduled chore needs a first due date.", code="DUE_REQUIRED")
    async with ctx.write() as conn:
        await require_space(conn, ctx, space_id, Action.WRITE)
        row = (
            (
                await conn.execute(
                    insert(tasks)
                    .values(
                        id=task_id,
                        space_id=space_id,
                        created_by=ctx.user_id,
                        assignee_person_id=await _check_assignee(conn, data.assignee_person_id, space_id),
                        title=data.title,
                        notes=data.notes,
                        due_date=due_date,
                        due_at=due_at,
                        chain_id=task_id,
                        status="open",
                        sort_key=_sort_key(due_date, due_at, stamp),
                        created_at=stamp,
                        updated_at=stamp,
                        **_repeat_columns(ctx, data.repeat),
                    )
                    .returning(*tasks.c)
                )
            )
            .mappings()
            .one()
        )
        await record_event(conn, ctx, "task.created", ("task", task_id, 1), space_id)
    return to_output(row)


async def update_task(ctx: Ctx, task_id: str, expected: int, data: TaskPatch) -> dict[str, Any]:
    async with ctx.write() as conn:
        task = await load_in_space(conn, ctx, tasks, task_id, Action.WRITE, "Task")
        if task["status"] == "archived":
            raise Conflict("Archived tasks cannot be edited.", code="TASK_ARCHIVED")
        values = await _patch_values(conn, ctx, task, data)
        row = await update_revisioned(conn, tasks, task_id, expected, values)

        event = "task.changed"
        if task["status"] == "open" and row["status"] == "done":
            event = "task.completed"
            await _spawn_successor(conn, ctx, row)
        elif task["status"] == "done" and row["status"] == "open":
            await _discard_untouched_successor(conn, task_id)
        await record_event(conn, ctx, event, ("task", task_id, row["revision"]), task["space_id"])
    return to_output(row)


async def _patch_values(conn: AsyncConnection, ctx: Ctx, task: RowMapping, data: TaskPatch) -> dict[str, Any]:
    sent = data.model_fields_set
    values: dict[str, Any] = {}
    if "title" in sent and data.title is not None:
        values["title"] = data.title
    if "notes" in sent and data.notes is not None:
        values["notes"] = data.notes
    if "due_date" in sent:
        values["due_date"] = data.due_date.isoformat() if data.due_date else None
    if "due_at" in sent:
        values["due_at"] = iso(data.due_at) if data.due_at else None
    if values.get("due_date") and "due_at" not in sent:
        values["due_at"] = None  # setting one kind of due clears the other
    if values.get("due_at") and "due_date" not in sent:
        values["due_date"] = None
    if "assignee_person_id" in sent:
        values["assignee_person_id"] = await _check_assignee(conn, data.assignee_person_id, task["space_id"])
    if "repeat" in sent and data.repeat is not None:
        values.update(_repeat_columns(ctx, data.repeat))
    if "status" in sent and data.status is not None and data.status != task["status"]:
        values["status"] = data.status
        values["completed_at"] = now_iso() if data.status == "done" else None

    merged = {**task, **values}
    if merged["due_date"] and merged["due_at"]:
        raise Invalid("Set due_date or due_at, not both.")
    if merged["repeat_mode"] == "schedule" and not (merged["due_date"] or merged["due_at"]):
        raise Invalid("A scheduled chore needs a due date.", code="DUE_REQUIRED")
    values["sort_key"] = _sort_key(merged["due_date"], merged["due_at"], task["created_at"])
    return values


def _next_due(ctx: Ctx, task: RowMapping, completed: datetime) -> tuple[str | None, str | None] | None:
    """(due_date, due_at) for the successor, or None when the schedule has run out."""
    tz = ctx.timezone
    completed_day = completed.astimezone(tz).date()
    if task["repeat_mode"] == "schedule":
        if task["due_date"]:
            due = date.fromisoformat(task["due_date"])
            found = recurrence.next_date(task["rrule"], due, max(due, completed_day))
            return (found.isoformat(), None) if found else None
        due_at = datetime.fromisoformat(task["due_at"])
        found_at = recurrence.next_datetime(task["rrule"], due_at, tz, max(due_at, completed))
        return (None, iso(found_at)) if found_at else None

    days = timedelta(days=task["repeat_interval_days"])
    if task["due_at"]:
        # same local time of day as before, N days after the day it was done
        local_time = datetime.fromisoformat(task["due_at"]).astimezone(tz).timetz().replace(tzinfo=None)
        local = datetime.combine(completed_day + days, local_time)
        return None, iso(recurrence.to_utc(local, tz))
    return (completed_day + days).isoformat(), None


async def _spawn_successor(conn: AsyncConnection, ctx: Ctx, task: RowMapping) -> None:
    if task["repeat_mode"] == "none":
        return
    due = _next_due(ctx, task, now())
    if due is None:
        return
    due_date, due_at = due
    stamp = now_iso()
    successor_id = new_id()
    await conn.execute(
        insert(tasks).values(
            id=successor_id,
            space_id=task["space_id"],
            created_by=task["created_by"],
            assignee_person_id=task["assignee_person_id"],
            title=task["title"],
            notes=task["notes"],
            due_date=due_date,
            due_at=due_at,
            repeat_mode=task["repeat_mode"],
            rrule=task["rrule"],
            repeat_interval_days=task["repeat_interval_days"],
            chain_id=task["chain_id"],
            previous_task_id=task["id"],
            status="open",
            sort_key=_sort_key(due_date, due_at, stamp),
            created_at=stamp,
            updated_at=stamp,
        )
    )
    await record_event(conn, ctx, "task.created", ("task", successor_id, 1), task["space_id"])


async def _discard_untouched_successor(conn: AsyncConnection, task_id: str) -> None:
    """Undo for an accidental completion: drop the spawned chore if nobody has touched it."""
    await conn.execute(
        delete(tasks).where(
            tasks.c.previous_task_id == task_id, tasks.c.status == "open", tasks.c.revision == 1
        )
    )


async def archive_task(ctx: Ctx, task_id: str, expected: int) -> None:
    async with ctx.write() as conn:
        task = await load_in_space(conn, ctx, tasks, task_id, Action.WRITE, "Task")
        row = await update_revisioned(conn, tasks, task_id, expected, {"status": "archived"})
        await audit(conn, ctx, "task.archived", "task", task_id, task["space_id"])
        await record_event(conn, ctx, "task.archived", ("task", task_id, row["revision"]), task["space_id"])
