from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.deps import CtxDep, IfMatch
from app.api.errors import ERROR_RESPONSES, Envelope, Paged
from app.application import tasks
from app.schemas.tasks import Task, TaskCreate, TaskPatch

router = APIRouter(tags=["tasks"], responses=ERROR_RESPONSES)


@router.get("/spaces/{space_id}/tasks", response_model=Paged[Task])
async def list_tasks(
    ctx: CtxDep,
    space_id: UUID,
    status: Literal["open", "done", "archived", "all"] = "open",
    assignee_person_id: UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    rows, next_cursor = await tasks.list_tasks(
        ctx, str(space_id), status, str(assignee_person_id) if assignee_person_id else None, cursor, limit
    )
    return {"data": rows, "next_cursor": next_cursor, "request_id": ctx.request_id}


@router.post("/spaces/{space_id}/tasks", status_code=201, response_model=Envelope[Task])
async def create_task(ctx: CtxDep, space_id: UUID, body: TaskCreate):
    return {"data": await tasks.create_task(ctx, str(space_id), body), "request_id": ctx.request_id}


@router.get("/tasks/{task_id}", response_model=Envelope[Task])
async def get_task(ctx: CtxDep, task_id: UUID):
    return {"data": await tasks.get_task(ctx, str(task_id)), "request_id": ctx.request_id}


@router.patch("/tasks/{task_id}", response_model=Envelope[Task])
async def update_task(ctx: CtxDep, revision: IfMatch, task_id: UUID, body: TaskPatch):
    """Completing a repeating chore (status=done) creates its next occurrence."""
    return {"data": await tasks.update_task(ctx, str(task_id), revision, body), "request_id": ctx.request_id}


@router.delete("/tasks/{task_id}", status_code=204)
async def archive_task(ctx: CtxDep, revision: IfMatch, task_id: UUID):
    await tasks.archive_task(ctx, str(task_id), revision)
    return Response(status_code=204)
