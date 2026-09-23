from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from legal_study.study_draft import EvidenceRef, StudyDraft


class StudyDraftValidationIssue(BaseModel):
    code: str
    message: str
    location: str


class StudyDraftValidationResult(BaseModel):
    valid: bool
    issues: list[StudyDraftValidationIssue] = Field(default_factory=list)


@dataclass(frozen=True)
class _EvidenceLocator:
    evidence_id: str
    page_number: int | None
    source_kind: str
    artifact_path: str
    source_anchor: str | None = None
    review_required: bool = False


def validate_study_draft_evidence(
    *,
    draft: StudyDraft,
    run_dir: Path,
) -> StudyDraftValidationResult:
    """Validate that every draft evidence reference resolves inside one ingest run.

    This is deliberately API-independent. It validates source identity and
    provenance only; it does not judge the legal content of the draft.
    """

    root = run_dir.resolve()
    issues: list[StudyDraftValidationIssue] = []

    canonical_path = _resolve_run_file(root, draft.source.canonical_source_path)
    handoff_path = _resolve_run_file(root, draft.source.handoff_path)
    validation_path = _resolve_run_file(root, draft.source.problem_validation_path)

    canonical = _read_json(canonical_path, issues, "source.canonical_source_path")
    problem_validation = _read_json(
        validation_path,
        issues,
        "source.problem_validation_path",
    )
    handoff_frontmatter = _read_handoff_frontmatter(
        handoff_path,
        issues,
        "source.handoff_path",
    )

    if canonical is None or handoff_frontmatter is None:
        return StudyDraftValidationResult(valid=False, issues=issues)

    _validate_source_identity(
        draft=draft,
        canonical=canonical,
        handoff_frontmatter=handoff_frontmatter,
        problem_validation=problem_validation,
        issues=issues,
    )

    evidence_index = _build_evidence_index(
        canonical=canonical,
        handoff_path=draft.source.handoff_path,
        canonical_path=draft.source.canonical_source_path,
    )

    refs = list(_iter_draft_evidence_refs(draft))
    for location, reference in refs:
        locator = evidence_index.get(reference.evidence_id)
        if locator is None:
            issues.append(
                StudyDraftValidationIssue(
                    code="UNKNOWN_EVIDENCE_ID",
                    message=f"Evidence id does not exist in canonical/handoff: {reference.evidence_id}",
                    location=location,
                )
            )
            continue
        _compare_reference(
            reference=reference,
            locator=locator,
            location=location,
            issues=issues,
        )
        artifact = _resolve_run_file(root, reference.artifact_path)
        if not artifact.is_file():
            issues.append(
                StudyDraftValidationIssue(
                    code="MISSING_EVIDENCE_ARTIFACT",
                    message=f"Evidence artifact is missing: {reference.artifact_path}",
                    location=location,
                )
            )

    expected_sheets = {
        int(page): str(path)
        for page, path in dict(canonical.get("handoff_review_sheets", {})).items()
    }
    for index, review in enumerate(draft.visual_reviews):
        location = f"visual_reviews[{index}]"
        expected = expected_sheets.get(review.page_number)
        if expected != review.review_sheet_path:
            issues.append(
                StudyDraftValidationIssue(
                    code="REVIEW_SHEET_MISMATCH",
                    message=(
                        f"Review sheet for page {review.page_number} must be "
                        f"{expected!r}, got {review.review_sheet_path!r}"
                    ),
                    location=location,
                )
            )
        sheet = _resolve_run_file(root, review.review_sheet_path)
        if not sheet.is_file():
            issues.append(
                StudyDraftValidationIssue(
                    code="MISSING_REVIEW_SHEET",
                    message=f"Review sheet is missing: {review.review_sheet_path}",
                    location=location,
                )
            )

    return StudyDraftValidationResult(valid=not issues, issues=issues)


def _resolve_run_file(run_dir: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Run artifact path escapes run directory: {relative}")
    resolved = (run_dir / path).resolve()
    if not resolved.is_relative_to(run_dir):
        raise ValueError(f"Run artifact path escapes run directory: {relative}")
    return resolved


def _read_json(
    path: Path,
    issues: list[StudyDraftValidationIssue],
    location: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        issues.append(
            StudyDraftValidationIssue(
                code="MISSING_REQUIRED_ARTIFACT",
                message=f"Required artifact is missing: {path.name}",
                location=location,
            )
        )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_REQUIRED_ARTIFACT",
                message=f"Could not read {path.name}: {exc}",
                location=location,
            )
        )
        return None
    if not isinstance(payload, dict):
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_REQUIRED_ARTIFACT",
                message=f"{path.name} must contain a JSON object",
                location=location,
            )
        )
        return None
    return payload


