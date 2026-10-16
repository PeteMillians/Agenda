from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.errors import ErrorEnvelope, error_response

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["live", "ready"]


@router.get("/live", response_model=HealthStatus)
async def live() -> HealthStatus:
    return HealthStatus(status="live")


@router.get("/ready", response_model=HealthStatus, responses={503: {"model": ErrorEnvelope}})
async def ready(request: Request):
    state = request.app.state
    if state.engine is not None:
        return HealthStatus(status="ready")
    if state.startup_error is not None:
        code, message = state.startup_error
        return error_response(request, 503, code, message)
    return error_response(request, 503, "NOT_READY", "Database is still starting.")
