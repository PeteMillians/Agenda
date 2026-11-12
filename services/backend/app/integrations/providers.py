"""The per-provider pieces the provider-neutral sync and write engines need (spec §10)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.integrations.base import Incoming
from app.integrations.google import mapping as google
from app.integrations.microsoft import mapping as microsoft


@dataclass(frozen=True)
class Codec:
    parse: Callable[[dict[str, Any], str], Incoming]
    to_provider: Callable[[dict[str, Any]], dict[str, Any]]
    client_key: Callable[[str, int], str]  # deterministic id that makes a retried insert harmless
    # Google keeps series as master + instance overrides, addressable by a derived id. Microsoft's
    # delta hands out occurrences only, so Outlook events never have Agenda exception rows.
    instance_id: Callable[[str, str], str] | None
    series: bool
    # the Agenda event id a provider item was created from, read back from the client key
    local_id: Callable[[dict[str, Any]], str | None]


CODECS = {
    "google": Codec(
        parse=google.parse,
        to_provider=google.to_google,
        client_key=lambda event_id, _revision: google.client_key(event_id),
        instance_id=google.instance_id,
        series=True,
        local_id=lambda item: google.local_id(item["id"]),
    ),
    "microsoft": Codec(
        parse=microsoft.parse,
        to_provider=microsoft.to_graph,
        client_key=microsoft.client_key,
        instance_id=None,
        series=False,
        local_id=lambda item: (item.get("transactionId") or "").partition(":")[0] or None,
    ),
}

LABELS = {"google": "Google", "microsoft": "Outlook"}
