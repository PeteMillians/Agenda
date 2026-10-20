"""Freeze the Python backend into the single executable Tauri bundles as a sidecar.

Run from the repo root:
    uv run --directory services/backend --group build python ../../packaging/build_sidecar.py

Produces apps/desktop/src-tauri/binaries/agenda-backend-<target-triple>[.exe], the name
`bundle.externalBin` expects, then smoke-tests it through the real stdin/stdout protocol.
PyInstaller cannot cross-compile, so this builds for the OS it runs on.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "services" / "backend"
OUT_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"
EXE = ".exe" if os.name == "nt" else ""


def target_triple() -> str:
    result = subprocess.run(["rustc", "--print", "host-tuple"], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def build(name: str, work: Path) -> Path:
    PyInstaller.__main__.run(
        [
            str(ROOT / "packaging" / "sidecar_entry.py"),
            "--onefile",
            "--noconfirm",
            "--clean",
            "--log-level=WARN",
            f"--name={name}",
            f"--distpath={work / 'dist'}",
            f"--workpath={work / 'build'}",
            f"--specpath={work}",
            f"--paths={BACKEND}",
            f"--add-data={BACKEND / 'migrations'}{os.pathsep}migrations",
            # imported by string at runtime, invisible to PyInstaller's analysis
            "--collect-submodules=uvicorn",
            "--collect-submodules=alembic",
            "--collect-submodules=sqlalchemy.dialects.sqlite",
            "--hidden-import=aiosqlite",
            # zoneinfo has no system database on Windows; it reads the tzdata package's files
            "--collect-data=tzdata",
            # keyring finds OS vault backends through package metadata; without it the frozen
            # app would silently have no vault
            "--copy-metadata=keyring",
            "--collect-submodules=keyring.backends",
        ]
    )
    return work / "dist" / f"{name}{EXE}"


def exercise_api(port: int, token: str) -> None:
    """Paths that need bundled data: migrations ran (ready), time zones (setup), recurrence (event)."""
    base = f"http://127.0.0.1:{port}/api/v1"
    headers = {"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}

    def send(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request) as res:
            return json.loads(res.read())

    send("POST", "/setup", {"display_name": "Smoke", "timezone": "America/New_York"})
    space = send("GET", "/spaces")["data"][0]["id"]
    send(
        "POST",
        f"/spaces/{space}/events",
        {
            "title": "DST check",
            "start_at": "2026-10-20T13:00:00Z",
            "end_at": "2026-10-20T14:00:00Z",
            "rrule": "FREQ=WEEKLY;BYDAY=TU",
        },
    )
    hours = {
        o["start_at"][11:13] for o in send("GET", f"/spaces/{space}/events?from=2026-10-19&to=2026-11-16")["data"]
    }
    if hours != {"13", "14"}:  # 09:00 New York on both sides of the DST change
        sys.exit(f"smoke test: time zone expansion is wrong: {sorted(hours)}")
    vault = send("GET", "/integrations/google/client")["data"]["vault"]
    if os.name == "nt" and vault != "OS credential vault":
        sys.exit(f"smoke test: Windows Credential Manager not found by the frozen app (vault={vault})")
    print(f"smoke test: vault = {vault}", file=sys.stderr)


def smoke_test(binary: Path) -> None:
    """Handshake, health, and graceful shutdown against the frozen binary."""
    with tempfile.TemporaryDirectory() as data_dir:
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(binary), "--data-dir", data_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin and proc.stdout
        token = secrets.token_hex(32)
        proc.stdin.write(token + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline().strip()
        if not line.startswith("AGENDA_READY port="):
            proc.kill()
            sys.exit(f"smoke test: expected AGENDA_READY, got {line!r}")
        port = int(line.removeprefix("AGENDA_READY port="))

        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready") as res:
                    if res.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError):
                pass
            if time.monotonic() > deadline:
                proc.kill()
                sys.exit("smoke test: /health/ready never returned 200")
            time.sleep(0.2)
        cold_start = time.monotonic() - started
        exercise_api(port, token)

        proc.stdin.write("shutdown\n")
        proc.stdin.flush()
        code = proc.wait(timeout=20)
        if code != 0:
            sys.exit(f"smoke test: exit code {code} after shutdown")
        print(f"smoke test passed: ready in {cold_start:.1f}s, setup + DST expansion ok, clean shutdown", file=sys.stderr)


def main() -> None:
    name = f"agenda-backend-{target_triple()}"
    with tempfile.TemporaryDirectory() as work:
        binary = build(name, Path(work))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        target = OUT_DIR / binary.name
        shutil.copy2(binary, target)
    size_mb = target.stat().st_size / 1_000_000
    print(f"built {target.relative_to(ROOT)} ({size_mb:.1f} MB)", file=sys.stderr)
    smoke_test(target)


if __name__ == "__main__":
    main()
