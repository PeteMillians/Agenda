"""Phase 0 vertical slice: write a note, restart, read it back."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.errors import ERROR_RESPONSES, AppError, Envelope, request_id_of
from app.db import notes

router = APIRouter(tags=["notes"], responses=ERROR_RESPONSES)


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class Note(BaseModel):
    id: uuid.UUID
    body: str
    created_at: datetime


def engine(request: Request) -> AsyncEngine:
    if request.app.state.engine is None:
        raise AppError(503, "NOT_READY", "Database is not ready.")
    return request.app.state.engine


@router.get("/notes", response_model=Envelope[list[Note]])
async def list_notes(request: Request, db: AsyncEngine = Depends(engine)):
    async with db.connect() as conn:
        rows = (await conn.execute(select(notes).order_by(notes.c.created_at.desc()))).mappings().all()
    return Envelope(data=[Note.model_validate(dict(r)) for r in rows], request_id=request_id_of(request))


@router.post("/notes", status_code=201, response_model=Envelope[Note])
async def create_note(request: Request, body: NoteCreate, db: AsyncEngine = Depends(engine)):
    note = Note(id=uuid.uuid4(), body=body.body, created_at=datetime.now(UTC))
    async with db.begin() as conn:
        await conn.execute(
            insert(notes).values(id=str(note.id), body=note.body, created_at=note.created_at.isoformat())
        )
    return Envelope(data=note, request_id=request_id_of(request))
