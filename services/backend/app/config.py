from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    token: str
    port: int
    dev: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agenda.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"
