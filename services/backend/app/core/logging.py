from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(logs_dir: Path, *, dev: bool) -> None:
    """Send logs to stderr and a rotating file. stdout is reserved for the protocol."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if dev else logging.INFO)
    for handler in (
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(logs_dir / "backend.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ):
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    # Logs every cursor call at DEBUG, which drowns everything else in dev.
    logging.getLogger("aiosqlite").setLevel(logging.INFO)
