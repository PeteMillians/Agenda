from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.api.deps import CtxDep, IfMatch
from app.api.errors import ERROR_RESPONSES, Envelope
from app.application import lists
from app.schemas.lists import ItemCreate, ItemPatch, ListCreate, ListDetail, ListItem, ListPatch, ListSummary

router = APIRouter(tags=["lists"], responses=ERROR_RESPONSES)


class Cleared(BaseModel):
    removed: int


@router.get("/spaces/{space_id}/lists", response_model=Envelope[list[ListSummary]])
async def list_lists(ctx: CtxDep, space_id: UUID):
    return {"data": await lists.list_lists(ctx, str(space_id)), "request_id": ctx.request_id}


@router.post("/spaces/{space_id}/lists", status_code=201, response_model=Envelope[ListSummary])
async def create_list(ctx: CtxDep, space_id: UUID, body: ListCreate):
    return {"data": await lists.create_list(ctx, str(space_id), body), "request_id": ctx.request_id}


@router.get("/lists/{list_id}", response_model=Envelope[ListDetail])
async def get_list(ctx: CtxDep, list_id: UUID):
    return {"data": await lists.get_list(ctx, str(list_id)), "request_id": ctx.request_id}


@router.patch("/lists/{list_id}", response_model=Envelope[ListSummary])
async def update_list(ctx: CtxDep, revision: IfMatch, list_id: UUID, body: ListPatch):
    return {"data": await lists.update_list(ctx, str(list_id), revision, body), "request_id": ctx.request_id}


@router.delete("/lists/{list_id}", status_code=204)
async def archive_list(ctx: CtxDep, revision: IfMatch, list_id: UUID):
    await lists.archive_list(ctx, str(list_id), revision)
    return Response(status_code=204)


@router.post("/lists/{list_id}/items", status_code=201, response_model=Envelope[ListItem])
async def add_item(ctx: CtxDep, list_id: UUID, body: ItemCreate):
    return {"data": dict(await lists.add_item(ctx, str(list_id), body)), "request_id": ctx.request_id}


@router.post("/lists/{list_id}/clear-checked", response_model=Envelope[Cleared])
async def clear_checked(ctx: CtxDep, list_id: UUID):
    removed = await lists.clear_checked(ctx, str(list_id))
    return {"data": {"removed": removed}, "request_id": ctx.request_id}


@router.patch("/list-items/{item_id}", response_model=Envelope[ListItem])
async def update_item(ctx: CtxDep, revision: IfMatch, item_id: UUID, body: ItemPatch):
    row = await lists.update_item(ctx, str(item_id), revision, body)
    return {"data": dict(row), "request_id": ctx.request_id}


@router.delete("/list-items/{item_id}", status_code=204)
async def delete_item(ctx: CtxDep, revision: IfMatch, item_id: UUID):
    await lists.delete_item(ctx, str(item_id), revision)
    return Response(status_code=204)
