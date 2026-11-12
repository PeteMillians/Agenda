"""Recording sync conflicts (spec §10). When Agenda and the provider both changed an event, both
versions are kept and the user chooses; neither side is overwritten in the meantime."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import sync_conflicts
from app.domain.clock import new_id, now_iso

log = logging.getLogger(__name__)

SNAPSHOT_FIELDS = (
    "title",
    "description",
    "location",
    "start_at",
    "end_at",
    "start_date",
    "end_date",
    "timezone",
    "rrule",
    "status",
)


def local_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**{f: row[f] for f in SNAPSHOT_FIELDS}, "deleted": row["deleted_at"] is not None}


async def open_conflict_ids(conn: AsyncConnection, source_id: str) -> set[str]:
    rows = await conn.execute(
        select(sync_conflicts.c.event_id).where(
            sync_conflicts.c.calendar_source_id == source_id, sync_conflicts.c.status == "open"
        )
    )
    return {r.event_id for r in rows}


async def record(
    conn: AsyncConnection,
    row: Mapping[str, Any],
    source_id: str,
    remote: dict[str, Any] | None,
    remote_version: str | None,
) -> str:
    """Open a conflict for `row`, or refresh the provider side of the one already open."""
    existing = await conn.scalar(
        select(sync_conflicts.c.id).where(
            sync_conflicts.c.event_id == row["id"], sync_conflicts.c.status == "open"
        )
    )
    values = {
        "local_snapshot": json.dumps(local_snapshot(row)),
        "remote_snapshot": json.dumps(remote) if remote is not None else None,
        "local_revision": row["revision"],
        "remote_version": remote_version,
    }
    if existing:
        await conn.execute(update(sync_conflicts).where(sync_conflicts.c.id == existing).values(**values))
        return existing
    conflict_id = new_id()
    await conn.execute(
        insert(sync_conflicts).values(
            id=conflict_id, event_id=row["id"], calendar_source_id=source_id, detected_at=now_iso(), **values
        )
    )
    log.info("sync conflict %s opened", conflict_id)  # ids only: never event content
    return conflict_id
