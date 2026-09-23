from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictSupplementalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplementalSearchAttempt(StrictSupplementalModel):
    source: Literal["obsidian_argument_pattern", "notion_statute"]
    query: str = Field(min_length=1)
    matched_ids: list[str] = Field(default_factory=list)


class ObsidianArgumentPattern(StrictSupplementalModel):
    source_kind: Literal["obsidian_argument_pattern"] = "obsidian_argument_pattern"
    pattern_id: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    related_statutes: list[str] = Field(default_factory=list)
    source_path: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source_path must not be empty")
        path = Path(cleaned)
        if (
            path.is_absolute()
            or PureWindowsPath(cleaned).is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("Obsidian pattern path must be vault-relative")
        return path.as_posix()

    @field_validator("source_sha256")
    @classmethod
    def validate_source_sha256(cls, value: str) -> str:
        if any(char not in "0123456789abcdef" for char in value.lower()):
            raise ValueError("source_sha256 must be hexadecimal")
        return value.lower()


class NotionStatuteRecord(StrictSupplementalModel):
    source_kind: Literal["notion_statute"] = "notion_statute"
    record_id: str = Field(min_length=1)
    law_name: str = Field(min_length=1)
    article: str = Field(min_length=1)
    text: str = Field(min_length=1)
    notion_url: str = Field(min_length=1)
    last_edited_time: str | None = None


class ExistingProblemNote(StrictSupplementalModel):
    source_kind: Literal["obsidian_problem_note"] = "obsidian_problem_note"
    relative_path: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("relative_path must not be empty")
        path = Path(cleaned)
        if (
            path.is_absolute()
            or PureWindowsPath(cleaned).is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("Problem note path must be vault-relative")
        if path.suffix.lower() != ".md":
            raise ValueError("Problem note path must end in .md")
        return path.as_posix()


class SupplementalRetrievalBundle(StrictSupplementalModel):
    """Auditable non-PDF context used by study-draft generation.

    The bundle intentionally has no field for 論文ナビゲートテキスト. Criminal-law
    common rules are sourced from the registered Obsidian argument patterns, and
    statute text is sourced from Notion.
    """

    schema_version: Literal["supplemental_retrieval.v1"] = "supplemental_retrieval.v1"
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    search_attempts: list[SupplementalSearchAttempt] = Field(default_factory=list)
    argument_patterns: list[ObsidianArgumentPattern] = Field(default_factory=list)
    statutes: list[NotionStatuteRecord] = Field(default_factory=list)
    existing_problem_notes: list[ExistingProblemNote] = Field(default_factory=list)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
