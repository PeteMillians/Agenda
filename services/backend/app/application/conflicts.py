"""Sync conflicts: the owner chooses local, provider, or both (spec §10).

A resolution is committed in one transaction against the stored snapshots; the sync job queued
afterwards reconciles the provider and the mappings, and is idempotent like every other sync.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.context import Ctx
from app.db.tables import calendar_sources, event_mappings, events, external_accounts, sync_conflicts
from app.domain.clock import new_id, now_iso
from app.domain.errors import Conflict, Invalid, NotFound
from app.integrations.model import CONTENT_FIELDS, TIMING_FIELDS, canonical
from app.integrations.runtime import Integrations
from app.integrations.state import SourceState, load_state
from app.integrations.sync import cancelled_row
from app.schemas.integrations import Resolution

_QUERY = (
    select(
        sync_conflicts,
        events.c.title,
        events.c.series_id,
        events.c.rrule,
        calendar_sources.c.name.label("calendar_name"),
        calendar_sources.c.color.label("calendar_color"),
        external_accounts.c.provider,
    )
    .join(events, events.c.id == sync_conflicts.c.event_id)
    .join(calendar_sources, calendar_sources.c.id == sync_conflicts.c.calendar_source_id)
    .join(external_accounts, external_accounts.c.id == calendar_sources.c.external_account_id)
)


def _gone(snapshot: dict[str, Any], is_occurrence: bool) -> bool:
    return bool(snapshot.get("deleted")) or (is_occurrence and snapshot.get("status") == "cancelled")


def _out(row: RowMapping) -> dict[str, Any]:
    local = json.loads(row["local_snapshot"])
    remote = json.loads(row["remote_snapshot"])
    remote.pop("provider_id", None)
    occurrence = row["series_id"] is not None
    choices = ["keep_local", "keep_remote"]
    if not (_gone(local, occurrence) or _gone(remote, occurrence)):
        choices.append("keep_both")
    return {
        **{k: row[k] for k in ("id", "event_id", "calendar_source_id", "calendar_name", "calendar_color")},
        **{k: row[k] for k in ("provider", "title", "detected_at", "status", "resolution", "resolved_at")},
        "kind": "occurrence" if occurrence else "series" if row["rrule"] else "single",
        "local": local,
        "remote": remote,
        "choices": choices,
    }


async def list_conflicts(ctx: Ctx, *, include_closed: bool = False) -> list[dict[str, Any]]:
    query = _QUERY.where(external_accounts.c.user_id == ctx.user_id).order_by(sync_conflicts.c.detected_at)
    if not include_closed:
        query = query.where(sync_conflicts.c.status == "open")
    async with ctx.read() as conn:
        return [_out(r) for r in (await conn.execute(query)).mappings()]


async def resolve(
    ctx: Ctx, integrations: Integrations, conflict_id: str, resolution: Resolution
) -> dict[str, Any]:
    async with ctx.write() as conn:
        found = (
            (
                await conn.execute(
                    _QUERY.where(
                        sync_conflicts.c.id == conflict_id, external_accounts.c.user_id == ctx.user_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise NotFound("Conflict")
        if found["status"] != "open":
            raise Conflict("This conflict was already settled.", code="CONFLICT_CLOSED")
        state = await load_state(conn, found["calendar_source_id"])
        row = state.rows[found["event_id"]]
        remote = json.loads(found["remote_snapshot"])
        provider_id = remote.pop("provider_id", None)
        occurrence = row["series_id"] is not None
        local_gone = _gone(state.snapshot(row["id"]), occurrence)
        remote_gone = _gone(remote, occurrence)
        if resolution == "keep_both" and (local_gone or remote_gone):
            raise Invalid(
                "One side deleted this event, so there is only one version to keep.",
                code="KEEP_BOTH_UNAVAILABLE",
            )
        if resolution == "keep_local":
            if remote_gone and occurrence:
                # the provider cancelled this occurrence: ours lives on as an event of its own
                await _copy(conn, state, row)
                await _take_remote(conn, state, row, remote, provider_id, found["remote_version"])
            elif remote_gone:
                # deleted at the provider: forget the old ids, and the next push creates it again
                await conn.execute(
                    delete(event_mappings).where(event_mappings.c.event_id.in_(state.series(row["id"])))
                )
            else:
                await _aim_at(conn, state, row, remote, provider_id, found["remote_version"])
        elif resolution == "keep_remote":
            await _take_remote(conn, state, row, remote, provider_id, found["remote_version"])
        else:
            await _copy(conn, state, row)
            await _take_remote(conn, state, row, remote, provider_id, found["remote_version"])
        await conn.execute(
            update(sync_conflicts)
            .where(sync_conflicts.c.id == conflict_id)
            .values(status="resolved", resolution=resolution, resolved_at=now_iso())
        )
        result = (await conn.execute(_QUERY.where(sync_conflicts.c.id == conflict_id))).mappings().first()
    await integrations.enqueue_sync(found["calendar_source_id"], dedupe=f"resolve:{conflict_id}")
    if result is None:  # keeping the provider's deletion removed the event, and its conflict with it
        return {**_out(found), "status": "resolved", "resolution": resolution, "resolved_at": now_iso()}
    return _out(result)


async def _upsert_mapping(conn: AsyncConnection, row: RowMapping, values: dict[str, Any]) -> None:
    exists = await conn.scalar(
        select(event_mappings.c.event_id).where(event_mappings.c.event_id == row["id"])
    )
    if exists:
        await conn.execute(
            update(event_mappings).where(event_mappings.c.event_id == row["id"]).values(**values)
        )
    else:
        await conn.execute(
            insert(event_mappings).values(
                event_id=row["id"], calendar_source_id=row["calendar_source_id"], **values
            )
        )


async def _aim_at(
    conn: AsyncConnection,
    state: SourceState,
    row: RowMapping,
    remote: dict[str, Any],
    provider_id: str | None,
    version: str | None,
) -> None:
    """Keep ours: the provider's version becomes the one we last saw, so the next pull does not
    raise the same conflict again, and the next conditional write replaces exactly that version
    and nothing newer."""
    values: dict[str, Any] = {"etag": version, "synced_snapshot": canonical(remote)}
    if row["id"] not in state.mappings:
        values.update(provider_event_id=provider_id, last_synced_revision=row["revision"])
    await _upsert_mapping(conn, row, values)


async def _take_remote(
    conn: AsyncConnection,
    state: SourceState,
    row: RowMapping,
    remote: dict[str, Any],
    provider_id: str | None,
    version: str | None,
) -> None:
    """Keep theirs: the row becomes the provider's version and is in step with it again."""
    if remote.get("deleted") and not row["series_id"]:
        await conn.execute(delete(events).where(events.c.id == row["id"]))  # occurrences cascade
        return
    stamp = now_iso()
    if remote.get("deleted") or remote.get("status") == "cancelled":
        values: dict[str, Any] = {"status": "cancelled", "deleted_at": None}
    else:
        values = {f: remote[f] for f in (*CONTENT_FIELDS, *TIMING_FIELDS, "rrule")}
        values.update(status="confirmed", deleted_at=None)
    await conn.execute(
        update(events)
        .where(events.c.id == row["id"])
        .values(**values, revision=events.c.revision + 1, updated_at=stamp)
    )
    if remote.get("rrule"):
        children = state.children.get(row["id"], [])
        derived = [c for c in children if c not in state.mappings and state.rows[c]["status"] == "cancelled"]
        if derived:
            await conn.execute(delete(events).where(events.c.id.in_(derived)))
        mapped = [c for c in children if c in state.mappings]
        if mapped:
            await conn.execute(update(events).where(events.c.id.in_(mapped)).values(deleted_at=None))
        master = (await conn.execute(select(events).where(events.c.id == row["id"]))).mappings().one()
        overridden = {state.rows[c]["original_start"] for c in children if c not in derived}
        for key in remote.get("exdates", []):
            if key not in overridden:
                await conn.execute(
                    insert(events).values(
                        id=new_id(),
                        space_id=row["space_id"],
                        calendar_source_id=row["calendar_source_id"],
                        created_at=stamp,
                        updated_at=stamp,
                        **cancelled_row(master, key),
                    )
                )
    revision = await conn.scalar(select(events.c.revision).where(events.c.id == row["id"]))
    if remote.get("deleted"):  # an occurrence the provider no longer has: it travels with its master
        await conn.execute(delete(event_mappings).where(event_mappings.c.event_id == row["id"]))
        return
    mapping = state.mappings.get(row["id"])
    await _upsert_mapping(
        conn,
        row,
        {
            "provider_event_id": provider_id or (mapping["provider_event_id"] if mapping else None),
            "etag": version,
            "synced_snapshot": canonical(remote),
            "last_synced_revision": revision,
        },
    )


async def _copy(conn: AsyncConnection, state: SourceState, row: RowMapping) -> None:
    """Keep both: our version becomes a separate event in the same calendar, with ids and
    mappings of its own, so the next push creates it at the provider instead of matching it."""
    stamp = now_iso()
    columns = (*CONTENT_FIELDS, *TIMING_FIELDS, "space_id", "calendar_source_id")
    copy_id = new_id()
    await conn.execute(
        insert(events).values(
            id=copy_id, **{c: row[c] for c in columns}, rrule=row["rrule"], created_at=stamp, updated_at=stamp
        )
    )
    if row["rrule"] is None:
        return
    for child in state.children.get(row["id"], []):
        ex = state.rows[child]
        if ex["deleted_at"] is None:
            await conn.execute(
                insert(events).values(
                    id=new_id(),
                    **{c: ex[c] for c in columns},
                    series_id=copy_id,
                    original_start=ex["original_start"],
                    status=ex["status"],
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
