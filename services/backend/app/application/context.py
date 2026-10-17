"""Who is calling, and the helpers every service uses inside its transaction."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Table, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import audit_log, outbox, space_memberships, spaces
from app.domain.clock import new_id, now_iso
from app.domain.errors import Conflict, Forbidden, NotFound, RevisionConflict
from app.domain.policy import Action, Role, allows


@dataclass(frozen=True)
class Ctx:
    engine: AsyncEngine
    user_id: str
    timezone: ZoneInfo
    request_id: str

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncConnection]:
        """One short write transaction (BEGIN IMMEDIATE). Never hold it across network calls."""
        async with self.engine.begin() as conn:
            yield conn

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.connect() as conn:
            yield conn


async def require_space(conn: AsyncConnection, ctx: Ctx, space_id: str, action: Action) -> RowMapping:
    """The space, if the caller may do `action` in it. Unknown and inaccessible look the same (404)."""
    row = (
        (
            await conn.execute(
                select(spaces, space_memberships.c.role)
                .join(space_memberships, space_memberships.c.space_id == spaces.c.id)
                .where(spaces.c.id == space_id, space_memberships.c.user_id == ctx.user_id)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Space")
    if not allows(Role(row["role"]), action):
        raise Forbidden("Your role in this space does not allow that.")
    if action is not Action.READ and row["archived_at"] is not None and action is not Action.MANAGE:
        raise Conflict("This space is archived.", code="SPACE_ARCHIVED")
    return row


async def load_in_space(
    conn: AsyncConnection, ctx: Ctx, table: Table, row_id: str, action: Action, what: str
) -> RowMapping:
    """Load a space-owned row and check access to *its* space, never one named by the client."""
    row = (await conn.execute(select(table).where(table.c.id == row_id))).mappings().first()
    if row is None:
        raise NotFound(what)
    try:
        await require_space(conn, ctx, row["space_id"], action)
    except NotFound:
        raise NotFound(what) from None
    return row


async def update_revisioned(
    conn: AsyncConnection, table: Table, row_id: str, expected: int, values: dict[str, Any]
) -> RowMapping:
    """Optimistic concurrency (spec §08): update only if the revision is still `expected`."""
    row = (
        (
            await conn.execute(
                update(table)
                .where(table.c.id == row_id, table.c.revision == expected)
                .values(**values, revision=table.c.revision + 1, updated_at=now_iso())
                .returning(*table.c)
            )
        )
        .mappings()
        .first()
    )
    if row is not None:
        return row
    current = await conn.scalar(select(table.c.revision).where(table.c.id == row_id))
    if current is None:
        raise NotFound()
    raise RevisionConflict(row_id, current)


def check_revision(row_id: str, actual: int, expected: int) -> None:
    """For mutations that delete rather than update (update_revisioned covers updates)."""
    if actual != expected:
        raise RevisionConflict(row_id, actual)


async def record_event(
    conn: AsyncConnection,
    ctx: Ctx,
    event_type: str,
    aggregate: tuple[str, str, int],
    space_id: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Outbox row in the caller's transaction (spec §09). Ids and metadata only, never content."""
    aggregate_type, aggregate_id, revision = aggregate
    envelope = {
        "event_id": new_id(),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": now_iso(),
        "producer": "agenda.api",
        "correlation_id": ctx.request_id,
        "causation_id": None,
        "actor_user_id": ctx.user_id,
        "space_id": space_id,
        "aggregate": {"type": aggregate_type, "id": aggregate_id, "revision": revision},
        "payload": payload or {},
    }
    await conn.execute(
        insert(outbox).values(
            id=envelope["event_id"],
            event_type=event_type,
            schema_version=1,
            envelope_json=json.dumps(envelope),
            created_at=envelope["occurred_at"],
        )
    )


async def audit(
    conn: AsyncConnection, ctx: Ctx, action: str, object_type: str, object_id: str, space_id: str | None
) -> None:
    """Audit consequential operations (spec §05). A stub per ADR 0001: deletes and archives."""
    await conn.execute(
        insert(audit_log).values(
            id=new_id(),
            actor_user_id=ctx.user_id,
            space_id=space_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            timestamp=now_iso(),
        )
    )
