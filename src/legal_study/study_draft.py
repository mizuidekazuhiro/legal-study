from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextTransformation(StrEnum):
    VERBATIM = "VERBATIM"
    EXTRACTED_REORDERED = "EXTRACTED_REORDERED"
    MINIMAL_CONNECTIVE = "MINIMAL_CONNECTIVE"


class DraftSource(StrictModel):
    source_sha256: str = Field(min_length=64, max_length=64)
    source_snapshot_path: str
    stable_page_ids: list[str] = Field(min_length=1)
    requested_pages: list[int] = Field(min_length=1)
    run_id: str
    handoff_path: str
    canonical_source_path: str = "canonical_source.json"
    problem_validation_path: str = "problem_validation.json"

    @field_validator(
        "source_snapshot_path",
        "handoff_path",
        "canonical_source_path",
        "problem_validation_path",
    )
    @classmethod
    def validate_paths(cls, value: str, info) -> str:
        if info.field_name == "source_snapshot_path":
            if not value.strip():
                raise ValueError("source_snapshot_path must not be empty")
            return value
        return _run_relative_path(value)


class InstructionSource(StrictModel):
    name: str = Field(min_length=1)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class EvidenceRef(StrictModel):
    evidence_id: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    source_kind: str = Field(min_length=1)
    artifact_path: str
    source_anchor: str | None = None
    review_required: bool = False

    @field_validator("artifact_path")
    @classmethod
    def validate_artifact_path(cls, value: str) -> str:
        return _run_relative_path(value)


class DraftText(StrictModel):
    text: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    transformation: TextTransformation
    review_required: bool = False


class VisualReviewRecord(StrictModel):
    page_number: int = Field(ge=1)
    review_sheet_path: str
    reviewed: bool
    unresolved_issue_ids: list[str] = Field(default_factory=list)

    @field_validator("review_sheet_path")
    @classmethod
    def validate_review_sheet_path(cls, value: str) -> str:
        return _run_relative_path(value)


class DraftUncertainty(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    blocking: bool = True


class AnkiCardDraft(StrictModel):
    name: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    source: str = Field(min_length=1)
    pdf_page: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    subtopic: str = Field(min_length=1)
    level: Literal["L1", "L2", "L3", "L4"]
    anki_type: str = Field(min_length=1)
    front: DraftText
    back: DraftText
    extra: DraftText
    anki_tags: list[str] = Field(default_factory=list)
    anki_deck: str = Field(min_length=1)
    status: Literal["DRAFT"] = "DRAFT"
    export_to_anki: Literal[False] = False


class ObsidianSectionDraft(StrictModel):
    heading: str = Field(min_length=1)
    blocks: list[DraftText] = Field(default_factory=list)


class ObsidianNoteDraft(StrictModel):
    relative_path: str
    sections: list[ObsidianSectionDraft] = Field(min_length=1)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _vault_relative_path(value)


class StudyDraft(StrictModel):
    schema_version: Literal["study_draft.v1"] = "study_draft.v1"
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    source: DraftSource
    instruction_sources: list[InstructionSource] = Field(min_length=1)
    visual_reviews: list[VisualReviewRecord] = Field(default_factory=list)
    anki_cards: list[AnkiCardDraft] = Field(default_factory=list)
    obsidian_note: ObsidianNoteDraft | None = None
    unresolved: list[DraftUncertainty] = Field(default_factory=list)


def study_draft_json_schema() -> dict[str, object]:
    """Return the API-independent JSON schema for one study draft."""

    return StudyDraft.model_json_schema()


def _run_relative_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("artifact path must not be empty")
    path = Path(cleaned)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact path must stay inside the run directory")
    return path.as_posix()


def _vault_relative_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("vault relative path must not be empty")
    path = Path(cleaned)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("vault relative path must not escape the vault")
    if path.suffix.lower() != ".md":
        raise ValueError("Obsidian draft path must end in .md")
    return path.as_posix()
