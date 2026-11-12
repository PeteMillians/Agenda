from __future__ import annotations

from html import escape
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from app.api.deps import CtxDep, IfMatch
from app.api.errors import ERROR_RESPONSES, Envelope
from app.application import conflicts
from app.application import integrations as service
from app.domain.errors import AppError, Unavailable
from app.integrations.errors import ProviderError
from app.integrations.runtime import Integrations
from app.schemas.integrations import (
    CalendarSource,
    CalendarSourcePatch,
    ConnectStart,
    ExternalAccount,
    OAuthClientIn,
    OAuthClientStatus,
    Provider,
    ResolveConflict,
    SyncConflict,
    SyncStarted,
)

router = APIRouter(tags=["integrations"], responses=ERROR_RESPONSES)


def integrations(request: Request) -> Integrations:
    if getattr(request.app.state, "integrations", None) is None:
        raise Unavailable("Integrations are not ready.")
    return request.app.state.integrations


IntegrationsDep = Annotated[Integrations, Depends(integrations)]


@router.get("/integrations/{provider}/client", response_model=Envelope[OAuthClientStatus])
async def get_client(ctx: CtxDep, deps: IntegrationsDep, provider: Provider):
    return {"data": await service.client_status(ctx, deps, provider), "request_id": ctx.request_id}


@router.put("/integrations/{provider}/client", response_model=Envelope[OAuthClientStatus])
async def set_client(ctx: CtxDep, deps: IntegrationsDep, provider: Provider, body: OAuthClientIn):
    return {"data": await service.set_client(ctx, deps, provider, body), "request_id": ctx.request_id}


@router.post("/integrations/{provider}/connect/start", response_model=Envelope[ConnectStart])
async def connect(
    ctx: CtxDep, request: Request, deps: IntegrationsDep, provider: Provider, write: bool = False
):
    """Begin OAuth (PKCE + state). Open the returned URL in the system browser. `write=true` also
    asks for permission to edit events, which two-way sync needs."""
    url = await service.start_connect(ctx, deps, provider, request.app.state.settings.port, write=write)
    return {"data": {"authorization_url": url}, "request_id": ctx.request_id}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agenda</title>
<style>body{{font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#17202b}}
h1{{font-size:1.4rem}}p{{color:#5b6878}}</style></head><body><h1>{title}</h1><p>{body}</p></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=escape(title), body=escape(body)), status_code=status)


@router.get("/integrations/{provider}/callback", include_in_schema=False)
async def callback(
    request: Request, provider: Provider, state: str = "", code: str = "", error: str = ""
) -> HTMLResponse:
    """Loopback redirect from the provider. The browser has no launch secret, so this route is
    public and trusts only a one-time state issued by this process."""
    deps = getattr(request.app.state, "integrations", None)
    name = service.NAMES[provider]
    if deps is None:
        return _page("Agenda isn't ready", "Wait a moment and try connecting again.", 503)
    if error:
        deps.pending.take(state)
        return _page("Not connected", f"{name} sign-in was cancelled. You can close this tab.", 400)
    try:
        email = await service.complete_connect(deps, provider, state, code)
    except AppError as exc:
        return _page("Not connected", exc.message, exc.status)
    except ProviderError as exc:
        return _page(
            "Not connected", f"{name} returned an error ({exc.category}). Try again from Agenda.", 502
        )
    return _page("Connected", f"Agenda can now reach {email}'s calendars. You can close this tab.")


@router.get("/external-accounts", response_model=Envelope[list[ExternalAccount]])
async def list_accounts(ctx: CtxDep):
    return {"data": await service.list_accounts(ctx), "request_id": ctx.request_id}


@router.post("/external-accounts/{account_id}/refresh-calendars", status_code=204)
async def refresh_calendars(ctx: CtxDep, deps: IntegrationsDep, account_id: UUID):
    """Pick up calendars added or removed at the provider."""
    await service.refresh_calendars(ctx, deps, str(account_id))
    return Response(status_code=204)


@router.delete("/external-accounts/{account_id}", status_code=204)
async def disconnect(ctx: CtxDep, deps: IntegrationsDep, account_id: UUID, keep_events: bool = False):
    """Revoke access at the provider and forget the token. `keep_events=true` turns the imported
    events into Agenda's own; otherwise they are removed."""
    await service.disconnect(ctx, deps, str(account_id), keep_events=keep_events)
    return Response(status_code=204)


@router.get("/calendar-sources", response_model=Envelope[list[CalendarSource]])
async def list_sources(ctx: CtxDep):
    return {"data": await service.list_sources(ctx), "request_id": ctx.request_id}


@router.patch("/calendar-sources/{source_id}", response_model=Envelope[CalendarSource])
async def update_source(
    ctx: CtxDep, deps: IntegrationsDep, revision: IfMatch, source_id: UUID, body: CalendarSourcePatch
):
    row = await service.update_source(ctx, deps, str(source_id), revision, body)
    return {"data": row, "request_id": ctx.request_id}


@router.post("/calendar-sources/{source_id}/sync", status_code=202, response_model=Envelope[SyncStarted])
async def sync_source(ctx: CtxDep, deps: IntegrationsDep, source_id: UUID):
    job_id = await service.sync_now(ctx, deps, str(source_id))
    return {"data": {"job_ids": [job_id]}, "request_id": ctx.request_id}


@router.post("/sync/run", status_code=202, response_model=Envelope[SyncStarted])
async def sync_all(ctx: CtxDep, deps: IntegrationsDep):
    """Poll every enabled calendar now, e.g. when the window regains focus. Repeated calls within a
    minute share one job per calendar."""
    return {"data": {"job_ids": await deps.enqueue_due(force=True)}, "request_id": ctx.request_id}


@router.get("/sync-conflicts", response_model=Envelope[list[SyncConflict]])
async def list_conflicts(ctx: CtxDep, include_closed: bool = False):
    """Events changed both here and at the provider, waiting for a decision."""
    return {
        "data": await conflicts.list_conflicts(ctx, include_closed=include_closed),
        "request_id": ctx.request_id,
    }


@router.post("/sync-conflicts/{conflict_id}/resolve", response_model=Envelope[SyncConflict])
async def resolve_conflict(ctx: CtxDep, deps: IntegrationsDep, conflict_id: UUID, body: ResolveConflict):
    """Keep this version, the provider's, or both (as two separate events). A sync job then
    brings the provider in line."""
    row = await conflicts.resolve(ctx, deps, str(conflict_id), body.resolution)
    return {"data": row, "request_id": ctx.request_id}
