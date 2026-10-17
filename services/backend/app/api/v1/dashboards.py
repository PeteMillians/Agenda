from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response

from app.api.deps import CtxDep, IfMatch
from app.api.errors import ERROR_RESPONSES, Envelope
from app.application import dashboards
from app.schemas.dashboards import Dashboard, DashboardCreate, DashboardPatch

router = APIRouter(tags=["dashboards"], responses=ERROR_RESPONSES)


@router.get("/dashboard-profiles", response_model=Envelope[list[Dashboard]])
async def list_dashboards(ctx: CtxDep):
    return {"data": await dashboards.list_dashboards(ctx), "request_id": ctx.request_id}


@router.post("/dashboard-profiles", status_code=201, response_model=Envelope[Dashboard])
async def create_dashboard(ctx: CtxDep, body: DashboardCreate):
    return {"data": await dashboards.create_dashboard(ctx, body), "request_id": ctx.request_id}


@router.get("/dashboard-profiles/{dashboard_id}", response_model=Envelope[Dashboard])
async def get_dashboard(ctx: CtxDep, dashboard_id: UUID):
    return {"data": await dashboards.get_dashboard(ctx, str(dashboard_id)), "request_id": ctx.request_id}


@router.patch("/dashboard-profiles/{dashboard_id}", response_model=Envelope[Dashboard])
async def update_dashboard(ctx: CtxDep, revision: IfMatch, dashboard_id: UUID, body: DashboardPatch):
    row = await dashboards.update_dashboard(ctx, str(dashboard_id), revision, body)
    return {"data": row, "request_id": ctx.request_id}


@router.delete("/dashboard-profiles/{dashboard_id}", status_code=204)
async def delete_dashboard(ctx: CtxDep, revision: IfMatch, dashboard_id: UUID):
    await dashboards.delete_dashboard(ctx, str(dashboard_id), revision)
    return Response(status_code=204)
