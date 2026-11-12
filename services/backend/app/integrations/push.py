"""Push: send local changes in a two-way calendar to the provider (spec §10). Provider-neutral.

Every row whose content differs from the snapshot both sides last agreed on is sent with that
snapshot's ETag as a precondition (If-Match). Network calls happen outside transactions; each
success is recorded in its own short write transaction, so an interrupted push resumes where it
stopped. A rejected precondition fetches the provider's version: if only its ETag moved, the
write is retried once with the new one; otherwise both versions become a conflict and neither
side is overwritten.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import event_mappings, events
from app.integrations.errors import Gone, PreconditionFailed, Unrepresentable
from app.integrations.model import CalendarProvider, Outgoing, Written, canonical
from app.integrations.state import SourceState, load_state
from app.integrations.sync import record_conflict

log = logging.getLogger(__name__)


@dataclass
class PushSummary:
    pushed: int = 0
    conflicts: int = 0
    unsupported: int = 0


class _Push:
    def __init__(
        self,
        engine: AsyncEngine,
        provider: CalendarProvider,
        token: str,
        source: RowMapping,
        state: SourceState,
    ):
        self.engine = engine
        self.provider = provider
        self.token = token
        self.source = source
        self.state = state
        self.summary = PushSummary()
        self.master_ids: dict[str, str] = {}  # local root id -> provider id, once it has one

    async def run(self) -> PushSummary:
        roots = sorted(
            (event_id for event_id, row in self.state.rows.items() if row["series_id"] is None),
            key=lambda event_id: self.state.rows[event_id]["created_at"],
        )
        for root in roots:
            if not self.state.frozen(root):
                await self._guarded(root, self._top)
        for root in roots:
            if root in self.master_ids and not self.state.frozen(root):
                for child in self.state.children.get(root, []):
                    await self._guarded(child, lambda event_id, root=root: self._child(root, event_id))
        return self.summary

    async def _guarded(self, event_id: str, step: Callable[[str], Awaitable[None]]) -> None:
        try:
            await step(event_id)
        except Unrepresentable as exc:
            # refused up front for edits made here; reaching this means a rule the provider
            # cannot store slipped through, so leave the row pending and say so
            self.summary.unsupported += 1
            log.warning("cannot push %s: %s", event_id, exc.reason)

    # ------------------------------------------------------------------ database

    async def _record(self, event_id: str, written: Written, snapshot: dict[str, Any]) -> None:
        values = {
            "provider_event_id": written.provider_id,
            "etag": written.etag,
            "synced_snapshot": canonical(snapshot),
            "last_synced_revision": self.state.rows[event_id]["revision"],
        }
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(event_mappings)
                .values(event_id=event_id, calendar_source_id=self.source["id"], **values)
                .on_conflict_do_update(index_elements=["event_id"], set_=values)
            )
        self.summary.pushed += 1

    async def _purge(self, event_id: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(delete(events).where(events.c.id == event_id))

    async def _conflict(
        self, event_id: str, remote: dict[str, Any], version: str | None, provider_id: str | None
    ) -> None:
        async with self.engine.begin() as conn:
            if await record_conflict(conn, self.state, event_id, remote, version, provider_id):
                self.summary.conflicts += 1

    async def _write(
        self, event_id: str, write: Callable[[str | None], Awaitable[Written]], snapshot: dict[str, Any]
    ) -> Written | None:
        """Run a conditional write against the row's mapping, settling a failed precondition."""
        mapping = self.state.mappings[event_id]
        etag = mapping["etag"]
        for attempt in (1, 2):
            try:
                written = await write(etag)
            except PreconditionFailed:
                remote = await self.provider.fetch(self.token, self.source, mapping["provider_event_id"])
            except Gone:
                remote = None
            else:
                await self._record(event_id, written, snapshot)
                return written
            remote_snapshot = self.state.remote_snapshot(event_id, remote)
            if remote is not None and attempt == 1 and remote_snapshot == self.state.synced(event_id):
                etag = remote.etag  # only the version tag moved; try again with it
                continue
            if remote is not None and remote_snapshot == snapshot:  # it already has our change
                await self._record(event_id, Written(mapping["provider_event_id"], remote.etag), snapshot)
                return None
            await self._conflict(
                event_id, remote_snapshot, remote.etag if remote else None, mapping["provider_event_id"]
            )
            return None
        return None

    # ------------------------------------------------------------------ single events and masters

    async def _top(self, event_id: str) -> None:
        row, mapping = self.state.rows[event_id], self.state.mappings.get(event_id)
        if row["deleted_at"] is not None:
            if mapping is not None:

                async def remove(etag: str | None) -> Written:
                    await self.provider.delete(self.token, self.source, mapping["provider_event_id"], etag)
                    return Written(mapping["provider_event_id"], None)

                if await self._write(event_id, remove, {"deleted": True}) is None:
                    return
            await self._purge(event_id)  # its occurrences go with it
            return
        out = Outgoing.of(row, self.state.exdates(event_id))
        snapshot = self.state.snapshot(event_id, parent=None)
        if mapping is None:
            written = await self.provider.insert(self.token, self.source, out)
            await self._record(event_id, written, snapshot)
            self.master_ids[event_id] = written.provider_id
            return
        self.master_ids[event_id] = mapping["provider_event_id"]
        if not self.state.is_dirty(event_id):
            return
        await self._write(
            event_id,
            lambda etag: self.provider.update(
                self.token, self.source, mapping["provider_event_id"], etag, out
            ),
            snapshot,
        )

    # ------------------------------------------------------------------ occurrences

    async def _child(self, root: str, event_id: str) -> None:
        row, mapping = self.state.rows[event_id], self.state.mappings.get(event_id)
        master_id = self.master_ids[root]
        if row["deleted_at"] is not None:
            # dropped with a series change ("this and following", a new rule): the provider
            # already removed the occurrence, so this only tidies up
            if mapping is not None:
                await self.provider.delete(self.token, self.source, mapping["provider_event_id"], None)
            await self._purge(event_id)
            return
        master = Outgoing.of(self.state.rows[root], self.state.exdates(root))
        out = Outgoing.of(row)
        snapshot = self.state.snapshot(event_id, parent=master_id)
        if mapping is None:
            if row["status"] == "cancelled":
                return  # carried by the master
            written = await self._fresh_instance(event_id, master_id, master, out)
            if written:
                await self._record(event_id, written, snapshot)
            return
        if mapping["synced_snapshot"] is None or canonical(snapshot) == mapping["synced_snapshot"]:
            return
        synced = self.state.synced(event_id) or {}
        if synced.get("parent") != master_id or synced.get("original_start") != row["original_start"]:
            # moved to another series (a split) or re-keyed: a different provider occurrence now
            written = await self._fresh_instance(event_id, master_id, master, out)
            if written:
                await self._record(event_id, written, snapshot)
            return
        await self._write(
            event_id,
            lambda etag: self.provider.write_instance(
                self.token, self.source, master_id, master, out, mapping["provider_event_id"], etag
            ),
            snapshot,
        )

    async def _fresh_instance(
        self, event_id: str, master_id: str, master: Outgoing, out: Outgoing
    ) -> Written | None:
        try:
            written = await self.provider.write_instance(
                self.token, self.source, master_id, master, out, None, None
            )
        except Gone:  # the provider has no such occurrence any more
            await self._conflict(event_id, {"deleted": True}, None, None)
            return None
        return written if written.provider_id else None


async def push(
    engine: AsyncEngine, provider: CalendarProvider, token: str, source: RowMapping
) -> PushSummary:
    async with engine.connect() as conn:
        state = await load_state(conn, source["id"], provider.instance_window())
    if not any(state.is_dirty(event_id) for event_id in state.rows):
        return PushSummary()
    return await _Push(engine, provider, token, source, state).run()
