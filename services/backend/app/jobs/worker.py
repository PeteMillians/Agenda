"""In-process worker (spec §09): drains the outbox into jobs, then runs jobs one at a time.

Workers are asyncio tasks inside the single sidecar process, so there are no cross-process
claim races. Handlers must be idempotent: a crash after the side effect but before
`succeed` means the job runs again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import outbox
from app.domain.clock import now_iso
from app.jobs import queue
from app.jobs.queue import Job, JobError

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]

# Which job kinds each outbox event fans out to. Empty in Phase 1; Phase 3 adds provider
# writes on event.changed, Phase 5 reminder recalculation on task.* and event.*.
SUBSCRIPTIONS: dict[str, list[str]] = {}
HANDLERS: dict[str, Handler] = {}


class Worker:
    def __init__(
        self,
        engine: AsyncEngine,
        handlers: Mapping[str, Handler] = HANDLERS,
        subscriptions: Mapping[str, list[str]] = SUBSCRIPTIONS,
        *,
        poll_interval: float = 1.0,
        lease: timedelta = timedelta(minutes=2),
    ):
        self.engine = engine
        self.handlers = handlers
        self.subscriptions = subscriptions
        self.poll_interval = poll_interval
        self.lease = lease
        self._wake = asyncio.Event()
        self._stopping = False
        self._current: Job | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="agenda-worker")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self, grace: float = 5.0) -> None:
        """Stop taking work, let the current job finish briefly, then release its lease."""
        self._stopping = True
        self._wake.set()
        if self._task is None:
            return
        done, _ = await asyncio.wait({self._task}, timeout=grace)
        if not done:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            if self._current is not None:
                await queue.release(self.engine, self._current)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.drain_outbox()
                if not await self.run_one():
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except Exception:  # a broken worker must not kill the backend; try again shortly
                log.exception("worker loop error")
                await asyncio.sleep(self.poll_interval)

    async def drain_outbox(self, batch: int = 100) -> int:
        """Turn unprocessed outbox rows into jobs, exactly once per (row, subscriber)."""
        async with self.engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(outbox)
                    .where(outbox.c.processed_at.is_(None))
                    .order_by(outbox.c.created_at)
                    .limit(batch)
                )
            ).mappings()
            rows = list(rows)
            for row in rows:
                envelope = json.loads(row["envelope_json"])
                for kind in self.subscriptions.get(row["event_type"], []):
                    await queue.enqueue(
                        conn, kind, {"event": envelope}, idempotency_key=f"outbox:{row['id']}"
                    )
            if rows:
                await conn.execute(
                    update(outbox)
                    .where(outbox.c.id.in_([r["id"] for r in rows]))
                    .values(processed_at=now_iso())
                )
        return len(rows)

    async def run_one(self) -> bool:
        """Claim and run one job. Returns False when nothing was due."""
        job = await queue.claim(self.engine, self.lease)
        if job is None:
            return False
        self._current = job
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                raise JobError("unknown_kind", retryable=False)
            # finish well inside the lease, or another claim could start a duplicate
            await asyncio.wait_for(handler(job.payload), self.lease.total_seconds() * 0.8)
        except JobError as error:
            state = await queue.fail(self.engine, job, error)
            log.warning("job %s (%s) failed: %s -> %s", job.id, job.kind, error.category, state)
        except TimeoutError:
            await queue.fail(self.engine, job, JobError("timeout", retryable=True))
            log.warning("job %s (%s) timed out", job.id, job.kind)
        except Exception:
            log.exception("job %s (%s) crashed", job.id, job.kind)
            await queue.fail(self.engine, job, JobError("internal", retryable=True))
        else:
            await queue.succeed(self.engine, job)
        finally:
            self._current = None
        return True