def _read_handoff_frontmatter(
    path: Path,
    issues: list[StudyDraftValidationIssue],
    location: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        issues.append(
            StudyDraftValidationIssue(
                code="MISSING_REQUIRED_ARTIFACT",
                message=f"Required artifact is missing: {path.name}",
                location=location,
            )
        )
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_REQUIRED_ARTIFACT",
                message=f"Could not read {path.name}: {exc}",
                location=location,
            )
        )
        return None

    if not text.startswith("---\n"):
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_HANDOFF_FRONTMATTER",
                message="Handoff markdown has no YAML frontmatter",
                location=location,
            )
        )
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_HANDOFF_FRONTMATTER",
                message="Handoff YAML frontmatter is not terminated",
                location=location,
            )
        )
        return None
    try:
        payload = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_HANDOFF_FRONTMATTER",
                message=f"Could not parse handoff YAML: {exc}",
                location=location,
            )
        )
        return None
    return payload if isinstance(payload, dict) else None


def _validate_source_identity(
    *,
    draft: StudyDraft,
    canonical: dict[str, Any],
    handoff_frontmatter: dict[str, Any],
    problem_validation: dict[str, Any] | None,
    issues: list[StudyDraftValidationIssue],
) -> None:
    canonical_source = canonical.get("source")
    if not isinstance(canonical_source, dict):
        issues.append(
            StudyDraftValidationIssue(
                code="INVALID_CANONICAL_SOURCE",
                message="canonical_source.json has no source object",
                location="source",
            )
        )
        return

    expected = {
        "subject": draft.subject,
        "question": draft.question,
        "source_sha256": draft.source.source_sha256,
        "source_pages": draft.source.requested_pages,
    }
    actual = {
        "subject": canonical.get("subject"),
        "question": canonical.get("question"),
        "source_sha256": canonical_source.get("sha256"),
        "source_pages": canonical_source.get("requested_pages"),
    }
    for key, value in expected.items():
        if actual.get(key) != value:
            issues.append(
                StudyDraftValidationIssue(
                    code="SOURCE_IDENTITY_MISMATCH",
                    message=f"Canonical {key}={actual.get(key)!r}, draft has {value!r}",
                    location=f"source.{key}",
                )
            )

    for key, value in expected.items():
        if handoff_frontmatter.get(key) != value:
            issues.append(
                StudyDraftValidationIssue(
                    code="HANDOFF_IDENTITY_MISMATCH",
                    message=(
                        f"Handoff {key}={handoff_frontmatter.get(key)!r}, "
                        f"draft has {value!r}"
                    ),
                    location=f"source.{key}",
                )
            )

    if problem_validation is not None and problem_validation.get("valid") is not True:
        issues.append(
            StudyDraftValidationIssue(
                code="PROBLEM_PACKET_NOT_VALID",
                message="problem_validation.json is not valid=true",
                location="source.problem_validation_path",
            )
        )


def _build_evidence_index(
    *,
    canonical: dict[str, Any],
    handoff_path: str,
    canonical_path: str,
) -> dict[str, _EvidenceLocator]:
    index: dict[str, _EvidenceLocator] = {}

    for page in canonical.get("pages", []):
        if not isinstance(page, dict) or page.get("page_number") is None:
            continue
        page_number = int(page["page_number"])
        _add_locator(
            index,
            _EvidenceLocator(
                evidence_id=f"page:{page_number}:reconciled_text",
                page_number=page_number,
                source_kind="canonical_page_text",
                artifact_path=canonical_path,
            ),
        )
        _add_locator(
            index,
            _EvidenceLocator(
                evidence_id=f"page:{page_number}:primary_text",
                page_number=page_number,
                source_kind="handoff_primary_text",
                artifact_path=handoff_path,
                source_anchor=f"PDF page {page_number} / Primary Reading Text",
                review_required=int(page.get("repair_review_count", 0)) > 0,
            ),
        )

    for item in canonical.get("reconciliation", {}).get("records", []):
        if isinstance(item, dict):
            _canonical_locator(
                index,
                item=item,
                default_kind="reconciliation_record",
                artifact_path=canonical_path,
            )
    for key, default_kind in (
        ("markers", "marker"),
        ("logical_markers", "logical_marker"),
        ("ocr_supplements", "ocr_supplement"),
    ):
        for item in canonical.get(key, []):
            if isinstance(item, dict):
                _canonical_locator(
                    index,
                    item=item,
                    default_kind=default_kind,
                    artifact_path=canonical_path,
                )

    for task in canonical.get("needs_review", []):
        if not isinstance(task, dict):
            continue
        _canonical_locator(
            index,
            item=task,
            default_kind="page_review",
            artifact_path=canonical_path,
            force_review=True,
        )
        for issue in task.get("issues", []):
            if isinstance(issue, dict):
                enriched = dict(issue)
                enriched.setdefault("page_number", task.get("page_number"))
                _canonical_locator(
                    index,
                    item=enriched,
                    default_kind="review_issue",
                    artifact_path=canonical_path,
                    force_review=True,
                )

    for page, path in dict(canonical.get("handoff_review_sheets", {})).items():
        page_number = int(page)
        _add_locator(
            index,
            _EvidenceLocator(
                evidence_id=f"review-sheet:{page_number}",
                page_number=page_number,
                source_kind="review_sheet",
                artifact_path=str(path),
                review_required=True,
            ),
        )
    return index


