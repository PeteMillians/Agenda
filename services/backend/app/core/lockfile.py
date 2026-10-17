"""Exclusive lock so only one backend ever opens a given database."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class LockHeld(Exception):
    pass


def acquire(path: Path) -> IO[str]:
    """Return an open handle holding the lock; closing it releases the lock."""
    handle = open(path, "a+")  # noqa: SIM115 - lifetime is the caller's
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise LockHeld(str(path)) from exc
    return handle
