from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from legal_study.supplemental_retrieval import (
    ObsidianArgumentPattern,
    SupplementalSearchAttempt,
)


def search_obsidian_argument_patterns(
    *,
    vault_dir: Path,
    queries: list[str],
    subject: str,
) -> tuple[list[ObsidianArgumentPattern], list[SupplementalSearchAttempt]]:
    """Search registered Obsidian argument-pattern notes without using source PDFs.

    Search is deliberately lexical and auditable. Query generation belongs to the
    caller; this function records every query and the pattern_ids it matched.
    """

    root = vault_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Obsidian vault directory is missing: {root}")

    cleaned_queries = [query.strip() for query in queries if query.strip()]
    if not cleaned_queries:
        raise ValueError("At least one non-empty Obsidian search query is required")

    patterns = _load_patterns(root=root, subject=subject)
    matched_by_id: dict[str, ObsidianArgumentPattern] = {}
    attempts: list[SupplementalSearchAttempt] = []

    for query in cleaned_queries:
        normalized_query = _normalize(query)
        matches = [
            pattern
            for pattern in patterns
            if _matches(pattern=pattern, normalized_query=normalized_query)
        ]
        matches.sort(key=lambda pattern: (_match_rank(pattern, normalized_query), pattern.pattern_id))
        matched_ids = [pattern.pattern_id for pattern in matches]
        attempts.append(
            SupplementalSearchAttempt(
                source="obsidian_argument_pattern",
                query=query,
                matched_ids=matched_ids,
            )
        )
        for pattern in matches:
            matched_by_id.setdefault(pattern.pattern_id, pattern)

    return list(matched_by_id.values()), attempts


def _load_patterns(*, root: Path, subject: str) -> list[ObsidianArgumentPattern]:
    by_id: dict[str, tuple[str, ObsidianArgumentPattern]] = {}

    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
            text = payload.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue

        frontmatter = _frontmatter(text)
        if frontmatter is None:
            continue
        if str(frontmatter.get("type", "")).strip() != "論証パターン":
            continue
        if str(frontmatter.get("subject", "")).strip() != subject:
            continue

        pattern_id = str(frontmatter.get("pattern_id", "")).strip()
        title = str(frontmatter.get("title", "")).strip()
        if not pattern_id or not title:
            continue

        body = _argument_pattern_section(text)
        if not body.strip():
            continue

        relative_path = path.resolve().relative_to(root).as_posix()
        sha256 = hashlib.sha256(payload).hexdigest()
        pattern = ObsidianArgumentPattern(
            pattern_id=pattern_id,
            aliases=_string_list(frontmatter.get("aliases")),
            title=title,
            body=body,
            related_statutes=_string_list(frontmatter.get("related_statutes")),
            source=_optional_string(frontmatter.get("source")),
            source_page=_source_page(frontmatter.get("source_page")),
            pdf_pages=_pdf_pages(frontmatter.get("pdf_pages")),
            source_path=relative_path,
            source_sha256=sha256,
        )

        existing = by_id.get(pattern_id)
        if existing is None:
            by_id[pattern_id] = (sha256, pattern)
            continue
        existing_sha, existing_pattern = existing
        if existing_sha != sha256:
            raise ValueError(
                "Conflicting duplicate Obsidian pattern_id: "
                f"{pattern_id} ({existing_pattern.source_path} vs {relative_path})"
            )

    return [item[1] for item in sorted(by_id.values(), key=lambda item: item[1].pattern_id)]


def _frontmatter(text: str) -> dict[str, Any] | None:
    normalized = text.lstrip("\ufeff")
    if not normalized.startswith("---\n"):
        return None
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        payload = yaml.safe_load(normalized[4:end]) or {}
    except yaml.YAMLError:
        return None
    return payload if isinstance(payload, dict) else None


def _argument_pattern_section(text: str) -> str:
    normalized = text.lstrip("\ufeff")
    lines = normalized.splitlines()
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        if line.strip() == "## 論証パターン":
            start = index + 1
            continue
        if start is not None and line.startswith("## "):
            end = index
            break
    if start is None:
        return ""
    return "\n".join(lines[start:end]).strip() + "\n"


def _matches(*, pattern: ObsidianArgumentPattern, normalized_query: str) -> bool:
    haystacks = [
        pattern.pattern_id,
        pattern.title,
        *pattern.aliases,
        pattern.body,
    ]
    return any(normalized_query in _normalize(value) for value in haystacks)


def _match_rank(pattern: ObsidianArgumentPattern, normalized_query: str) -> int:
    if _normalize(pattern.pattern_id) == normalized_query:
        return 0
    if any(_normalize(alias) == normalized_query for alias in pattern.aliases):
        return 1
    if normalized_query in _normalize(pattern.title):
        return 2
    if any(normalized_query in _normalize(alias) for alias in pattern.aliases):
        return 3
    return 4


def _normalize(value: str) -> str:
    return "".join(value.split()).casefold()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    cleaned = str(value).strip()
    return [cleaned] if cleaned else []


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _source_page(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    cleaned = str(value).strip()
    return cleaned or None


def _pdf_pages(value: Any) -> int | str | list[int] | list[str] | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        if all(isinstance(item, int) for item in value):
            return value
        return [str(item).strip() for item in value if str(item).strip()]
    cleaned = str(value).strip()
    return cleaned or None
