"""Dashboard profiles: view configuration only, never a data-ownership boundary (spec §02)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.context import Ctx, check_revision, update_revisioned
from app.db.tables import dashboard_profiles, space_memberships
from app.domain.clock import new_id, now_iso
from app.domain.errors import Invalid, NotFound
from app.schemas.dashboards import DashboardCreate, DashboardPatch, WidgetPlacement


async def _accessible_spaces(conn: AsyncConnection, ctx: Ctx) -> set[str]:
    rows = await conn.execute(
        select(space_memberships.c.space_id).where(space_memberships.c.user_id == ctx.user_id)
    )
    return {r.space_id for r in rows}


async def _checked_space_ids(conn: AsyncConnection, ctx: Ctx, space_ids: list[Any]) -> str:
    wanted = [str(s) for s in dict.fromkeys(space_ids)]  # de-duplicate, keep order
    unknown = set(wanted) - await _accessible_spaces(conn, ctx)
    if unknown:
        raise Invalid("A dashboard can only show spaces you can access.", code="UNKNOWN_SPACE")
    return json.dumps(wanted)


def _layout_json(layout: list[WidgetPlacement]) -> str:
    return json.dumps([p.model_dump() for p in layout])


def _output(row: RowMapping, accessible: set[str]) -> dict[str, Any]:
    out = dict(row)
    out.pop("user_id")
    out["space_ids"] = [s for s in json.loads(out.pop("space_ids")) if s in accessible]
    out["layout"] = json.loads(out.pop("layout_json"))
    return out


async def _own(conn: AsyncConnection, ctx: Ctx, dashboard_id: str) -> RowMapping:
    row = (
        (
            await conn.execute(
                select(dashboard_profiles).where(
                    dashboard_profiles.c.id == dashboard_id, dashboard_profiles.c.user_id == ctx.user_id
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Dashboard")
    return row


async def list_dashboards(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.read() as conn:
        accessible = await _accessible_spaces(conn, ctx)
        rows = await conn.execute(
            select(dashboard_profiles)
            .where(dashboard_profiles.c.user_id == ctx.user_id)
            .order_by(dashboard_profiles.c.position, dashboard_profiles.c.created_at)
        )
        return [_output(r, accessible) for r in rows.mappings()]


async def get_dashboard(ctx: Ctx, dashboard_id: str) -> dict[str, Any]:
    async with ctx.read() as conn:
        return _output(await _own(conn, ctx, dashboard_id), await _accessible_spaces(conn, ctx))


async def create_dashboard(ctx: Ctx, data: DashboardCreate) -> dict[str, Any]:
    stamp = now_iso()
    async with ctx.write() as conn:
        last = await conn.scalar(
            select(func.max(dashboard_profiles.c.position)).where(dashboard_profiles.c.user_id == ctx.user_id)
        )
        row = (
            (
                await conn.execute(
                    insert(dashboard_profiles)
                    .values(
                        id=new_id(),
                        user_id=ctx.user_id,
                        name=data.name,
                        position=(last or 0.0) + 1.0,
                        space_ids=await _checked_space_ids(conn, ctx, data.space_ids),
                        layout_json=_layout_json(data.layout),
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    .returning(*dashboard_profiles.c)
                )
            )
            .mappings()
            .one()
        )
        return _output(row, await _accessible_spaces(conn, ctx))


async def update_dashboard(
    ctx: Ctx, dashboard_id: str, expected: int, data: DashboardPatch
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    async with ctx.write() as conn:
        await _own(conn, ctx, dashboard_id)
        if data.name is not None:
            values["name"] = data.name
        if data.position is not None:
            values["position"] = data.position
        if data.space_ids is not None:
            values["space_ids"] = await _checked_space_ids(conn, ctx, data.space_ids)
        if data.layout is not None:
            values["layout_json"] = _layout_json(data.layout)
        row = await update_revisioned(conn, dashboard_profiles, dashboard_id, expected, values)
        return _output(row, await _accessible_spaces(conn, ctx))


async def delete_dashboard(ctx: Ctx, dashboard_id: str, expected: int) -> None:
    """Removes the view only; the spaces and their data are untouched."""
    async with ctx.write() as conn:
        row = await _own(conn, ctx, dashboard_id)
        check_revision(dashboard_id, row["revision"], expected)
        await conn.execute(delete(dashboard_profiles).where(dashboard_profiles.c.id == dashboard_id))
