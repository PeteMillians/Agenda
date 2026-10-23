from __future__ import annotations

from html import escape
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse

from app.api.deps import CtxDep, IfMatch
from app.api.errors import ERROR_RESPONSES, Envelope
from app.application import integrations as service
from app.domain.errors import AppError, Unavailable
from app.integrations.base import ProviderError
from app.integrations.providers import LABELS
from app.integrations.runtime import Integrations
from app.schemas.integrations import (
    CalendarSource,
    CalendarSourcePatch,
    ConflictResolution,
    ConnectStart,
    ExternalAccount,
    MicrosoftClientIn,
    OAuthClientIn,
    OAuthClientStatus,
    SyncConflict,
    SyncStarted,
)

router = APIRouter(tags=["integrations"], responses=ERROR_RESPONSES)


def integrations(request: Request) -> Integrations:
    if getattr(request.app.state, "integrations", None) is None:
        raise Unavailable("Integrations are not ready.")
    return request.app.state.integrations


IntegrationsDep = Annotated[Integrations, Depends(integrations)]
ProviderPath = Literal["google", "microsoft"]


@router.get("/integrations/google/client", response_model=Envelope[OAuthClientStatus])
async def google_client(ctx: CtxDep, deps: IntegrationsDep):
    return {"data": await service.client_status(ctx, deps), "request_id": ctx.request_id}


@router.put("/integrations/google/client", response_model=Envelope[OAuthClientStatus])
async def set_google_client(ctx: CtxDep, deps: IntegrationsDep, body: OAuthClientIn):
    return {"data": await service.set_client(ctx, deps, body), "request_id": ctx.request_id}


@router.get("/integrations/microsoft/client", response_model=Envelope[OAuthClientStatus])
async def microsoft_client(ctx: CtxDep, deps: IntegrationsDep):
    return {"data": await service.client_status(ctx, deps, "microsoft"), "request_id": ctx.request_id}


@router.put("/integrations/microsoft/client", response_model=Envelope[OAuthClientStatus])
async def set_microsoft_client(ctx: CtxDep, deps: IntegrationsDep, body: MicrosoftClientIn):
    return {"data": await service.set_microsoft_client(ctx, deps, body), "request_id": ctx.request_id}


@router.post("/integrations/{provider}/connect/start", response_model=Envelope[ConnectStart])
async def connect(
    ctx: CtxDep,
    request: Request,
    deps: IntegrationsDep,
    provider: ProviderPath,
    write: Annotated[bool, Query(description="Also ask for event write access (two-way sync)")] = False,
):
    """Begin OAuth (PKCE + state). Open the returned URL in the system browser. Signing in again
    with `write=true` upgrades an existing account in place."""
    url = await service.start(ctx, deps, provider, request.app.state.settings.port, write=write)
    return {"data": {"authorization_url": url}, "request_id": ctx.request_id}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agenda</title>
<style>body{{font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#17202b}}
h1{{font-size:1.4rem}}p{{color:#5b6878}}</style></head><body><h1>{title}</h1><p>{body}</p></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=escape(title), body=escape(body)), status_code=status)


# What Entra's AADSTS codes usually mean for this app; the code itself is safe to show.
ENTRA_HINTS = {
    "AADSTS50011": "the redirect URI isn't registered. Add the one shown in Agenda under"
    " Authentication > Mobile and desktop applications",
    "AADSTS700016": "the application (client) ID wasn't found in this directory. Check the ID, or set"
    " the sign-in authority to match the app's supported account types",
    "AADSTS50020": "this account type isn't allowed by the app registration. Allow personal Microsoft"
    " accounts, or use the matching sign-in authority",
    "AADSTS50194": "the app is single-tenant. Set the sign-in authority to your directory (tenant) ID",
    "AADSTS9002346": "the app is for personal accounts only. Set the sign-in authority to consumers",
    "AADSTS65001": "consent was not granted",
}


def _sign_in_error(provider: str, error: str, description: str) -> str:
    label = LABELS[provider]
    if error == "access_denied" and "AADSTS" not in description:
        return f"{label} sign-in was cancelled. You can close this tab."
    for code, hint in ENTRA_HINTS.items():
        if code in description:
            return f"Microsoft refused the sign-in ({code}): {hint}."
    return f"{label} refused the sign-in ({escape(error)[:60]}). Check the app registration, then try again."


@router.get("/integrations/{provider}/callback", include_in_schema=False)
async def oauth_callback(
    request: Request,
    provider: ProviderPath,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> HTMLResponse:
    """Loopback redirect from the provider. The browser has no launch secret, so this route is
    public and trusts only a one-time state issued by this process."""
    deps = getattr(request.app.state, "integrations", None)
    if deps is None:
        return _page("Agenda isn't ready", "Wait a moment and try connecting again.", 503)
    label = LABELS[provider]
    if error:
        deps.pending.take(state)
        return _page("Not connected", _sign_in_error(provider, error, error_description), 400)
    try:
        email = await service.complete(deps, provider, state, code)
    except AppError as exc:
        return _page("Not connected", exc.message, exc.status)
    except ProviderError as exc:
        return _page(
            "Not connected", f"{label} returned an error ({exc.category}). Try again from Agenda.", 502
        )
    return _page("Connected", f"Agenda is connected to {email}'s calendars. You can close this tab.")


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


@router.get("/sync-conflicts", response_model=Envelope[list[SyncConflict]])
async def list_conflicts(ctx: CtxDep):
    """Events changed both in Agenda and at the provider. Neither side is overwritten until resolved."""
    return {"data": await service.list_conflicts(ctx), "request_id": ctx.request_id}


@router.post("/sync-conflicts/{conflict_id}/resolve", status_code=204)
async def resolve_conflict(ctx: CtxDep, deps: IntegrationsDep, conflict_id: UUID, body: ConflictResolution):
    await service.resolve_conflict(ctx, deps, str(conflict_id), body.resolution)
    return Response(status_code=204)


@router.post("/sync/run", status_code=202, response_model=Envelope[SyncStarted])
async def sync_all(ctx: CtxDep, deps: IntegrationsDep):
    """Poll every enabled calendar now, e.g. when the window regains focus. Repeated calls within a
    minute share one job per calendar."""
    return {"data": {"job_ids": await deps.enqueue_due(force=True)}, "request_id": ctx.request_id}
