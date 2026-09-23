from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeRevision:
    sha: str | None
    source: str
    dirty: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _run_git(*args: str) -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def resolve_code_revision() -> CodeRevision:
    """Resolve code provenance without making Git availability mandatory."""
    configured = os.environ.get("LEGAL_STUDY_GIT_COMMIT", "").strip()
    if configured:
        return CodeRevision(sha=configured, source="environment", dirty=None)

    sha = _run_git("rev-parse", "HEAD")
    if sha is None:
        return CodeRevision(sha=None, source="unavailable", dirty=None)

    status = _run_git("status", "--porcelain")
    return CodeRevision(
        sha=sha,
        source="git",
        dirty=bool(status) if status is not None else None,
    )
