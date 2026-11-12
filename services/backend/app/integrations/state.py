"""A calendar source's rows measured against the provider (spec §10).

Each mapped row keeps the snapshot both sides last agreed on. A row whose current snapshot
differs has a local change waiting to be pushed; that is the offline write queue, and it
survives restarts because it is just data. Conflicts compare snapshots too, so a new ETag on
unchanged content, or both sides making the same change, is never a conflict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import event_mappings, events, sync_conflicts
from app.integrations.model import Incoming, canonical, incoming_snapshot, row_snapshot

_PARENT = object()  # "work the parent out from the master's mapping"


@dataclass
class SourceState:
    rows: dict[str, RowMapping]  # every event row of the source, tombstones included
    mappings: dict[str, RowMapping]  # by event id
    conflicts: dict[str, RowMapping]  # open conflicts by event id
    window: tuple[date, date] | None = None  # occurrences the provider reports (None: all)
    children: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for event_id, row in self.rows.items():
            if row["series_id"]:
                self.children.setdefault(row["series_id"], []).append(event_id)

    @property
    def by_provider(self) -> dict[str, str]:
        return {m["provider_event_id"]: event_id for event_id, m in self.mappings.items()}

    def root(self, event_id: str) -> str:
        return self.rows[event_id]["series_id"] or event_id

    def series(self, root: str) -> list[str]:
        return [root, *self.children.get(root, [])]

    def frozen(self, event_id: str) -> bool:
        """A series with an open conflict is neither edited nor pushed until it is resolved."""
        return any(member in self.conflicts for member in self.series(self.root(event_id)))

    def parent(self, event_id: str) -> str | None:
        series_id = self.rows[event_id]["series_id"]
        mapping = self.mappings.get(series_id) if series_id else None
        return mapping["provider_event_id"] if mapping else None

    def exdates(self, master_id: str) -> list[str]:
        """Cancelled occurrences with no provider instance of their own: Google keeps them as
        EXDATE lines on the master, Microsoft as missing instances."""
        return sorted(
            self.rows[c]["original_start"]
            for c in self.children.get(master_id, [])
            if self.rows[c]["status"] == "cancelled"
            and self.rows[c]["deleted_at"] is None
            and c not in self.mappings
        )

    def snapshot(self, event_id: str, parent: Any = _PARENT) -> dict[str, Any]:
        row = self.rows[event_id]
        return row_snapshot(
            row,
            parent=self.parent(event_id) if parent is _PARENT else parent,
            exdates=self.exdates(event_id) if row["rrule"] else (),
        )

    def synced(self, event_id: str) -> dict[str, Any] | None:
        mapping = self.mappings.get(event_id)
        return json.loads(mapping["synced_snapshot"]) if mapping and mapping["synced_snapshot"] else None

    def is_dirty(self, event_id: str) -> bool:
        row, mapping = self.rows[event_id], self.mappings.get(event_id)
        if mapping is None:
            # new here, or deleted before it was ever pushed; a cancelled occurrence without an
            # instance of its own travels with its master instead
            return not (row["series_id"] and row["status"] == "cancelled" and row["deleted_at"] is None)
        if mapping["synced_snapshot"] is None:
            return False
        return canonical(self.snapshot(event_id)) != mapping["synced_snapshot"]

    def series_dirty(self, root: str) -> bool:
        return any(self.is_dirty(member) for member in self.series(root))

    def pending(self) -> int:
        """Rows with a change waiting to be sent (what the UI calls pending changes). Rows held
        by a conflict wait for a decision instead, so they are counted there."""
        return sum(
            1
            for event_id, row in self.rows.items()
            if self.is_dirty(event_id)
            and not (row["deleted_at"] and event_id not in self.mappings)
            and not self.frozen(event_id)
        )

    def remote_snapshot(self, event_id: str, incoming: Incoming | None) -> dict[str, Any]:
        """The provider's version of row `event_id`, comparable with `snapshot(event_id)`."""
        if incoming is None:
            return {"deleted": True}
        row = self.rows[event_id]
        if row["series_id"]:
            master = self.rows[row["series_id"]]
            key = incoming.original_key or row["original_start"]
            return incoming_snapshot(incoming, original_start=key[:10] if master["start_date"] else key)
        if incoming.rrule is not None:
            overridden = {
                self.rows[c]["original_start"] for c in self.children.get(event_id, []) if c in self.mappings
            }
            keys = {k[:10] if row["start_date"] else k for k in incoming.exdate_keys} - overridden
            if self.window is not None:  # outside the window only our own copy knows
                lo, hi = (d.isoformat() for d in self.window)
                keys |= {k for k in self.exdates(event_id) if not lo <= k[:10] < hi}
            incoming = replace(incoming, exdate_keys=tuple(sorted(keys)))
        return incoming_snapshot(incoming)


async def load_state(
    conn: AsyncConnection, source_id: str, window: tuple[date, date] | None = None
) -> SourceState:
    rows = await conn.execute(select(events).where(events.c.calendar_source_id == source_id))
    mappings = await conn.execute(
        select(event_mappings).where(event_mappings.c.calendar_source_id == source_id)
    )
    conflicts = await conn.execute(
        select(sync_conflicts).where(
            sync_conflicts.c.calendar_source_id == source_id, sync_conflicts.c.status == "open"
        )
    )
    return SourceState(
        rows={r["id"]: r for r in rows.mappings()},
        mappings={m["event_id"]: m for m in mappings.mappings()},
        conflicts={c["event_id"]: c for c in conflicts.mappings()},
        window=window,
    )
