from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.deps import CtxDep, IfMatch
from app.api.errors import ERROR_RESPONSES, Envelope
from app.application import events
from app.schemas.events import Event, EventCreate, EventPatch, Occurrence, Scope

router = APIRouter(tags=["events"], responses=ERROR_RESPONSES)

ScopeParam = Annotated[Scope | None, Query(description="Required for recurring events.")]
OccurrenceParam = Annotated[
    str | None, Query(description="`occurrence_start` of the occurrence, for scope=this or following.")
]


@router.get("/spaces/{space_id}/events", response_model=Envelope[list[Occurrence]])
async def list_events(
    ctx: CtxDep,
    space_id: UUID,
    start: Annotated[date, Query(alias="from", description="First day, in your time zone.")],
    end: Annotated[date, Query(alias="to", description="Day after the last day (exclusive).")],
):
    """Occurrences in a window, with recurring series expanded and exceptions applied."""
    return {
        "data": await events.list_occurrences(ctx, str(space_id), start, end),
        "request_id": ctx.request_id,
    }


@router.post("/spaces/{space_id}/events", status_code=201, response_model=Envelope[Event])
async def create_event(ctx: CtxDep, space_id: UUID, body: EventCreate):
    return {"data": await events.create_event(ctx, str(space_id), body), "request_id": ctx.request_id}


@router.get("/events/{event_id}", response_model=Envelope[Event])
async def get_event(ctx: CtxDep, event_id: UUID):
    return {"data": await events.get_event(ctx, str(event_id)), "request_id": ctx.request_id}


@router.patch("/events/{event_id}", response_model=Envelope[Event])
async def update_event(
    ctx: CtxDep,
    revision: IfMatch,
    event_id: UUID,
    body: EventPatch,
    scope: ScopeParam = None,
    occurrence_start: OccurrenceParam = None,
):
    """Returns the row that now holds the change: the event, the series master, the new
    series created by scope=following, or the exception created by scope=this."""
    row = await events.update_event(ctx, str(event_id), revision, body, scope, occurrence_start)
    return {"data": row, "request_id": ctx.request_id}


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(
    ctx: CtxDep,
    revision: IfMatch,
    event_id: UUID,
    scope: ScopeParam = None,
    occurrence_start: OccurrenceParam = None,
):
    await events.delete_event(ctx, str(event_id), revision, scope, occurrence_start)
    return Response(status_code=204)
