"""Lists and their items. Each item carries its own revision, so ticking one item never
conflicts with editing another."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, insert, select
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
from app.db.tables import list_items, lists
from app.domain.clock import new_id, now_iso
from app.domain.errors import NotFound
from app.domain.policy import Action
from app.schemas.lists import ItemCreate, ItemPatch, ListCreate, ListPatch

_COUNTS = (
    select(
        list_items.c.list_id,
        func.count().label("item_count"),
        func.coalesce(func.sum(list_items.c.checked), 0).label("checked_count"),
    )
    .group_by(list_items.c.list_id)
    .subquery()
)


async def _summary(conn: AsyncConnection, list_id: str) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                select(
                    lists,
                    func.coalesce(_COUNTS.c.item_count, 0).label("item_count"),
                    func.coalesce(_COUNTS.c.checked_count, 0).label("checked_count"),
                )
                .outerjoin(_COUNTS, _COUNTS.c.list_id == lists.c.id)
                .where(lists.c.id == list_id)
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def list_lists(ctx: Ctx, space_id: str) -> list[dict[str, Any]]:
    query = (
        select(
            lists,
            func.coalesce(_COUNTS.c.item_count, 0).label("item_count"),
            func.coalesce(_COUNTS.c.checked_count, 0).label("checked_count"),
        )
        .outerjoin(_COUNTS, _COUNTS.c.list_id == lists.c.id)
        .where(lists.c.space_id == space_id, lists.c.archived_at.is_(None))
        .order_by(lists.c.name)
    )
    async with ctx.read() as conn:
        await require_space(conn, ctx, space_id, Action.READ)
        return [dict(r) for r in (await conn.execute(query)).mappings()]


async def get_list(ctx: Ctx, list_id: str) -> dict[str, Any]:
    async with ctx.read() as conn:
        await load_in_space(conn, ctx, lists, list_id, Action.READ, "List")
        detail = await _summary(conn, list_id)
        items = await conn.execute(
            select(list_items).where(list_items.c.list_id == list_id).order_by(list_items.c.position)
        )
        detail["items"] = [dict(i) for i in items.mappings()]
    return detail


async def create_list(ctx: Ctx, space_id: str, data: ListCreate) -> dict[str, Any]:
    stamp = now_iso()
    list_id = new_id()
    async with ctx.write() as conn:
        await require_space(conn, ctx, space_id, Action.WRITE)
        await conn.execute(
            insert(lists).values(
                id=list_id, space_id=space_id, name=data.name, created_at=stamp, updated_at=stamp
            )
        )
        await record_event(conn, ctx, "list.created", ("list", list_id, 1), space_id)
        return await _summary(conn, list_id)


async def update_list(ctx: Ctx, list_id: str, expected: int, data: ListPatch) -> dict[str, Any]:
    async with ctx.write() as conn:
        found = await load_in_space(conn, ctx, lists, list_id, Action.WRITE, "List")
        row = await update_revisioned(conn, lists, list_id, expected, data.model_dump(exclude_none=True))
        await record_event(conn, ctx, "list.changed", ("list", list_id, row["revision"]), found["space_id"])
        return await _summary(conn, list_id)


async def archive_list(ctx: Ctx, list_id: str, expected: int) -> None:
    async with ctx.write() as conn:
        found = await load_in_space(conn, ctx, lists, list_id, Action.WRITE, "List")
        await update_revisioned(conn, lists, list_id, expected, {"archived_at": now_iso()})
        await audit(conn, ctx, "list.archived", "list", list_id, found["space_id"])


async def _item_with_list(conn: AsyncConnection, ctx: Ctx, item_id: str) -> tuple[RowMapping, RowMapping]:
    item = (await conn.execute(select(list_items).where(list_items.c.id == item_id))).mappings().first()
    if item is None:
        raise NotFound("List item")
    try:
        parent = await load_in_space(conn, ctx, lists, item["list_id"], Action.WRITE, "List")
    except NotFound:
        raise NotFound("List item") from None
    return item, parent


async def add_item(ctx: Ctx, list_id: str, data: ItemCreate) -> RowMapping:
    stamp = now_iso()
    async with ctx.write() as conn:
        found = await load_in_space(conn, ctx, lists, list_id, Action.WRITE, "List")
        position = data.position
        if position is None:
            last = await conn.scalar(
                select(func.max(list_items.c.position)).where(list_items.c.list_id == list_id)
            )
            position = (last or 0.0) + 1.0
        row = (
            (
                await conn.execute(
                    insert(list_items)
                    .values(
                        id=new_id(),
                        list_id=list_id,
                        text=data.text,
                        position=position,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    .returning(*list_items.c)
                )
            )
            .mappings()
            .one()
        )
        await record_event(
            conn, ctx, "list.item_added", ("list", list_id, found["revision"]), found["space_id"]
        )
    return row


async def update_item(ctx: Ctx, item_id: str, expected: int, data: ItemPatch) -> RowMapping:
    async with ctx.write() as conn:
        _, parent = await _item_with_list(conn, ctx, item_id)
        row = await update_revisioned(conn, list_items, item_id, expected, data.model_dump(exclude_none=True))
        await record_event(
            conn, ctx, "list.item_changed", ("list", parent["id"], parent["revision"]), parent["space_id"]
        )
    return row


async def delete_item(ctx: Ctx, item_id: str, expected: int) -> None:
    async with ctx.write() as conn:
        item, parent = await _item_with_list(conn, ctx, item_id)
        check_revision(item_id, item["revision"], expected)
        await conn.execute(delete(list_items).where(list_items.c.id == item_id))
        await record_event(
            conn, ctx, "list.item_removed", ("list", parent["id"], parent["revision"]), parent["space_id"]
        )


async def clear_checked(ctx: Ctx, list_id: str) -> int:
    """Remove every checked item, e.g. after a shopping trip. Returns how many were removed."""
    async with ctx.write() as conn:
        found = await load_in_space(conn, ctx, lists, list_id, Action.WRITE, "List")
        result = await conn.execute(
            delete(list_items).where(list_items.c.list_id == list_id, list_items.c.checked.is_(True))
        )
        await record_event(conn, ctx, "list.cleared", ("list", list_id, found["revision"]), found["space_id"])
    return result.rowcount
