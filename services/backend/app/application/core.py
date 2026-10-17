"""Setup, profile, spaces, and people."""

from __future__ import annotations

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.application.context import (
    Ctx,
    audit,
    check_revision,
    load_in_space,
    record_event,
    require_space,
    update_revisioned,
)
from app.db.tables import people, space_memberships, spaces, tasks, users
from app.domain.clock import new_id, now_iso
from app.domain.errors import Conflict
from app.domain.policy import Action
from app.schemas.core import PersonCreate, PersonPatch, SetupRequest, SpaceCreate, SpacePatch, UserPatch

# --------------------------------------------------------------------------- setup and profile


async def setup_completed(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return await conn.scalar(select(users.c.id).limit(1)) is not None


async def run_setup(engine: AsyncEngine, data: SetupRequest) -> RowMapping:
    """Create the owner and default spaces. Allowed exactly once."""
    async with engine.begin() as conn:
        if await conn.scalar(select(users.c.id).limit(1)) is not None:
            raise Conflict("Setup has already been completed.", code="SETUP_ALREADY_DONE")
        stamp = now_iso()
        user_id = new_id()
        await conn.execute(
            insert(users).values(
                id=user_id,
                display_name=data.display_name,
                timezone=data.timezone,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        for space in data.spaces:
            await _insert_space(conn, user_id, space)
        return (await conn.execute(select(users).where(users.c.id == user_id))).mappings().one()


async def get_me(ctx: Ctx) -> RowMapping:
    async with ctx.read() as conn:
        return (await conn.execute(select(users).where(users.c.id == ctx.user_id))).mappings().one()


async def update_me(ctx: Ctx, data: UserPatch) -> RowMapping:
    values = data.model_dump(exclude_unset=True, exclude_none=True)
    async with ctx.write() as conn:
        if values:
            await conn.execute(
                update(users).where(users.c.id == ctx.user_id).values(**values, updated_at=now_iso())
            )
        return (await conn.execute(select(users).where(users.c.id == ctx.user_id))).mappings().one()


# --------------------------------------------------------------------------- spaces


def _with_role(space: RowMapping, role: str) -> dict:
    return {**space, "role": role}


async def _insert_space(conn: AsyncConnection, owner_id: str, data: SpaceCreate) -> RowMapping:
    stamp = now_iso()
    space_id = new_id()
    row = (
        (
            await conn.execute(
                insert(spaces)
                .values(
                    id=space_id,
                    owner_user_id=owner_id,
                    name=data.name,
                    type=data.type,
                    color=data.color,
                    created_at=stamp,
                    updated_at=stamp,
                )
                .returning(*spaces.c)
            )
        )
        .mappings()
        .one()
    )
    await conn.execute(insert(space_memberships).values(space_id=space_id, user_id=owner_id, role="owner"))
    return row


async def list_spaces(ctx: Ctx, include_archived: bool) -> list[dict]:
    query = (
        select(spaces, space_memberships.c.role)
        .join(space_memberships, space_memberships.c.space_id == spaces.c.id)
        .where(space_memberships.c.user_id == ctx.user_id)
        .order_by(spaces.c.created_at)
    )
    if not include_archived:
        query = query.where(spaces.c.archived_at.is_(None))
    async with ctx.read() as conn:
        return [dict(row) for row in (await conn.execute(query)).mappings()]


async def create_space(ctx: Ctx, data: SpaceCreate) -> dict:
    async with ctx.write() as conn:
        row = await _insert_space(conn, ctx.user_id, data)
        await record_event(conn, ctx, "space.created", ("space", row["id"], 1), row["id"])
    return _with_role(row, "owner")


async def get_space(ctx: Ctx, space_id: str) -> dict:
    async with ctx.read() as conn:
        return dict(await require_space(conn, ctx, space_id, Action.READ))


async def update_space(ctx: Ctx, space_id: str, expected: int, data: SpacePatch) -> dict:
    values = data.model_dump(exclude_unset=True, exclude_none=True)
    if "archived" in values:
        values["archived_at"] = now_iso() if values.pop("archived") else None
    async with ctx.write() as conn:
        current = await require_space(conn, ctx, space_id, Action.MANAGE)
        row = await update_revisioned(conn, spaces, space_id, expected, values)
        await record_event(conn, ctx, "space.changed", ("space", space_id, row["revision"]), space_id)
        if data.archived:
            await audit(conn, ctx, "space.archived", "space", space_id, space_id)
    return _with_role(row, current["role"])


async def delete_space(ctx: Ctx, space_id: str, expected: int, *, hard: bool) -> None:
    """Archive by default. Hard delete removes the space and everything in it."""
    async with ctx.write() as conn:
        await require_space(conn, ctx, space_id, Action.MANAGE)
        if not hard:
            await update_revisioned(conn, spaces, space_id, expected, {"archived_at": now_iso()})
            await audit(conn, ctx, "space.archived", "space", space_id, space_id)
            return
        check_revision(
            space_id, await conn.scalar(select(spaces.c.revision).where(spaces.c.id == space_id)), expected
        )
        await conn.execute(delete(spaces).where(spaces.c.id == space_id))
        await audit(conn, ctx, "space.deleted", "space", space_id, None)
        await record_event(conn, ctx, "space.deleted", ("space", space_id, expected + 1), None)


# --------------------------------------------------------------------------- people


async def list_people(ctx: Ctx, space_id: str) -> list[RowMapping]:
    async with ctx.read() as conn:
        await require_space(conn, ctx, space_id, Action.READ)
        query = select(people).where(people.c.space_id == space_id).order_by(people.c.display_name)
        return list((await conn.execute(query)).mappings())


async def create_person(ctx: Ctx, space_id: str, data: PersonCreate) -> RowMapping:
    stamp = now_iso()
    async with ctx.write() as conn:
        await require_space(conn, ctx, space_id, Action.WRITE)
        row = (
            (
                await conn.execute(
                    insert(people)
                    .values(
                        id=new_id(),
                        space_id=space_id,
                        display_name=data.display_name,
                        color=data.color,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    .returning(*people.c)
                )
            )
            .mappings()
            .one()
        )
        await record_event(conn, ctx, "person.created", ("person", row["id"], 1), space_id)
    return row


async def update_person(ctx: Ctx, person_id: str, expected: int, data: PersonPatch) -> RowMapping:
    async with ctx.write() as conn:
        person = await load_in_space(conn, ctx, people, person_id, Action.WRITE, "Person")
        row = await update_revisioned(conn, people, person_id, expected, data.model_dump(exclude_none=True))
        await record_event(
            conn, ctx, "person.changed", ("person", person_id, row["revision"]), person["space_id"]
        )
    return row


async def delete_person(ctx: Ctx, person_id: str, expected: int) -> None:
    """Remove a person; their open tasks become unassigned (spec §07)."""
    async with ctx.write() as conn:
        person = await load_in_space(conn, ctx, people, person_id, Action.WRITE, "Person")
        check_revision(person_id, person["revision"], expected)
        # Unassign explicitly so each affected task's revision moves (ON DELETE SET NULL would not).
        await conn.execute(
            update(tasks)
            .where(tasks.c.assignee_person_id == person_id)
            .values(assignee_person_id=None, revision=tasks.c.revision + 1, updated_at=now_iso())
        )
        await conn.execute(delete(people).where(people.c.id == person_id))
        await audit(conn, ctx, "person.deleted", "person", person_id, person["space_id"])
        await record_event(
            conn, ctx, "person.deleted", ("person", person_id, expected + 1), person["space_id"]
        )