def _canonical_locator(
    index: dict[str, _EvidenceLocator],
    *,
    item: dict[str, Any],
    default_kind: str,
    artifact_path: str,
    force_review: bool = False,
) -> None:
    evidence_id = item.get("id")
    if not isinstance(evidence_id, str) or not evidence_id:
        return
    page_number = item.get("page_number")
    status = str(item.get("status") or item.get("review_status") or "")
    source_kind = str(item.get("source_kind") or default_kind)
    _add_locator(
        index,
        _EvidenceLocator(
            evidence_id=evidence_id,
            page_number=int(page_number) if page_number is not None else None,
            source_kind=source_kind,
            artifact_path=artifact_path,
            review_required=force_review
            or status.upper() in {"NEEDS_REVIEW", "REVIEW_REQUIRED", "UNRESOLVED"},
        ),
    )


def _add_locator(index: dict[str, _EvidenceLocator], locator: _EvidenceLocator) -> None:
    existing = index.get(locator.evidence_id)
    if existing is not None and existing != locator:
        raise ValueError(f"Duplicate evidence id with conflicting provenance: {locator.evidence_id}")
    index[locator.evidence_id] = locator


def _iter_draft_evidence_refs(draft: StudyDraft):
    for card_index, card in enumerate(draft.anki_cards):
        for field_name in ("front", "back", "extra"):
            text = getattr(card, field_name)
            for ref_index, reference in enumerate(text.evidence_refs):
                yield (
                    f"anki_cards[{card_index}].{field_name}.evidence_refs[{ref_index}]",
                    reference,
                )
    if draft.obsidian_note is not None:
        for section_index, section in enumerate(draft.obsidian_note.sections):
            for block_index, block in enumerate(section.blocks):
                for ref_index, reference in enumerate(block.evidence_refs):
                    location = (
                        "obsidian_note.sections"
                        f"[{section_index}].blocks[{block_index}]"
                        f".evidence_refs[{ref_index}]"
                    )
                    yield (location, reference)
    for unresolved_index, unresolved in enumerate(draft.unresolved):
        for ref_index, reference in enumerate(unresolved.evidence_refs):
            yield (
                f"unresolved[{unresolved_index}].evidence_refs[{ref_index}]",
                reference,
            )


def _compare_reference(
    *,
    reference: EvidenceRef,
    locator: _EvidenceLocator,
    location: str,
    issues: list[StudyDraftValidationIssue],
) -> None:
    comparisons = (
        ("page_number", reference.page_number, locator.page_number),
        ("source_kind", reference.source_kind, locator.source_kind),
        ("artifact_path", reference.artifact_path, locator.artifact_path),
        ("source_anchor", reference.source_anchor, locator.source_anchor),
    )
    for field_name, actual, expected in comparisons:
        if actual != expected:
            issues.append(
                StudyDraftValidationIssue(
                    code="EVIDENCE_PROVENANCE_MISMATCH",
                    message=(
                        f"{reference.evidence_id} {field_name} must be "
                        f"{expected!r}, got {actual!r}"
                    ),
                    location=location,
                )
            )
    if locator.review_required and reference.review_required is not True:
        issues.append(
            StudyDraftValidationIssue(
                code="REVIEW_REQUIRED_NOT_PROPAGATED",
                message=(
                    f"{reference.evidence_id} is review-required in source evidence "
                    "but draft reference does not preserve that flag"
                ),
                location=location,
            )
        )
