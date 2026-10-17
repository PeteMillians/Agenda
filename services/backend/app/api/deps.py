"""Request dependencies: the database, the calling actor, and If-Match."""

from __future__ import annotations

from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.errors import request_id_of
from app.application.context import Ctx
from app.db.tables import users
from app.domain.errors import Conflict, PreconditionRequired, Unavailable


def engine(request: Request) -> AsyncEngine:
    if request.app.state.engine is None:
        raise Unavailable("Database is not ready.")
    return request.app.state.engine


async def ctx(request: Request, db: Annotated[AsyncEngine, Depends(engine)]) -> Ctx:
    """The actor comes from the local session, never from the request (spec §06).
    v1 has exactly one user, the owner, who exists once setup has run."""
    async with db.connect() as conn:
        owner = (await conn.execute(select(users.c.id, users.c.timezone).limit(1))).first()
    if owner is None:
        raise Conflict("Run first-time setup before using the API.", code="SETUP_REQUIRED")
    return Ctx(
        engine=db, user_id=owner.id, timezone=ZoneInfo(owner.timezone), request_id=request_id_of(request)
    )


def if_match(if_match: Annotated[str | None, Header()] = None) -> int:
    """Expected revision from If-Match. Accepts `3` or `"3"`. Required on every mutation."""
    if if_match is None:
        raise PreconditionRequired("Send If-Match with the revision you loaded.")
    value = if_match.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise PreconditionRequired("If-Match must be a revision number.")
    return int(value)


CtxDep = Annotated[Ctx, Depends(ctx)]
IfMatch = Annotated[int, Depends(if_match)]
