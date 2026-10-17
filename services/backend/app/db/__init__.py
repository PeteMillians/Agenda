"""SQLite engine and startup migration. Tables live in app.db.tables."""

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
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
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


def checkpoint(db_path: Path) -> None:
    """Fold the WAL back into the main file so a clean shutdown leaves one self-contained file."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_pragmas(dbapi_conn, _record):
        # Take over transaction control from the sqlite3 driver so BEGIN IMMEDIATE is ours to emit.
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        for pragma in PRAGMAS:
            cursor.execute(pragma)
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin_immediate(conn):
        # Take the write lock up front: a deferred transaction that reads and then writes can
        # fail with SQLITE_BUSY immediately, ignoring busy_timeout. One process, short
        # transactions (spec §09), so serializing them costs nothing.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
