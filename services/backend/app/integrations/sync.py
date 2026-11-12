"""Pull: apply a provider's changes to a calendar source (spec §10). Provider-neutral.

Fetching happens first, outside any transaction; this applies the changes in one short write
transaction: masters and single events before instance overrides, because providers do not
order them. A full listing reconciles by event_mappings, so it never duplicates and it removes
anything the provider no longer has.

A row with a local change still waiting to be pushed is never overwritten. If the provider
changed it too, both versions are kept as a conflict for the owner to decide (spec §10).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.events import Timing, is_occurrence
from app.db.tables import event_mappings, events, sync_conflicts
from app.domain.clock import new_id, now_iso
from app.integrations.model import Changes, Incoming, canonical
from app.integrations.state import SourceState, load_state

log = logging.getLogger(__name__)


@dataclass
class Summary:
    full: bool
    created: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    conflicts: int = 0


# --------------------------------------------------------------------------- shared row helpers


def occurrence_timing(master: RowMapping, key: str) -> dict[str, str | None]:
    first = Timing.of(master)
    start: date | datetime = date.fromisoformat(key[:10]) if first.all_day else datetime.fromisoformat(key)
    return first.starting(start).columns()


def cancelled_row(master: RowMapping, key: str) -> dict[str, Any]:
    return {
        "title": master["title"],
        "description": "",
        "location": "",
        "timezone": master["timezone"],
        **occurrence_timing(master, key),
        "series_id": master["id"],
        "original_start": key,
        "status": "cancelled",
    }


async def record_conflict(
    conn: AsyncConnection,
    state: SourceState,
    event_id: str,
    remote: dict[str, Any],
    remote_version: str | None,
    provider_id: str | None,
) -> bool:
    """Keep both versions for the owner to choose between. Returns False if this exact remote
    version is already waiting; a newer remote version supersedes the older conflict."""
    current = state.conflicts.get(event_id)
    if current is not None:
        if (
            current["remote_version"] == remote_version
            and json.loads(current["remote_snapshot"]).get("provider_id") == provider_id
        ):
            return False
        await conn.execute(
            update(sync_conflicts).where(sync_conflicts.c.id == current["id"]).values(status="superseded")
        )
    row = state.rows[event_id]
    await conn.execute(
        insert(sync_conflicts).values(
            id=new_id(),
            event_id=event_id,
            calendar_source_id=row["calendar_source_id"],
            local_snapshot=canonical(state.snapshot(event_id)),
            remote_snapshot=canonical({**remote, "provider_id": provider_id}),
            local_revision=row["revision"],
            remote_version=remote_version,
            detected_at=now_iso(),
        )
    )
    log.info("sync conflict on %s", event_id)
    return True


# --------------------------------------------------------------------------- pull


class _Pull:
    def __init__(self, conn: AsyncConnection, source: RowMapping, changes: Changes, state: SourceState):
        self.conn = conn
        self.source = source
        self.changes = changes
        self.state = state
        self.summary = Summary(full=changes.full, skipped=changes.skipped)
        self.mapped: dict[str, tuple[str, str | None]] = {
            m["provider_event_id"]: (event_id, m["etag"]) for event_id, m in state.mappings.items()
        }
        self.seen: set[str] = set()
        self.touched: set[str] = set()
        self.dirty = {event_id for event_id in state.rows if state.is_dirty(event_id)}

    async def run(self) -> Summary:
        for provider_id in self.changes.dropped:
            await self._remove(provider_id)
        for incoming in self.changes.items:
            if not incoming.is_instance:
                await self._apply_top(incoming)
        for incoming in self.changes.items:
            if incoming.is_instance:
                await self._apply_instance(incoming)
        if self.changes.full:
            for provider_id in set(self.mapped) - self.seen:
                if not self._outside_window(provider_id):
                    await self._remove(provider_id)
        await self._settle()
        return self.summary

    # ------------------------------------------------------------------ helpers

    def _outside_window(self, provider_id: str) -> bool:
        """An override the listing could not have reported, because it is outside its window."""
        window = self.changes.window
        event_id = self.mapped[provider_id][0]
        row = self.state.rows.get(event_id)
        if window is None or row is None or not row["series_id"]:
            return False
        lo, hi = (d.isoformat() for d in window)
        return not lo <= row["original_start"][:10] < hi

    def _series_ids(self, event_id: str) -> list[str]:
        return self.state.series(self.state.root(event_id)) if event_id in self.state.rows else [event_id]

    async def _contested(self, event_id: str, incoming: Incoming | None, *, whole_series: bool) -> bool:
        """True if the row holds local changes, so the provider's version must not be applied.
        Records a conflict when the provider changed it too; adopts the new ETag when it did not."""
        if event_id not in self.state.rows:
            return False
        members = self._series_ids(event_id) if whole_series else [event_id]
        if not any(member in self.dirty for member in members):
            return False
        remote = self.state.remote_snapshot(event_id, incoming)
        synced = self.state.synced(event_id)
        mapping = self.state.mappings.get(event_id)
        if remote == synced or remote == self.state.snapshot(event_id):
            # the provider has nothing new, or already has exactly our change
            if mapping is not None and incoming is not None and incoming.etag != mapping["etag"]:
                await self.conn.execute(
                    update(event_mappings)
                    .where(event_mappings.c.event_id == event_id)
                    .values(
                        etag=incoming.etag,
                        **({} if remote == synced else {"synced_snapshot": canonical(remote)}),
                    )
                )
            return True
        if await record_conflict(
            self.conn,
            self.state,
            event_id,
            remote,
            incoming.etag if incoming else None,
            incoming.provider_id if incoming else None,
        ):
            self.summary.conflicts += 1
        return True

    def _unchanged(self, event_id: str, incoming: Incoming) -> bool:
        mapping = self.state.mappings.get(event_id)
        if mapping is None or event_id not in self.state.rows:
            return False
        if mapping["synced_snapshot"] is not None:
            return self.state.remote_snapshot(event_id, incoming) == self.state.synced(event_id)
        return incoming.etag is not None and incoming.etag == mapping["etag"]

    async def _adopt_etag(self, event_id: str, etag: str | None) -> None:
        if self.state.mappings[event_id]["etag"] != etag:
            await self.conn.execute(
                update(event_mappings).where(event_mappings.c.event_id == event_id).values(etag=etag)
            )

    async def _remove(self, provider_id: str) -> None:
        self.seen.add(provider_id)
        found = self.mapped.get(provider_id)
        if not found:
            return
        event_id = found[0]
        row = self.state.rows.get(event_id)
        if row is not None and row["deleted_at"] is not None:
            pass  # deleted on both sides: nothing to decide
        elif await self._contested(event_id, None, whole_series=row is not None and not row["series_id"]):
            return
        self.mapped.pop(provider_id)
        # imported rows are a cache of the provider: delete outright (exceptions, mappings, and
        # conflicts cascade)
        await self.conn.execute(delete(events).where(events.c.id == event_id))
        self.summary.removed += 1

    async def _map(self, provider_id: str, event_id: str, etag: str | None) -> None:
        existed = provider_id in self.mapped
        self.mapped[provider_id] = (event_id, etag)
        self.touched.add(event_id)
        if existed:
            await self.conn.execute(
                update(event_mappings)
                .where(event_mappings.c.event_id == event_id)
                .values(etag=etag, provider_event_id=provider_id)
            )
        else:
            await self.conn.execute(
                insert(event_mappings).values(
                    event_id=event_id,
                    calendar_source_id=self.source["id"],
                    provider_event_id=provider_id,
                    etag=etag,
                    last_synced_revision=0,  # set by _settle
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
            self.touched.add(event_id)
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
        self.touched.add(event_id)
        return event_id

    # ------------------------------------------------------------------ masters and singles

    async def _apply_top(self, incoming: Incoming) -> None:
        self.seen.add(incoming.provider_id)
        if incoming.cancelled:
            await self._remove(incoming.provider_id)
            return
        found = self.mapped.get(incoming.provider_id)
        if found:
            event_id = found[0]
            # turning a series into a single event would drop local edits to its occurrences
            if await self._contested(event_id, incoming, whole_series=incoming.rrule is None):
                return
            if self._unchanged(event_id, incoming):
                await self._adopt_etag(event_id, incoming.etag)
                return
        event_id = await self._upsert(
            found[0] if found else None,
            {**incoming.content, **(incoming.timing or {}), "rrule": incoming.rrule, "status": "confirmed"},
        )
        await self._map(incoming.provider_id, event_id, incoming.etag)
        await self._reset_derived_exceptions(event_id, incoming)

    async def _reset_derived_exceptions(self, master_id: str, incoming: Incoming) -> None:
        """Cancelled occurrences without an instance of their own (Google EXDATE lines, Microsoft
        missing instances) are rebuilt whenever the master changes. Mapped overrides, and local
        edits not pushed yet, are left alone."""
        rows = await self.conn.execute(
            select(events.c.id, events.c.original_start, events.c.status).where(
                events.c.series_id == master_id
            )
        )
        existing = list(rows)
        if incoming.rrule is None:  # no longer a series: nothing can hang off it
            await self.conn.execute(delete(events).where(events.c.series_id == master_id))
            return
        mapped_ids = {event_id for event_id, _ in self.mapped.values()}
        window = self.changes.window
        lo, hi = (d.isoformat() for d in window) if window else ("", "~")
        derived = [
            r.id
            for r in existing
            if r.id not in mapped_ids and r.status == "cancelled" and lo <= r.original_start[:10] < hi
        ]
        if derived:
            await self.conn.execute(delete(events).where(events.c.id.in_(derived)))
        kept = {r.original_start for r in existing if r.id not in derived}
        master = (await self.conn.execute(select(events).where(events.c.id == master_id))).mappings().one()
        for key in incoming.exdate_keys:
            if key not in kept:
                await self._upsert(None, cancelled_row(master, key))
        self.touched.add(master_id)

    # ------------------------------------------------------------------ instance overrides

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
        if found is None:  # a derived row, or a local edit not pushed yet, may hold this occurrence
            by_key = await self.conn.scalar(
                select(events.c.id).where(events.c.series_id == master["id"], events.c.original_start == key)
            )
            found = (by_key, None) if by_key else None
        if master["rrule"] is None or not is_occurrence(master, key):
            # the series no longer has this occurrence (e.g. after it was split); the provider
            # reports what became of the old instance, which there is nothing to attach to
            if found and incoming.provider_id in self.mapped:
                await self._remove(incoming.provider_id)
            return
        if found:
            if await self._contested(found[0], incoming, whole_series=False):
                return
            if found[0] in self.state.mappings and self._unchanged(found[0], incoming):
                await self._adopt_etag(found[0], incoming.etag)
                return
        if incoming.cancelled:
            values = cancelled_row(master, key)
        else:
            values = {
                **incoming.content,
                **(incoming.timing or occurrence_timing(master, key)),
                "series_id": master["id"],
                "original_start": key,
                "status": "confirmed",
            }
        event_id = await self._upsert(found[0] if found else None, values)
        await self._map(incoming.provider_id, event_id, incoming.etag)
        self.touched.add(master["id"])

    # ------------------------------------------------------------------ baseline

    async def _settle(self) -> None:
        """Record what both sides now agree on for every row this pull wrote, unless the row
        (or, for a master, its derived occurrences) carries a local change."""
        if not self.touched:
            return
        after = await load_state(self.conn, self.source["id"], self.changes.window)
        for event_id in self.touched:
            if event_id not in after.mappings or event_id in self.dirty:
                continue
            await self.conn.execute(
                update(event_mappings)
                .where(event_mappings.c.event_id == event_id)
                .values(
                    synced_snapshot=canonical(after.snapshot(event_id)),
                    last_synced_revision=after.rows[event_id]["revision"],
                )
            )


async def apply_changes(conn: AsyncConnection, source: RowMapping, changes: Changes) -> Summary:
    state = await load_state(conn, source["id"], changes.window)
    return await _Pull(conn, source, changes, state).run()


async def baseline(conn: AsyncConnection, source_id: str) -> None:
    """Treat every row of a source as in step with the provider, e.g. when two-way sync is switched
    on for a calendar that until now could not be edited here."""
    state = await load_state(conn, source_id)
    for event_id in state.mappings:
        await conn.execute(
            update(event_mappings)
            .where(event_mappings.c.event_id == event_id)
            .values(
                synced_snapshot=canonical(state.snapshot(event_id)),
                last_synced_revision=state.rows[event_id]["revision"],
            )
        )
