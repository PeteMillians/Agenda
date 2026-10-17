"""Durable job queue in SQLite (spec §09).

queued -> leased -> succeeded; a retryable failure returns to queued after backoff, and a
permanent failure (or running out of attempts) ends in dead_letter. A lease that expires
because the process died makes the job claimable again, so work resumes after a crash.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import jobs
from app.domain.clock import iso, new_id, now, now_iso

BACKOFF_BASE = timedelta(seconds=5)
BACKOFF_CAP = timedelta(minutes=30)


class JobError(Exception):
    """Raise from a handler to control what happens next. `category` is safe to show users."""

    def __init__(self, category: str, *, retryable: bool, retry_after: timedelta | None = None):
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.retry_after = retry_after  # e.g. a provider's Retry-After


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @classmethod
    def of(cls, row: RowMapping) -> Job:
        return cls(row["id"], row["kind"], json.loads(row["payload"]), row["attempts"], row["max_attempts"])


async def enqueue(
    conn: AsyncConnection,
    kind: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    delay: timedelta = timedelta(0),
    max_attempts: int = 5,
) -> str:
    """Add a job in the caller's transaction. With an idempotency key, a duplicate returns
    the existing job's id instead of creating another."""
    stamp = now_iso()
    job_id = new_id()
    result = await conn.execute(
        insert(jobs)
        .values(
            id=job_id,
            kind=kind,
            payload=json.dumps(payload),
            schema_version=1,
            state="queued",
            max_attempts=max_attempts,
            available_at=iso(now() + delay),
            idempotency_key=idempotency_key,
            created_at=stamp,
            updated_at=stamp,
        )
        .on_conflict_do_nothing()
    )
    if result.rowcount:
        return job_id
    existing = await conn.scalar(
        select(jobs.c.id).where(jobs.c.kind == kind, jobs.c.idempotency_key == idempotency_key)
    )
    assert existing is not None
    return existing


async def claim(engine: AsyncEngine, lease: timedelta) -> Job | None:
    """Lease the next due job. One short write transaction, never held across the job's work."""
    stamp = now()
    due = (
        select(jobs.c.id)
        .where(
            or_(
                (jobs.c.state == "queued") & (jobs.c.available_at <= iso(stamp)),
                (jobs.c.state == "leased") & (jobs.c.lease_until < iso(stamp)),  # holder died
            )
        )
        .order_by(jobs.c.available_at)
        .limit(1)
        .scalar_subquery()
    )
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    update(jobs)
                    .where(jobs.c.id == due)
                    .values(
                        state="leased",
                        lease_until=iso(stamp + lease),
                        attempts=jobs.c.attempts + 1,
                        updated_at=iso(stamp),
                    )
                    .returning(*jobs.c)
                )
            )
            .mappings()
            .first()
        )
    return Job.of(row) if row else None


async def succeed(engine: AsyncEngine, job: Job) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(jobs)
            .where(jobs.c.id == job.id)
            .values(state="succeeded", lease_until=None, updated_at=now_iso())
        )


def backoff(attempts: int) -> timedelta:
    """Exponential with jitter: 5s, 10s, 20s ... capped at 30 minutes, each scaled by 0.5-1.0."""
    base = min(BACKOFF_BASE * 2 ** max(attempts - 1, 0), BACKOFF_CAP)
    return base * (0.5 + random.random() / 2)


async def fail(engine: AsyncEngine, job: Job, error: JobError) -> str:
    """Record a failure; returns the new state."""
    if error.retryable and job.attempts < job.max_attempts:
        state = "queued"
        available_at = iso(now() + max(backoff(job.attempts), error.retry_after or timedelta(0)))
    else:
        state, available_at = "dead_letter", None
    values: dict[str, Any] = {
        "state": state,
        "lease_until": None,
        "last_error_category": error.category,
        "updated_at": now_iso(),
    }
    if available_at:
        values["available_at"] = available_at
    async with engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job.id).values(**values))
    return state


async def release(engine: AsyncEngine, job: Job) -> None:
    """Give a lease back unfinished (shutdown). The interrupted attempt does not count."""
    async with engine.begin() as conn:
        await conn.execute(
            update(jobs)
            .where(jobs.c.id == job.id, jobs.c.state == "leased")
            .values(state="queued", lease_until=None, attempts=jobs.c.attempts - 1, updated_at=now_iso())
        )
