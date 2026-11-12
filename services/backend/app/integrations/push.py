"""Sending Agenda's edits to a two-way provider calendar (spec §10, ADR 0005).

Every row of the calendar with `needs_push` is sent, masters and single events before instance
overrides (an override is addressed through its series' provider id):

- a write carries the stored ETag as If-Match, so a provider copy that changed in the meantime is
  never overwritten: a 412 fetches the provider's version and opens a conflict instead;
- inserts use a deterministic client key, so a retry after a crash can't create a duplicate;
- `needs_push` is cleared only if the row still has the revision that was sent, so an edit made
  while the request was in flight is sent by the next run.

Network calls happen outside any transaction; each row's bookkeeping is one short transaction.
Retryable failures (offline, 429, 5xx) leave the rows pending and the job retries with backoff:
that is the offline write queue.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import event_mappings, events
from app.integrations import conflicts
from app.integrations.base import (
    AccessExpired,
    Gone,
    PreconditionFailed,
    ProviderClient,
    Unsupported,
    Written,
)
from app.integrations.providers import Codec

log = logging.getLogger(__name__)

TokenFn = Callable[..., Awaitable[str]]


@dataclass
class PushSummary:
    written: int = 0
    deleted: int = 0
    conflicts: int = 0
    skipped: int = 0


class _Push:
    def __init__(
        self, engine: AsyncEngine, source: RowMapping, client: ProviderClient, codec: Codec, token: TokenFn
    ):
        self.engine = engine
        self.source = source
        self.client = client
        self.codec = codec
        self.token = token
        self.calendar = source["provider_calendar_id"]
        self.summary = PushSummary()

    async def call(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(await self.token(), self.calendar, *args, **kwargs)
        except AccessExpired:
            return await fn(await self.token(force=True), self.calendar, *args, **kwargs)

    # ------------------------------------------------------------------ bookkeeping

    async def _pending(self) -> list[RowMapping]:
        async with self.engine.connect() as conn:
            blocked = await conflicts.open_conflict_ids(conn, self.source["id"])
            rows = list(
                (
                    await conn.execute(
                        select(
                            events,
                            event_mappings.c.provider_event_id,
                            event_mappings.c.etag,
                            event_mappings.c.last_synced_revision,
                        )
                        .outerjoin(event_mappings, event_mappings.c.event_id == events.c.id)
                        .where(
                            events.c.calendar_source_id == self.source["id"], events.c.needs_push.is_(True)
                        )
                        .order_by(events.c.series_id.is_not(None), events.c.created_at)
                    )
                ).mappings()
            )
        # a conflicted series holds back its overrides too, until the user decides
        return [r for r in rows if r["id"] not in blocked and r["series_id"] not in blocked]

    async def _done(self, row: RowMapping, written: Written | None, provider_id: str | None = None) -> None:
        """Record what the provider now holds for `row` and clear needs_push if nothing newer
        was saved meanwhile. `written=None` with no provider id drops the mapping."""
        async with self.engine.begin() as conn:
            await conn.execute(delete(event_mappings).where(event_mappings.c.event_id == row["id"]))
            pid = written.provider_event_id if written else provider_id
            if pid is not None:
                # another row may hold this provider id (e.g. a re-keyed override): it moves here
                await conn.execute(
                    delete(event_mappings).where(
                        event_mappings.c.calendar_source_id == self.source["id"],
                        event_mappings.c.provider_event_id == pid,
                    )
                )
                await conn.execute(
                    insert(event_mappings).values(
                        event_id=row["id"],
                        calendar_source_id=self.source["id"],
                        provider_event_id=pid,
                        etag=written.etag if written else None,
                        last_synced_revision=row["revision"],
                    )
                )
            await conn.execute(
                update(events)
                .where(events.c.id == row["id"], events.c.revision == row["revision"])
                .values(needs_push=False)
            )

    async def _conflict(self, row: RowMapping, provider_id: str) -> None:
        remote = await self.call(self.client.get_event, provider_id)
        async with self.engine.begin() as conn:
            current = (await conn.execute(select(events).where(events.c.id == row["id"]))).mappings().one()
            await conflicts.record(
                conn, current, self.source["id"], remote, (remote or {}).get("etag") or _etag(remote)
            )
        self.summary.conflicts += 1

    # ------------------------------------------------------------------ rows

    async def run(self) -> PushSummary:
        for row in await self._pending():
            if row["series_id"] is None:
                await self._top(row)
            else:
                await self._override(row)
        return self.summary

    def _body(self, row: RowMapping) -> dict[str, Any] | None:
        try:
            return self.codec.to_provider(dict(row))
        except Unsupported as reason:
            log.warning("not sent to the provider: %s", reason)
            self.summary.skipped += 1
            return None

    async def _top(self, row: RowMapping) -> None:
        pid, etag = row["provider_event_id"], row["etag"]
        if row["deleted_at"] is not None:
            if pid is not None:
                try:
                    await self.call(self.client.delete_event, pid, etag)
                except PreconditionFailed:
                    await self._conflict(row, pid)  # changed there after we last saw it
                    return
                self.summary.deleted += 1
            await self._done(row, None)
            async with self.engine.begin() as conn:  # its overrides went with it
                await conn.execute(
                    delete(event_mappings).where(
                        event_mappings.c.event_id.in_(
                            select(events.c.id).where(events.c.series_id == row["id"])
                        )
                    )
                )
            return
        body = self._body(row)
        if body is None:
            await self._done(row, Written(pid, etag, {}) if pid else None)
            return
        if pid is None:
            written = await self.call(
                self.client.insert_event, body, client_key=self.codec.client_key(row["id"], row["revision"])
            )
        elif row["revision"] == row["last_synced_revision"]:
            await self._done(row, Written(pid, etag, {}))
            return
        else:
            try:
                written = await self.call(self.client.patch_event, pid, body, etag)
            except (PreconditionFailed, Gone):
                await self._conflict(row, pid)
                return
        self.summary.written += 1
        await self._done(row, written)

    async def _override(self, row: RowMapping) -> None:
        """One occurrence of a series (Google only): addressed as `<master id>_<original start>`."""
        if self.codec.instance_id is None:
            await self._done(row, None)
            return
        async with self.engine.connect() as conn:
            master_pid = await conn.scalar(
                select(event_mappings.c.provider_event_id).where(
                    event_mappings.c.event_id == row["series_id"]
                )
            )
            master_pending = await conn.scalar(
                select(events.c.needs_push).where(
                    and_(events.c.id == row["series_id"], events.c.needs_push.is_(True))
                )
            )
        if master_pid is None or master_pending:
            return  # the series isn't at the provider yet; this run or the next sends it first
        pid = self.codec.instance_id(master_pid, row["original_start"])
        etag = row["etag"] if row["provider_event_id"] == pid else None
        if row["deleted_at"] is not None:
            # a tombstoned override means the series itself changed; the master's write covers it
            await self._done(row, None)
            return
        if row["status"] == "cancelled":
            try:
                await self.call(self.client.delete_event, pid, etag)
            except PreconditionFailed:
                await self._conflict(row, pid)
                return
            self.summary.deleted += 1
            await self._done(row, None, pid)
            return
        body = self._body(row)
        if body is None:
            await self._done(row, None)
            return
        try:
            written = await self.call(self.client.patch_event, pid, body, etag)
        except PreconditionFailed:
            await self._conflict(row, pid)
            return
        except Gone:
            log.warning("occurrence not found at the provider; left as an Agenda-only change")
            self.summary.skipped += 1
            await self._done(row, None)
            return
        self.summary.written += 1
        await self._done(row, written)


def _etag(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    return item.get("@odata.etag") or (f'W/"{item["changeKey"]}"' if item.get("changeKey") else None)


async def push_source(
    engine: AsyncEngine, source: RowMapping, client: ProviderClient, codec: Codec, token: TokenFn
) -> PushSummary:
    return await _Push(engine, source, client, codec, token).run()
