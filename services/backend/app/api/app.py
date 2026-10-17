from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api import health
from app.api.errors import install_error_handlers
from app.api.v1 import router as v1_router
from app.config import Settings
from app.core.security import install_boundary
from app.db import SchemaTooNew, checkpoint, make_engine, migrate
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
        app.state.engine = engine
        app.state.worker = Worker(engine)
        app.state.worker.start()  # resumes queued work and reclaims leases left by a crash
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
    app.state.startup_error = None
    task = asyncio.create_task(prepare_database(app))
    yield
    await task
    if app.state.worker is not None:
        await app.state.worker.stop()
    if app.state.engine is not None:
        await app.state.engine.dispose()
        # outside any transaction: SQLite refuses a checkpoint inside one
        await asyncio.to_thread(checkpoint, app.state.settings.db_path)
    log.info("backend stopped")


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="Agenda local API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.dev else None,
        redoc_url=None,
    )
    app.state.settings = settings
    install_error_handlers(app)
    install_boundary(app, settings)
    app.include_router(health.router)
    app.include_router(v1_router)
    return app
