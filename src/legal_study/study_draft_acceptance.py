from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from legal_study.study_draft import StudyDraft
from legal_study.study_draft_validation import validate_study_draft_evidence


class StrictAcceptanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StudyDraftAcceptanceIssue(StrictAcceptanceModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    location: str | None = None


class StudyDraftAcceptanceResult(StrictAcceptanceModel):
    accepted: bool
    output_path: str | None = None
    draft_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    issues: list[StudyDraftAcceptanceIssue] = Field(default_factory=list)


def accept_study_draft_response(
    *,
    raw_response_text: str,
    run_dir: Path,
) -> StudyDraftAcceptanceResult:
    """Validate one model response and persist only an accepted draft candidate.

    This layer is deliberately transport-independent: it accepts raw JSON text,
    performs schema/provenance/review gates, and writes study_draft.json only
    after all gates pass. It does not call OpenAI or change queue state.
    """

    root = run_dir.resolve()
    if not root.is_dir():
        return _rejected(
            "RUN_DIR_MISSING",
            f"Run directory does not exist: {run_dir}",
        )

    try:
        payload = json.loads(raw_response_text)
    except json.JSONDecodeError as exc:
        return _rejected(
            "INVALID_JSON",
            f"Response is not valid JSON: {exc.msg}",
        )

    if not isinstance(payload, dict):
        return _rejected(
            "INVALID_JSON_ROOT",
            "Response JSON root must be an object",
        )

    try:
        draft = StudyDraft.model_validate(payload)
    except ValidationError as exc:
        return StudyDraftAcceptanceResult(
            accepted=False,
            issues=[
                StudyDraftAcceptanceIssue(
                    code="SCHEMA_VALIDATION_FAILED",
                    message=_format_pydantic_errors(exc),
                )
            ],
        )

    issues: list[StudyDraftAcceptanceIssue] = []

    provenance = validate_study_draft_evidence(draft=draft, run_dir=root)
    for issue in provenance.issues:
        issues.append(
            StudyDraftAcceptanceIssue(
                code=issue.code,
                message=issue.message,
                location=issue.location,
            )
        )

    issues.extend(_review_gate_issues(draft))

    if issues:
        return StudyDraftAcceptanceResult(
            accepted=False,
            issues=issues,
        )

    normalized = _normalized_json_bytes(draft)
    draft_sha256 = hashlib.sha256(normalized).hexdigest()
    output = root / "study_draft.json"

    if output.exists():
        try:
            existing = output.read_bytes()
        except OSError as exc:
            return _rejected(
                "OUTPUT_READ_FAILED",
                f"Could not read existing study_draft.json: {exc}",
            )
        if existing == normalized:
            return StudyDraftAcceptanceResult(
                accepted=True,
                output_path="study_draft.json",
                draft_sha256=draft_sha256,
            )
        return _rejected(
            "OUTPUT_ALREADY_EXISTS",
            "study_draft.json already exists with different content; refusing overwrite",
        )

    try:
        _atomic_write(output, normalized)
    except OSError as exc:
        return _rejected(
            "OUTPUT_WRITE_FAILED",
            f"Could not persist study_draft.json: {exc}",
        )

    return StudyDraftAcceptanceResult(
        accepted=True,
        output_path="study_draft.json",
        draft_sha256=draft_sha256,
    )


def _review_gate_issues(draft: StudyDraft) -> list[StudyDraftAcceptanceIssue]:
    issues: list[StudyDraftAcceptanceIssue] = []

    for index, unresolved in enumerate(draft.unresolved):
        if unresolved.blocking:
            issues.append(
                StudyDraftAcceptanceIssue(
                    code="BLOCKING_UNRESOLVED",
                    message=(
                        f"Blocking uncertainty remains: "
                        f"{unresolved.code}: {unresolved.message}"
                    ),
                    location=f"unresolved[{index}]",
                )
            )

    for index, review in enumerate(draft.visual_reviews):
        if not review.reviewed:
            issues.append(
                StudyDraftAcceptanceIssue(
                    code="VISUAL_REVIEW_INCOMPLETE",
                    message=f"Visual review for PDF page {review.page_number} is not complete",
                    location=f"visual_reviews[{index}]",
                )
            )
        if review.unresolved_issue_ids:
            issues.append(
                StudyDraftAcceptanceIssue(
                    code="VISUAL_REVIEW_UNRESOLVED",
                    message=(
                        f"Visual review for PDF page {review.page_number} still has "
                        f"unresolved issues: {review.unresolved_issue_ids}"
                    ),
                    location=f"visual_reviews[{index}].unresolved_issue_ids",
                )
            )

    for location, text in _iter_draft_texts(draft):
        if text.review_required:
            issues.append(
                StudyDraftAcceptanceIssue(
                    code="DRAFT_TEXT_REVIEW_REQUIRED",
                    message="Draft text still requires review",
                    location=location,
                )
            )

    return issues


def _iter_draft_texts(draft: StudyDraft):
    for card_index, card in enumerate(draft.anki_cards):
        for field_name in ("front", "back", "extra"):
            yield (
                f"anki_cards[{card_index}].{field_name}",
                getattr(card, field_name),
            )
    if draft.obsidian_note is not None:
        for section_index, section in enumerate(draft.obsidian_note.sections):
            for block_index, block in enumerate(section.blocks):
                yield (
                    f"obsidian_note.sections[{section_index}].blocks[{block_index}]",
                    block,
                )


def _normalized_json_bytes(draft: StudyDraft) -> bytes:
    payload: dict[str, Any] = draft.model_dump(mode="json")
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def _atomic_write(output: Path, content: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _format_pydantic_errors(exc: ValidationError) -> str:
    messages = []
    for error in exc.errors(include_url=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        messages.append(f"{location}: {error['msg']}")
    return "; ".join(messages)


def _rejected(code: str, message: str) -> StudyDraftAcceptanceResult:
    return StudyDraftAcceptanceResult(
        accepted=False,
        issues=[
            StudyDraftAcceptanceIssue(
                code=code,
                message=message,
            )
        ],
    )
