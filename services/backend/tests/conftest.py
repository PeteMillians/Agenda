from __future__ import annotations

import functools
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings
from app.db import migrate

TOKEN = "a" * 64
PORT = 50123
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_settings(data_dir: Path, *, dev: bool = False) -> Settings:
    return Settings(data_dir=data_dir, token=TOKEN, port=PORT, dev=dev)


@functools.cache
def _migrated_template() -> Path:
    """Migrate once per session; each test starts from a copy (startup then finds nothing to do)."""
    path = Path(tempfile.mkdtemp()) / "template.db"
    migrate(path, path.parent / "backups")
    return path


@contextmanager
def running(settings: Settings, *, wait_ready: bool = True, **app_options: Any) -> Iterator[TestClient]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not settings.db_path.exists():
        shutil.copy(_migrated_template(), settings.db_path)
    with TestClient(create_app(settings, **app_options), base_url=f"http://127.0.0.1:{PORT}") as client:
        if wait_ready:
            deadline = time.monotonic() + 10
            while client.get("/health/ready").status_code != 200:
                assert time.monotonic() < deadline, "backend never became ready"
                time.sleep(0.01)
        yield client


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with running(settings) as c:
        yield c


def complete_setup(client: TestClient, timezone: str = "America/New_York") -> dict[str, dict]:
    """Run first-time setup; return the default spaces keyed by name."""
    res = client.post("/api/v1/setup", json={"display_name": "Pete", "timezone": timezone}, headers=AUTH)
    assert res.status_code == 201, res.text
    spaces = client.get("/api/v1/spaces", headers=AUTH).json()["data"]
    return {space["name"]: space for space in spaces}


@pytest.fixture
def spaces(client: TestClient) -> dict[str, dict]:
    return complete_setup(client)


def rev(revision: int) -> dict[str, str]:
    return {**AUTH, "If-Match": str(revision)}
