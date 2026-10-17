from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings

TOKEN = "a" * 64
PORT = 50123
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_settings(data_dir: Path, *, dev: bool = False) -> Settings:
    return Settings(data_dir=data_dir, token=TOKEN, port=PORT, dev=dev)


@contextmanager
def running(settings: Settings, *, wait_ready: bool = True) -> Iterator[TestClient]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}") as client:
        if wait_ready:
            deadline = time.monotonic() + 10
            while client.get("/health/ready").status_code != 200:
                assert time.monotonic() < deadline, "backend never became ready"
                time.sleep(0.05)
        yield client


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with running(settings) as c:
        yield c
