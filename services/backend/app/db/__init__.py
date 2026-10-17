"""SQLite engine, table definitions, and startup migration."""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
)

metadata = MetaData()

# Phase 0 only: proves write -> restart -> read. Replaced by the real domain in Phase 1.
notes = Table(
    "notes",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("body", Text, nullable=False),
    Column("created_at", String, nullable=False),  # ISO 8601 UTC
)


class SchemaTooNew(Exception):
    """The database was written by a newer Agenda; refuse to open it."""


def migrations_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)  # set inside a PyInstaller bundle
    if frozen_root:
        return Path(frozen_root) / "migrations"
    return Path(__file__).resolve().parents[2] / "migrations"


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", migrations_dir().as_posix())
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def current_revision(db_path: Path) -> str | None:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def migrate(db_path: Path, backups_dir: Path) -> None:
    """Bring the database to head, snapshotting first if it already has data."""
    cfg = alembic_config(db_path)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    existed = db_path.exists()
    current = current_revision(db_path)

    if current is not None:
        try:
            script.get_revision(current)
        except CommandError as exc:
            raise SchemaTooNew(f"database revision {current} is newer than this app ({head})") from exc
    if current == head:
        return

    if existed:
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot = backups_dir / f"pre-migration-{current or 'base'}-{stamp}.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(snapshot),))
        log.info("pre-migration snapshot written to %s", snapshot)

    log.info("migrating database %s -> %s", current, head)
    command.upgrade(cfg, "head")


def make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        for pragma in PRAGMAS:
            cursor.execute(pragma)
        cursor.close()

    return engine
