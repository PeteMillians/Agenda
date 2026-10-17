"""Long-lived integration state: HTTP client, vault, pending sign-ins, access tokens, and the
calendar sync job and poll scheduler (spec §09, §10)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import app_settings, calendar_sources, external_accounts
from app.domain.clock import iso, now, now_iso
from app.integrations.google.client import (
    AccessExpired,
    AuthRevoked,
    GoogleClient,
    Grant,
    ProviderError,
    SyncTokenExpired,
)
from app.integrations.google.sync import apply_changes
from app.integrations.oauth import PendingAuthorizations
from app.integrations.vault import Vault
from app.jobs import queue
from app.jobs.queue import JobError

log = logging.getLogger(__name__)

GOOGLE_CLIENT_SETTING = "google.oauth_client"
POLL_EVERY = timedelta(minutes=5)
SCHEDULER_TICK_SECONDS = 60
SYNC_JOB = "calendar.sync"


class MissingClient(Exception):
    pass


class Integrations:
    def __init__(self, engine: AsyncEngine, http: httpx.AsyncClient, vault: Vault | None):
        self.engine = engine
        self.http = http
        self.vault = vault
        self.pending = PendingAuthorizations()
        self._tokens: dict[str, Grant] = {}
        self._scheduler: asyncio.Task[None] | None = None
        self.wake_worker = lambda: None  # set by the app once the worker exists

    # ------------------------------------------------------------------ provider client

    async def google_client(self) -> GoogleClient:
        async with self.engine.connect() as conn:
            raw = await conn.scalar(
                select(app_settings.c.value).where(app_settings.c.key == GOOGLE_CLIENT_SETTING)
            )
        if raw is None:
            raise MissingClient()
        config = json.loads(raw)
        return GoogleClient(self.http, config["client_id"], config["client_secret"])

    async def access_token(self, account: RowMapping, *, force: bool = False) -> str:
        cached = self._tokens.get(account["id"])
        if cached and not force and cached.expires_at - now() > timedelta(minutes=1):
            return cached.access_token
        refresh = self.vault.get(account["credential_ref"]) if self.vault else None
        if not refresh:
            raise AuthRevoked()
        grant = await (await self.google_client()).refresh(refresh)
        if grant.refresh_token and grant.refresh_token != refresh and self.vault:
            self.vault.set(account["credential_ref"], grant.refresh_token)  # rotated
        self._tokens[account["id"]] = grant
        return grant.access_token

    def remember(self, account_id: str, grant: Grant) -> None:
        self._tokens[account_id] = grant

    def forget(self, account_id: str) -> None:
        self._tokens.pop(account_id, None)

    # ------------------------------------------------------------------ the sync job

    async def sync_calendar(self, payload: dict[str, Any]) -> None:
        """Job handler for `calendar.sync`. Idempotent: replaying it re-reads from the cursor."""
        source_id = payload["calendar_source_id"]
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(calendar_sources, external_accounts.c.status.label("account_status"))
                        .join(
                            external_accounts,
                            external_accounts.c.id == calendar_sources.c.external_account_id,
                        )
                        .where(calendar_sources.c.id == source_id)
                    )
                )
                .mappings()
                .first()
            )
            account = (
                (
                    await conn.execute(
                        select(external_accounts).where(external_accounts.c.id == row["external_account_id"])
                    )
                )
                .mappings()
                .first()
                if row
                else None
            )
        if row is None or account is None or not row["enabled"] or row["space_id"] is None:
            return  # detached or disabled since the job was queued
        if row["account_status"] == "needs_reauth":
            await self._mark(source_id, "needs_reauth", "needs_reauth")
            return
        try:
            client = await self.google_client()
            token = await self.access_token(account)
            cursor = row["sync_cursor"]
            try:
                items, next_cursor = await client.list_events(token, row["provider_calendar_id"], cursor)
            except AccessExpired:
                token = await self.access_token(account, force=True)
                items, next_cursor = await client.list_events(token, row["provider_calendar_id"], cursor)
            except SyncTokenExpired:
                log.info("sync token expired for %s; running a full sync", source_id)
                cursor = None
                items, next_cursor = await client.list_events(token, row["provider_calendar_id"], None)
            async with self.engine.begin() as conn:
                summary = await apply_changes(conn, row, items, full=cursor is None)
                await conn.execute(
                    update(calendar_sources)
                    .where(calendar_sources.c.id == source_id)
                    .values(
                        sync_cursor=next_cursor,
                        sync_state="ok",
                        last_error=None,
                        last_synced_at=now_iso(),
                        updated_at=now_iso(),
                    )
                )
            log.info("synced %s: %s", source_id, summary)
        except MissingClient as exc:
            await self._mark(source_id, "error", "oauth_client_missing")
            raise JobError("oauth_client_missing", retryable=False) from exc
        except AuthRevoked as exc:
            await self._needs_reauth(account["id"])
            raise JobError("needs_reauth", retryable=False) from exc
        except ProviderError as exc:
            await self._mark(source_id, "error", exc.category)
            raise JobError(exc.category, retryable=exc.retryable, retry_after=exc.retry_after) from exc

    async def _mark(self, source_id: str, state: str, error: str | None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(calendar_sources)
                .where(calendar_sources.c.id == source_id)
                .values(sync_state=state, last_error=error, updated_at=now_iso())
            )

    async def _needs_reauth(self, account_id: str) -> None:
        """Stop retrying a dead refresh token; the UI asks the user to reconnect (spec §10)."""
        self.forget(account_id)
        async with self.engine.begin() as conn:
            await conn.execute(
                update(external_accounts)
                .where(external_accounts.c.id == account_id)
                .values(
                    status="needs_reauth", revision=external_accounts.c.revision + 1, updated_at=now_iso()
                )
            )
            await conn.execute(
                update(calendar_sources)
                .where(calendar_sources.c.external_account_id == account_id)
                .values(sync_state="needs_reauth", last_error="needs_reauth", updated_at=now_iso())
            )

    # ------------------------------------------------------------------ scheduling

    async def enqueue_sync(self, source_id: str, *, dedupe: str) -> str:
        async with self.engine.begin() as conn:
            job_id = await queue.enqueue(
                conn, SYNC_JOB, {"calendar_source_id": source_id}, idempotency_key=f"{source_id}:{dedupe}"
            )
        self.wake_worker()
        return job_id

    async def enqueue_due(self, *, force: bool = False) -> list[str]:
        """Queue a poll for each enabled calendar not synced within POLL_EVERY (or all, if forced).
        The idempotency key is the time slot, so overlapping triggers queue one job."""
        cutoff = iso(now() - POLL_EVERY)
        query = (
            select(calendar_sources.c.id)
            .join(external_accounts, external_accounts.c.id == calendar_sources.c.external_account_id)
            .where(
                calendar_sources.c.enabled.is_(True),
                calendar_sources.c.space_id.is_not(None),
                external_accounts.c.status == "active",
            )
        )
        if not force:
            query = query.where(
                or_(calendar_sources.c.last_synced_at.is_(None), calendar_sources.c.last_synced_at < cutoff)
            )
        async with self.engine.connect() as conn:
            ids = [r.id for r in await conn.execute(query)]
        slot = int(now().timestamp() // (60 if force else POLL_EVERY.total_seconds()))
        return [
            await self.enqueue_sync(source_id, dedupe=f"{'now' if force else 'poll'}:{slot}")
            for source_id in ids
        ]

    def start_scheduler(self) -> None:
        async def loop() -> None:
            while True:
                try:
                    await (
                        self.enqueue_due()
                    )  # first pass runs at launch: catch up on changes made while closed
                except Exception:
                    log.exception("calendar poll scheduling failed")
                await asyncio.sleep(SCHEDULER_TICK_SECONDS)

        self._scheduler = asyncio.create_task(loop(), name="agenda-calendar-poll")

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
