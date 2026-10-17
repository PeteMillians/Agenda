"""Freeze the Python backend into the single executable Tauri bundles as a sidecar.

Run from the repo root:
    uv run --directory services/backend --group build python ../../packaging/build_sidecar.py

Produces apps/desktop/src-tauri/binaries/agenda-backend-<target-triple>[.exe], the name
`bundle.externalBin` expects, then smoke-tests it through the real stdin/stdout protocol.
PyInstaller cannot cross-compile, so this builds for the OS it runs on.
"""

from __future__ import annotations

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
        ]
    )
    return work / "dist" / f"{name}{EXE}"


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
        proc.stdin.write(secrets.token_hex(32) + "\n")
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

        proc.stdin.write("shutdown\n")
        proc.stdin.flush()
        code = proc.wait(timeout=20)
        if code != 0:
            sys.exit(f"smoke test: exit code {code} after shutdown")
        print(f"smoke test passed: ready in {cold_start:.1f}s, clean shutdown", file=sys.stderr)


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
