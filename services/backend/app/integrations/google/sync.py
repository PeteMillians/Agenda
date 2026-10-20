"""Read-only Google Calendar import (spec §10, Phase 3).

Fetch every page first (no transaction held across network calls), then apply the changes in
one short write transaction: masters and single events before instance overrides, because
Google does not order them. A full sync reconciles by event_mappings, so it never duplicates and
it removes anything Google no longer has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.events import Timing
from app.db.tables import event_mappings, events
from app.domain.clock import new_id, now_iso
from app.integrations.google.mapping import Incoming, Unsupported, parse

log = logging.getLogger(__name__)


@dataclass
class Summary:
    full: bool
    created: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0


class _Apply:
    def __init__(self, conn: AsyncConnection, source: RowMapping, full: bool):
        self.conn = conn
        self.source = source
        self.summary = Summary(full=full)
        self.mapped: dict[str, tuple[str, str | None]] = {}  # provider id -> (event id, etag)
        self.seen: set[str] = set()

    async def run(self, items: list[dict[str, Any]]) -> Summary:
        rows = await self.conn.execute(
            select(
                event_mappings.c.provider_event_id, event_mappings.c.event_id, event_mappings.c.etag
            ).where(event_mappings.c.calendar_source_id == self.source["id"])
        )
        self.mapped = {r.provider_event_id: (r.event_id, r.etag) for r in rows}
        parsed: list[Incoming] = []
        for item in items:
            try:
                parsed.append(parse(item, self.source["timezone"]))
            except Unsupported as reason:
                self.summary.skipped += 1
                log.warning("skipping provider event: %s", reason)
                await self._remove(item["id"])  # it may have been importable before
            except (KeyError, ValueError):
                self.summary.skipped += 1
                log.warning("skipping malformed provider event")
        for incoming in parsed:
            if not incoming.is_instance:
                await self._apply_top(incoming)
        for incoming in parsed:
            if incoming.is_instance:
                await self._apply_instance(incoming)
        if self.summary.full:
            for provider_id in set(self.mapped) - self.seen:
                await self._remove(provider_id)
        return self.summary

    # ------------------------------------------------------------------ helpers

    async def _remove(self, provider_id: str) -> None:
        self.seen.add(provider_id)
        found = self.mapped.pop(provider_id, None)
        if found:
            # imported rows are a cache of the provider: delete outright (exceptions and
            # mappings cascade)
            await self.conn.execute(delete(events).where(events.c.id == found[0]))
            self.summary.removed += 1

    async def _map(self, provider_id: str, event_id: str, etag: str | None) -> None:
        revision = await self.conn.scalar(select(events.c.revision).where(events.c.id == event_id))
        existed = provider_id in self.mapped
        self.mapped[provider_id] = (event_id, etag)
        if existed:
            await self.conn.execute(
                update(event_mappings)
                .where(event_mappings.c.event_id == event_id)
                .values(etag=etag, last_synced_revision=revision, provider_event_id=provider_id)
            )
        else:
            await self.conn.execute(
                insert(event_mappings).values(
                    event_id=event_id,
                    calendar_source_id=self.source["id"],
                    provider_event_id=provider_id,
                    etag=etag,
                    last_synced_revision=revision,
                )
            )

    async def _upsert(self, event_id: str | None, values: dict[str, Any]) -> str:
        stamp = now_iso()
        if event_id:
            await self.conn.execute(
                update(events)
                .where(events.c.id == event_id)
                .values(**values, revision=events.c.revision + 1, updated_at=stamp)
            )
            self.summary.updated += 1
            return event_id
        event_id = new_id()
        await self.conn.execute(
            insert(events).values(
                id=event_id,
                space_id=self.source["space_id"],
                calendar_source_id=self.source["id"],
                created_at=stamp,
                updated_at=stamp,
                **values,
            )
        )
        self.summary.created += 1
        return event_id

    # ------------------------------------------------------------------ masters and singles

    async def _apply_top(self, incoming: Incoming) -> None:
        self.seen.add(incoming.provider_id)
        if incoming.cancelled:
            await self._remove(incoming.provider_id)
            return
        found = self.mapped.get(incoming.provider_id)
        if found and found[1] == incoming.etag and incoming.etag is not None:
            return  # unchanged
        event_id = await self._upsert(
            found[0] if found else None,
            {**incoming.content, **(incoming.timing or {}), "rrule": incoming.rrule, "status": "confirmed"},
        )
        await self._map(incoming.provider_id, event_id, incoming.etag)
        await self._reset_derived_exceptions(event_id, incoming)

    async def _reset_derived_exceptions(self, master_id: str, incoming: Incoming) -> None:
        """EXDATE lines become cancelled exception rows that have no provider id of their own;
        rebuild them whenever the master changes. Mapped overrides are left alone."""
        mapped_ids = {event_id for event_id, _ in self.mapped.values()}
        rows = await self.conn.execute(
            select(events.c.id, events.c.original_start).where(events.c.series_id == master_id)
        )
        existing = list(rows)
        if incoming.rrule is None:  # no longer a series: nothing can hang off it
            await self.conn.execute(delete(events).where(events.c.series_id == master_id))
            return
        derived = [r.id for r in existing if r.id not in mapped_ids]
        if derived:
            await self.conn.execute(delete(events).where(events.c.id.in_(derived)))
        overridden = {r.original_start for r in existing if r.id in mapped_ids}
        master = (await self.conn.execute(select(events).where(events.c.id == master_id))).mappings().one()
        for key in incoming.exdate_keys:
            if key not in overridden:
                await self._upsert(None, self._cancelled_row(master, key))

    # ------------------------------------------------------------------ instance overrides

    def _occurrence_timing(self, master: RowMapping, key: str) -> dict[str, str | None]:
        first = Timing.of(master)
        start: date | datetime = (
            date.fromisoformat(key[:10]) if first.all_day else datetime.fromisoformat(key)
        )
        return first.starting(start).columns()

    def _cancelled_row(self, master: RowMapping, key: str) -> dict[str, Any]:
        return {
            "title": master["title"],
            "description": "",
            "location": "",
            "timezone": master["timezone"],
            **self._occurrence_timing(master, key),
            "series_id": master["id"],
            "original_start": key,
            "status": "cancelled",
        }

    async def _apply_instance(self, incoming: Incoming) -> None:
        self.seen.add(incoming.provider_id)
        master_entry = self.mapped.get(incoming.series_provider_id or "")
        if master_entry is None or incoming.original_key is None:
            self.summary.skipped += 1
            log.warning("override for an unknown series; skipped")
            return
        master = (
            (await self.conn.execute(select(events).where(events.c.id == master_entry[0]))).mappings().one()
        )
        key = incoming.original_key[:10] if master["start_date"] else incoming.original_key
        found = self.mapped.get(incoming.provider_id)
        if found is None:  # an EXDATE-derived row may already hold this occurrence
            by_key = await self.conn.scalar(
                select(events.c.id).where(events.c.series_id == master["id"], events.c.original_start == key)
            )
            found = (by_key, None) if by_key else None
        elif found[1] == incoming.etag and incoming.etag is not None:
            return
        if incoming.cancelled:
            values = self._cancelled_row(master, key)
        else:
            values = {
                **incoming.content,
                **(incoming.timing or self._occurrence_timing(master, key)),
                "series_id": master["id"],
                "original_start": key,
                "status": "confirmed",
            }
        event_id = await self._upsert(found[0] if found else None, values)
        await self._map(incoming.provider_id, event_id, incoming.etag)


async def apply_changes(
    conn: AsyncConnection, source: RowMapping, items: list[dict[str, Any]], *, full: bool
) -> Summary:
    return await _Apply(conn, source, full).run(items)
