from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from legal_study.settings import LocalSettings


def source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_path_component(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", normalized).strip(" .")
    if not sanitized or sanitized in {".", ".."}:
        sanitized = fallback
    if sanitized.upper() in _WINDOWS_RESERVED_NAMES:
        sanitized = f"_{sanitized}"
    return sanitized[:80]


def run_dir(
    *,
    subject: str,
    question: str,
    source_sha256: str,
    input_hash: str,
    settings: LocalSettings | None = None,
) -> Path:
    cfg = settings or LocalSettings()
    cfg.ensure()
    safe_subject = safe_path_component(subject, fallback="unknown")
    safe_question = safe_path_component(question, fallback="adhoc")
    path = (
        cfg.runs_dir
        / safe_subject
        / f"{safe_question}-{source_sha256[:12]}-{input_hash[:12]}"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path
