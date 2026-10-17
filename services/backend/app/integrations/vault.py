"""Where provider refresh tokens live (spec §06): the OS credential vault, never the database.

Windows Credential Manager, macOS Keychain, or the Linux Secret Service, through `keyring`.
With no vault, release builds refuse to connect accounts rather than store tokens in plaintext.
Dev builds (--dev) may use an owner-only file instead (ADR 0004), because WSL has no vault.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Protocol

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE = "Agenda"


class VaultUnavailable(Exception):
    pass


class Vault(Protocol):
    description: str

    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, secret: str) -> None: ...
    def delete(self, ref: str) -> None: ...


class OsVault:
    description = "OS credential vault"

    def get(self, ref: str) -> str | None:
        return keyring.get_password(SERVICE, ref)

    def set(self, ref: str, secret: str) -> None:
        try:
            keyring.set_password(SERVICE, ref, secret)
        except keyring.errors.KeyringError as exc:
            raise VaultUnavailable(str(exc)) from exc

    def delete(self, ref: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):  # already gone
            keyring.delete_password(SERVICE, ref)


class DevFileVault:
    """Development only. Plaintext in an owner-only file inside the dev data directory."""

    description = "dev file store (NOT a secure vault)"

    def __init__(self, path: Path):
        self.path = path
        log.warning("using %s for provider tokens; this is for development only", path)

    def _read(self) -> dict[str, str]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write(self, data: dict[str, str]) -> None:
        self.path.touch(mode=0o600, exist_ok=True)
        if os.name != "nt":
            self.path.chmod(0o600)
        self.path.write_text(json.dumps(data))

    def get(self, ref: str) -> str | None:
        return self._read().get(ref)

    def set(self, ref: str, secret: str) -> None:
        self._write({**self._read(), ref: secret})

    def delete(self, ref: str) -> None:
        data = self._read()
        data.pop(ref, None)
        self._write(data)


class MemoryVault:
    """Tests."""

    description = "memory"

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.secrets.get(ref)

    def set(self, ref: str, secret: str) -> None:
        self.secrets[ref] = secret

    def delete(self, ref: str) -> None:
        self.secrets.pop(ref, None)


def os_vault_available() -> bool:
    # keyring falls back to a "fail" backend (priority 0) when the OS offers nothing
    return keyring.get_keyring().priority > 0


def make_vault(data_dir: Path, *, dev: bool) -> Vault | None:
    if os_vault_available():
        return OsVault()
    if dev:
        return DevFileVault(data_dir / "dev-credentials.json")
    log.error("no OS credential vault; provider accounts cannot be connected")
    return None
