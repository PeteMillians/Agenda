from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.api import health
from app.api.errors import install_error_handlers
from app.api.v1 import router as v1_router
from app.config import Settings
from app.core.security import install_boundary
from app.db import SchemaTooNew, checkpoint, make_engine, migrate
from app.integrations.runtime import PUSH_JOB, SYNC_JOB, Integrations
from app.integrations.vault import Vault, make_vault
from app.jobs.worker import Worker

log = logging.getLogger(__name__)


async def prepare_database(app: FastAPI) -> None:
    """Runs after the socket is up so /health/ready can report 503 while migrating."""
    settings: Settings = app.state.settings
    try:
        await asyncio.to_thread(migrate, settings.db_path, settings.backups_dir)
        engine = make_engine(settings.db_path)
        async with engine.connect() as conn:  # opens the file and applies PRAGMAs (WAL is persistent)
            await conn.execute(text("SELECT 1"))
        vault = app.state.vault_override or make_vault(settings.data_dir, dev=settings.dev)
        integrations = Integrations(engine, app.state.http, vault)
        worker = Worker(
            engine,
            {SYNC_JOB: integrations.sync_calendar, PUSH_JOB: integrations.push_calendar},
            # an Agenda edit to a two-way calendar's event is sent to the provider (spec §10)
            {kind: [PUSH_JOB] for kind in ("event.created", "event.changed", "event.deleted")},
        )
        integrations.wake_worker = worker.wake
        app.state.integrations = integrations
        app.state.worker = worker
        app.state.engine = engine  # ready from here on
        worker.start()  # resumes queued work and reclaims leases left by a crash
        integrations.start_scheduler()  # first poll runs now: catch up on changes made while closed
        app.state.startup_error = None
        log.info("database ready")
    except SchemaTooNew as exc:
        log.error("%s", exc)
        app.state.startup_error = ("SCHEMA_TOO_NEW", str(exc))
    except Exception as exc:
        log.exception("database startup failed")
        app.state.startup_error = ("STARTUP_FAILED", f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = None
    app.state.worker = None
    app.state.integrations = None
    app.state.startup_error = None
    app.state.http = httpx.AsyncClient(transport=app.state.http_transport, timeout=30)
    task = asyncio.create_task(prepare_database(app))
    yield
    await task
    if app.state.integrations is not None:
        await app.state.integrations.stop()
    if app.state.worker is not None:
        await app.state.worker.stop()
    if app.state.engine is not None:
        await app.state.engine.dispose()
        # outside any transaction: SQLite refuses a checkpoint inside one
        await asyncio.to_thread(checkpoint, app.state.settings.db_path)
    await app.state.http.aclose()
    log.info("backend stopped")


def create_app(
    settings: Settings,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    vault: Vault | None = None,
) -> FastAPI:
    """`http_transport` and `vault` exist for tests (a fake Google, an in-memory vault)."""
    app = FastAPI(
        title="Agenda local API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.dev else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.http_transport = http_transport
    app.state.vault_override = vault
    install_error_handlers(app)
    install_boundary(app, settings)
    app.include_router(health.router)
    app.include_router(v1_router)
    return app
