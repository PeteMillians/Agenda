"""Sidecar process entry point.

Protocol with the Tauri shell (see docs/Agenda_Architecture.html §03):
  stdin  line 1        launch secret (hex), written by the parent
  stdin  "shutdown"    request a graceful stop; EOF means the parent died
  stdout AGENDA_READY port=<n>     socket is listening; poll /health/ready next
  stdout AGENDA_ERROR code=<CODE>  fatal startup error, process exits
Nothing else may be written to stdout. Logs go to stderr and a file.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from app.api.app import create_app
from app.config import Settings
from app.core import lockfile
from app.core.logging import configure_logging

log = logging.getLogger("agenda")

MIN_SECRET_CHARS = 32


def protocol(line: str) -> None:
    print(line, flush=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agenda-backend")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--dev", action="store_true", help="allow the Vite dev origin and API docs")
    return parser.parse_args(argv)


def watch_parent(server: uvicorn.Server) -> None:
    """Stop the server on a "shutdown" line or when stdin closes (parent gone)."""
    for line in sys.stdin:
        if line.strip() == "shutdown":
            log.info("shutdown requested by parent")
            break
    else:
        log.info("stdin closed; parent is gone")
    server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    token = sys.stdin.readline().strip()
    if len(token) < MIN_SECRET_CHARS:
        protocol("AGENDA_ERROR code=BAD_SECRET")
        return 2

    data_dir: Path = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        data_dir.chmod(0o700)
    configure_logging(data_dir / "logs", dev=args.dev)

    try:
        lock = lockfile.acquire(data_dir / "agenda.lock")
    except lockfile.LockHeld:
        log.error("another backend holds %s", data_dir / "agenda.lock")
        protocol("AGENDA_ERROR code=DB_LOCKED")
        return 3

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)  # queue early connections until uvicorn starts accepting
    port = sock.getsockname()[1]

    settings = Settings(data_dir=data_dir, token=token, port=port, dev=args.dev)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
            log_config=None,
            access_log=False,
        )
    )
    threading.Thread(target=watch_parent, args=(server,), daemon=True).start()

    log.info("listening on 127.0.0.1:%d", port)
    protocol(f"AGENDA_READY port={port}")
    try:
        server.run(sockets=[sock])
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
