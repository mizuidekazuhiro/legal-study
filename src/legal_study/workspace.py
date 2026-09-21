from __future__ import annotations

import hashlib
from pathlib import Path

from legal_study.settings import LocalSettings


def source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_dir(*, subject: str, question: str, source: Path, settings: LocalSettings | None = None) -> Path:
    cfg = settings or LocalSettings()
    cfg.ensure()
    short_hash = source_fingerprint(source)[:12]
    safe_subject = subject.strip().replace("/", "-").replace("\\", "-")
    safe_question = question.strip().replace("/", "-").replace("\\", "-")
    path = cfg.runs_dir / safe_subject / f"{safe_question}-{short_hash}"
    path.mkdir(parents=True, exist_ok=True)
    return path
