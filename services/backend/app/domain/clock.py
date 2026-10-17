"""One timestamp format everywhere, so ISO strings sort correctly as text."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def now_iso() -> str:
    return iso(now())


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def new_id() -> str:
    return str(uuid.uuid4())
