from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def default_home() -> Path:
    override = os.environ.get("LEGAL_STUDY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".legal-study").resolve()


class LocalSettings(BaseModel):
    home: Path = Field(default_factory=default_home)

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def sources_dir(self) -> Path:
        return self.home / "sources" / "sha256"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def temp_dir(self) -> Path:
        return self.home / "tmp"

    @property
    def state_db(self) -> Path:
        return self.home / "state.sqlite3"

    def ensure(self) -> None:
        for path in (
            self.home,
            self.runs_dir,
            self.sources_dir,
            self.cache_dir,
            self.models_dir,
            self.temp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
