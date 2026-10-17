from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CtxDep
from app.api.errors import ERROR_RESPONSES, Envelope
from app.db.tables import jobs
from app.domain.errors import NotFound

router = APIRouter(tags=["jobs"], responses=ERROR_RESPONSES)


class JobStatus(BaseModel):
    """Safe status only: never payloads, credentials, or provider responses (spec §09)."""

    id: UUID
    kind: str
    state: Literal["queued", "leased", "succeeded", "dead_letter", "cancelled"]
    attempts: int
    max_attempts: int
    available_at: datetime
    last_error_category: str | None
    created_at: datetime
    updated_at: datetime


@router.get("/jobs/{job_id}", response_model=Envelope[JobStatus])
async def get_job(ctx: CtxDep, job_id: UUID):
    columns = [jobs.c[name] for name in JobStatus.model_fields]
    async with ctx.read() as conn:
        row = (await conn.execute(select(*columns).where(jobs.c.id == str(job_id)))).mappings().first()
    if row is None:
        raise NotFound("Job")
    return {"data": dict(row), "request_id": ctx.request_id}
