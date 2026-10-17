"""The real stdin/stdout protocol the Tauri supervisor depends on (spec §04)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import TOKEN

BACKEND_DIR = Path(__file__).resolve().parents[1]


def spawn(data_dir: Path, secret: str = TOKEN) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main", "--data-dir", str(data_dir)],
        cwd=BACKEND_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    proc.stdin.write(secret + "\n")
    proc.stdin.flush()
    return proc


def wait_ready(port: int) -> None:
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health/ready").status_code == 200:
                return
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, "never became ready"
        time.sleep(0.1)


@pytest.fixture
def backend(tmp_path):
    proc = spawn(tmp_path)
    line = proc.stdout.readline().strip()
    assert line.startswith("AGENDA_READY port="), line
    port = int(line.removeprefix("AGENDA_READY port="))
    yield proc, port, tmp_path
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_announces_port_and_serves(backend):
    _, port, _ = backend
    wait_ready(port)
    res = httpx.get(
        f"http://127.0.0.1:{port}/api/v1/setup/status", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert res.json()["data"] == {"completed": False}


def test_shutdown_line_stops_gracefully(backend):
    proc, port, _ = backend
    wait_ready(port)
    proc.stdin.write("shutdown\n")
    proc.stdin.flush()
    assert proc.wait(timeout=10) == 0


def test_stdin_eof_means_parent_died(backend):
    proc, port, _ = backend
    wait_ready(port)
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0


def test_second_backend_on_same_data_dir_is_refused(backend):
    _, port, data_dir = backend
    wait_ready(port)
    second = spawn(data_dir)
    assert second.stdout.readline().strip() == "AGENDA_ERROR code=DB_LOCKED"
    assert second.wait(timeout=10) == 3


def test_short_secret_is_refused(tmp_path):
    proc = spawn(tmp_path, secret="short")
    assert proc.stdout.readline().strip() == "AGENDA_ERROR code=BAD_SECRET"
    assert proc.wait(timeout=10) == 2
