import sqlite3

from alembic import command

from app.db import alembic_config, current_revision, migrate
from tests.conftest import running


def test_fresh_database_migrates_to_head(settings):
    settings.data_dir.mkdir(parents=True)
    migrate(settings.db_path, settings.backups_dir)
    assert current_revision(settings.db_path) == "0003"
    assert not settings.backups_dir.exists()  # nothing to snapshot on first run


def test_existing_database_is_snapshotted_before_upgrade(settings):
    settings.data_dir.mkdir(parents=True)
    cfg = alembic_config(settings.db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # an existing database that is behind head
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("CREATE TABLE marker (x)")

    migrate(settings.db_path, settings.backups_dir)

    assert current_revision(settings.db_path) == "0003"
    [snapshot] = settings.backups_dir.glob("pre-migration-base-*.db")
    with sqlite3.connect(snapshot) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "marker" in tables and "notes" not in tables


def test_newer_schema_is_refused(settings):
    settings.data_dir.mkdir(parents=True)
    migrate(settings.db_path, settings.backups_dir)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE alembic_version SET version_num = '9999_future'")

    with running(settings, wait_ready=False) as client:
        import time

        deadline = time.monotonic() + 5
        while (res := client.get("/health/ready")).json()["error"]["code"] == "NOT_READY":
            assert time.monotonic() < deadline
            time.sleep(0.05)
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "SCHEMA_TOO_NEW"
