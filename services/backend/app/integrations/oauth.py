"""OAuth 2.0 for a public desktop client: system browser, PKCE (S256), one-time state, loopback
redirect (spec §10). The client secret of a desktop app is not confidential and is not relied on."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field

STATE_TTL_SECONDS = 600


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass
class Pending:
    provider: str
    verifier: str
    redirect_uri: str
    created: float = field(default_factory=time.monotonic)


class PendingAuthorizations:
    """In memory on purpose: a flow that outlives the sidecar must simply start again."""

    def __init__(self) -> None:
        self._by_state: dict[str, Pending] = {}

    def start(self, provider: str, redirect_uri: str) -> tuple[str, str]:
        """Returns (state, code_challenge)."""
        self._expire()
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        self._by_state[state] = Pending(provider, verifier, redirect_uri)
        return state, challenge

    def take(self, state: str) -> Pending | None:
        """One-time: a state can complete at most one sign-in."""
        self._expire()
        return self._by_state.pop(state, None)

    def _expire(self) -> None:
        cutoff = time.monotonic() - STATE_TTL_SECONDS
        for key in [k for k, v in self._by_state.items() if v.created < cutoff]:
            del self._by_state[key]
