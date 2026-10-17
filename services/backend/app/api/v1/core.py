"""Setup, profile, spaces, and people (spec §07)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import CtxDep, IfMatch, engine
from app.api.errors import ERROR_RESPONSES, Envelope, request_id_of
from app.application import core
from app.schemas.core import (
    Person,
    PersonCreate,
    PersonPatch,
    SetupRequest,
    SetupStatus,
    Space,
    SpaceCreate,
    SpacePatch,
    User,
    UserPatch,
)

router = APIRouter(responses=ERROR_RESPONSES)

EngineDep = Annotated[AsyncEngine, Depends(engine)]


@router.get("/setup/status", tags=["setup"], response_model=Envelope[SetupStatus])
async def setup_status(request: Request, db: EngineDep):
    completed = await core.setup_completed(db)
    return Envelope(data=SetupStatus(completed=completed), request_id=request_id_of(request))


@router.post("/setup", tags=["setup"], status_code=201, response_model=Envelope[User])
async def setup(request: Request, db: EngineDep, body: SetupRequest):
    return {"data": dict(await core.run_setup(db, body)), "request_id": request_id_of(request)}


@router.get("/users/me", tags=["profile"], response_model=Envelope[User])
async def me(ctx: CtxDep):
    return {"data": dict(await core.get_me(ctx)), "request_id": ctx.request_id}


@router.patch("/users/me", tags=["profile"], response_model=Envelope[User])
async def update_me(ctx: CtxDep, body: UserPatch):
    return {"data": dict(await core.update_me(ctx, body)), "request_id": ctx.request_id}


@router.get("/spaces", tags=["spaces"], response_model=Envelope[list[Space]])
async def list_spaces(ctx: CtxDep, include_archived: bool = False):
    return {"data": await core.list_spaces(ctx, include_archived), "request_id": ctx.request_id}


@router.post("/spaces", tags=["spaces"], status_code=201, response_model=Envelope[Space])
async def create_space(ctx: CtxDep, body: SpaceCreate):
    return {"data": await core.create_space(ctx, body), "request_id": ctx.request_id}


@router.get("/spaces/{space_id}", tags=["spaces"], response_model=Envelope[Space])
async def get_space(ctx: CtxDep, space_id: UUID):
    return {"data": await core.get_space(ctx, str(space_id)), "request_id": ctx.request_id}


@router.patch("/spaces/{space_id}", tags=["spaces"], response_model=Envelope[Space])
async def update_space(ctx: CtxDep, revision: IfMatch, space_id: UUID, body: SpacePatch):
    return {"data": await core.update_space(ctx, str(space_id), revision, body), "request_id": ctx.request_id}


@router.delete("/spaces/{space_id}", tags=["spaces"], status_code=204)
async def delete_space(
    ctx: CtxDep,
    revision: IfMatch,
    space_id: UUID,
    hard: Annotated[bool, Query(description="Permanently delete the space and all its data.")] = False,
):
    await core.delete_space(ctx, str(space_id), revision, hard=hard)
    return Response(status_code=204)


@router.get("/spaces/{space_id}/people", tags=["people"], response_model=Envelope[list[Person]])
async def list_people(ctx: CtxDep, space_id: UUID):
    rows = await core.list_people(ctx, str(space_id))
    return {"data": [dict(r) for r in rows], "request_id": ctx.request_id}


@router.post("/spaces/{space_id}/people", tags=["people"], status_code=201, response_model=Envelope[Person])
async def create_person(ctx: CtxDep, space_id: UUID, body: PersonCreate):
    return {"data": dict(await core.create_person(ctx, str(space_id), body)), "request_id": ctx.request_id}


@router.patch("/people/{person_id}", tags=["people"], response_model=Envelope[Person])
async def update_person(ctx: CtxDep, revision: IfMatch, person_id: UUID, body: PersonPatch):
    row = await core.update_person(ctx, str(person_id), revision, body)
    return {"data": dict(row), "request_id": ctx.request_id}


@router.delete("/people/{person_id}", tags=["people"], status_code=204)
async def delete_person(ctx: CtxDep, revision: IfMatch, person_id: UUID):
    await core.delete_person(ctx, str(person_id), revision)
    return Response(status_code=204)
