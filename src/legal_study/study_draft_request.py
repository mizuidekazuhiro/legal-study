from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from legal_study.study_draft import DraftSource, study_draft_json_schema
from legal_study.supplemental_retrieval import SupplementalRetrievalBundle


class StrictBundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleTextDocument(StrictBundleModel):
    kind: Literal["handoff", "instruction"]
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    text: str = Field(min_length=1)


class BundleReviewImage(StrictBundleModel):
    page_number: int = Field(ge=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    mime_type: str = Field(min_length=1)


class BundleLogicalMarker(StrictBundleModel):
    id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    color: str = Field(min_length=1)
    exact_text: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    boundary_confidence: float | None = None
    review_status: str = Field(min_length=1)
    reason: str | None = None
    evidence_image: str | None = Field(default=None, min_length=1)


class StudyDraftRequestBundle(StrictBundleModel):
    schema_version: Literal["study_draft_request.v1"] = "study_draft_request.v1"
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    source: DraftSource
    handoff: BundleTextDocument
    instructions: list[BundleTextDocument] = Field(min_length=1)
    review_sheets: list[BundleReviewImage] = Field(default_factory=list)
    logical_markers: list[BundleLogicalMarker] = Field(default_factory=list)
    primary_text_review_required_pages: list[int] = Field(default_factory=list)
    supplemental: SupplementalRetrievalBundle | None = None
    response_schema: dict[str, Any]
    response_schema_sha256: str = Field(min_length=64, max_length=64)
    output_filename: Literal["study_draft.json"] = "study_draft.json"


_SUBJECT_ANKI_INSTRUCTION = {
    "administrative": "20_行政法_Ankiカード作成仕様_科目別特則.md",
    "constitutional": "21_憲法_Ankiカード作成仕様_科目別特則.md",
    "criminal": "22_刑法_Ankiカード作成仕様_科目別特則.md",
}


def required_instruction_names(
    subject: str,
    *,
    include_anki: bool = True,
    include_obsidian: bool = True,
) -> list[str]:
    """Return the instruction documents required for one draft-generation request."""

    names = [
        "00_論文作成・登録_本番_プロジェクト指示.md",
        "01_論文作成_共通原則_原文・赤字・資料確認.md",
    ]
    if include_anki:
        names.append("10_Ankiカード作成仕様_共通.md")
        subject_rule = _SUBJECT_ANKI_INSTRUCTION.get(subject)
        if subject_rule is None:
            raise ValueError(f"No Anki subject instruction mapping for subject: {subject}")
        names.append(subject_rule)
    if include_obsidian:
        names.extend(
            [
                "30_Obsidianノート作成仕様_共通.md",
                "31_Obsidian_Vault直接更新仕様.md",
            ]
        )
    return names


def build_study_draft_request_bundle(
    *,
    subject: str,
    question: str,
    source: DraftSource,
    run_dir: Path,
    instruction_dir: Path,
    include_anki: bool = True,
    include_obsidian: bool = True,
    supplemental: SupplementalRetrievalBundle | None = None,
) -> StudyDraftRequestBundle:
    """Build the deterministic, API-independent input bundle for one study draft.

    This function reads files only. It does not call OpenAI, modify queue state,
    create Anki/Obsidian outputs, or register anything in Notion.
    """

    root = run_dir.resolve()
    instruction_root = instruction_dir.resolve()

    if supplemental is not None and (
        supplemental.subject != subject or supplemental.question != question
    ):
        raise ValueError(
            "Supplemental retrieval identity mismatch: "
            f"supplemental=({supplemental.subject!r}, {supplemental.question!r}), "
            f"request=({subject!r}, {question!r})"
        )

    if subject != source_subject_hint(subject):
        raise ValueError("Subject normalization failed")
    if not source.run_id.strip():
        raise ValueError("source.run_id must not be empty")

    handoff_path = _resolve_inside(root, source.handoff_path)
    handoff = _read_text_document(
        kind="handoff",
        name=handoff_path.name,
        path=source.handoff_path,
        absolute_path=handoff_path,
    )
    handoff_frontmatter = _read_yaml_frontmatter(handoff.text, handoff.name)

    instruction_names = required_instruction_names(
        subject,
        include_anki=include_anki,
        include_obsidian=include_obsidian,
    )
    instructions: list[BundleTextDocument] = []
    for name in instruction_names:
        absolute_path = _resolve_inside(instruction_root, name)
        instructions.append(
            _read_text_document(
                kind="instruction",
                name=name,
                path=name,
                absolute_path=absolute_path,
            )
        )

    canonical_path = _resolve_inside(root, source.canonical_source_path)
    canonical = _read_json_object(canonical_path)
    validation_path = _resolve_inside(root, source.problem_validation_path)
    problem_validation = _read_json_object(validation_path)
    if problem_validation.get("valid") is not True:
        raise ValueError("problem_validation.json is not valid=true")
    _validate_bundle_source_identity(
        subject=subject,
        question=question,
        source=source,
        canonical=canonical,
        handoff_frontmatter=handoff_frontmatter,
    )

    primary_text_review_required_pages = sorted(
        int(page["page_number"])
        for page in canonical.get("pages", [])
        if isinstance(page, dict)
        and page.get("page_number") is not None
        and int(page.get("repair_review_count", 0)) > 0
    )

    logical_markers = _read_logical_markers(
        canonical=canonical,
        requested_pages=source.requested_pages,
    )

    sheets = canonical.get("handoff_review_sheets", {})
    if not isinstance(sheets, dict):
        raise TypeError("canonical_source.json handoff_review_sheets must be an object")

    review_sheets: list[BundleReviewImage] = []
    for page_key, relative_path in sorted(
        sheets.items(),
        key=lambda item: int(item[0]),
    ):
        page_number = int(page_key)
        if page_number not in source.requested_pages:
            raise ValueError(
                f"Review sheet page {page_number} is outside requested_pages"
            )
        if not isinstance(relative_path, str):
            raise TypeError("Review sheet path must be a string")
        image_path = _resolve_inside(root, relative_path)
        if not image_path.is_file():
            raise FileNotFoundError(f"Review sheet is missing: {relative_path}")
        mime_type, _ = mimetypes.guess_type(image_path.name)
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError(
                f"Unsupported review sheet media type: {relative_path} -> {mime_type}"
            )
        review_sheets.append(
            BundleReviewImage(
                page_number=page_number,
                path=Path(relative_path).as_posix(),
                sha256=_sha256_file(image_path),
                mime_type=mime_type,
            )
        )

    schema = study_draft_json_schema()
    schema_bytes = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return StudyDraftRequestBundle(
        subject=subject,
        question=question,
        source=source,
        handoff=handoff,
        instructions=instructions,
        review_sheets=review_sheets,
        logical_markers=logical_markers,
        primary_text_review_required_pages=primary_text_review_required_pages,
        supplemental=supplemental,
        response_schema=schema,
        response_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
    )


def _read_logical_markers(
    *,
    canonical: dict[str, Any],
    requested_pages: list[int],
) -> list[BundleLogicalMarker]:
    raw_items = canonical.get("logical_markers", [])
    if not isinstance(raw_items, list):
        raise TypeError("canonical_source.json logical_markers must be a list")

    requested = set(requested_pages)
    result: list[BundleLogicalMarker] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise TypeError("canonical_source.json logical marker must be an object")
        page_number = item.get("page_number")
        if not isinstance(page_number, int) or page_number not in requested:
            continue

        exact_text = item.get("exact_text")
        color = item.get("color")
        marker_id = item.get("id")
        start_char = item.get("start_char")
        end_char = item.get("end_char")
        review_status = item.get("review_status")
        evidence_image = item.get("evidence_image")
        if not isinstance(marker_id, str) or not marker_id.strip():
            raise ValueError("Logical marker id must be a non-empty string")
        if not isinstance(color, str) or not color.strip():
            raise ValueError(f"Logical marker {marker_id} color is missing")
        if not isinstance(exact_text, str) or not exact_text:
            raise ValueError(f"Logical marker {marker_id} exact_text is missing")
        if not isinstance(start_char, int) or not isinstance(end_char, int):
            raise TypeError(f"Logical marker {marker_id} char bounds must be integers")
        if end_char < start_char:
            raise ValueError(f"Logical marker {marker_id} has reversed char bounds")
        if not isinstance(review_status, str) or not review_status.strip():
            raise ValueError(f"Logical marker {marker_id} review_status is missing")
        if evidence_image is not None and (
            not isinstance(evidence_image, str) or not evidence_image.strip()
        ):
            raise TypeError(
                f"Logical marker {marker_id} evidence_image must be a non-empty string"
            )

        boundary_confidence = item.get("boundary_confidence")
        if boundary_confidence is not None and not isinstance(
            boundary_confidence, (int, float)
        ):
            raise TypeError(
                f"Logical marker {marker_id} boundary_confidence must be numeric"
            )
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise TypeError(f"Logical marker {marker_id} reason must be a string")

        result.append(
            BundleLogicalMarker(
                id=marker_id,
                page_number=page_number,
                color=color,
                exact_text=exact_text,
                start_char=start_char,
                end_char=end_char,
                boundary_confidence=(
                    float(boundary_confidence)
                    if boundary_confidence is not None
                    else None
                ),
                review_status=review_status,
                reason=reason,
                evidence_image=(
                    Path(evidence_image).as_posix()
                    if isinstance(evidence_image, str)
                    else None
                ),
            )
        )

    return sorted(result, key=lambda item: (item.page_number, item.start_char, item.id))


def source_subject_hint(subject: str) -> str:
    cleaned = subject.strip()
    if not cleaned:
        raise ValueError("subject must not be empty")
    return cleaned


def _resolve_inside(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Path escapes configured root: {relative}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes configured root: {relative}")
    return resolved


def _read_text_document(
    *,
    kind: Literal["handoff", "instruction"],
    name: str,
    path: str,
    absolute_path: Path,
) -> BundleTextDocument:
    if not absolute_path.is_file():
        raise FileNotFoundError(f"Required {kind} file is missing: {path}")
    payload = absolute_path.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Required {kind} file is not UTF-8: {path}") from exc
    if not text.strip():
        raise ValueError(f"Required {kind} file is empty: {path}")
    return BundleTextDocument(
        kind=kind,
        name=name,
        path=Path(path).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        text=text,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required JSON artifact is missing: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON artifact: {path.name}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"JSON artifact must contain an object: {path.name}")
    return payload


def _validate_bundle_source_identity(
    *,
    subject: str,
    question: str,
    source: DraftSource,
    canonical: dict[str, Any],
    handoff_frontmatter: dict[str, Any],
) -> None:
    canonical_source = canonical.get("source")
    if not isinstance(canonical_source, dict):
        raise TypeError("canonical_source.json has no source object")

    comparisons = {
        "subject": (canonical.get("subject"), subject),
        "question": (canonical.get("question"), question),
        "source_sha256": (canonical_source.get("sha256"), source.source_sha256),
        "requested_pages": (
            canonical_source.get("requested_pages"),
            source.requested_pages,
        ),
    }
    mismatches = [
        f"{key}: canonical={actual!r}, bundle={expected!r}"
        for key, (actual, expected) in comparisons.items()
        if actual != expected
    ]
    handoff_comparisons = {
        "subject": (handoff_frontmatter.get("subject"), subject),
        "question": (handoff_frontmatter.get("question"), question),
        "source_sha256": (
            handoff_frontmatter.get("source_sha256"),
            source.source_sha256,
        ),
        "source_pages": (
            handoff_frontmatter.get("source_pages"),
            source.requested_pages,
        ),
    }
    mismatches.extend(
        f"handoff {key}: handoff={actual!r}, bundle={expected!r}"
        for key, (actual, expected) in handoff_comparisons.items()
        if actual != expected
    )
    if mismatches:
        raise ValueError("Bundle source identity mismatch: " + "; ".join(mismatches))


def _read_yaml_frontmatter(text: str, name: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ValueError(f"Handoff has no YAML frontmatter: {name}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"Handoff YAML frontmatter is not terminated: {name}")
    try:
        payload = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid handoff YAML frontmatter: {name}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"Handoff YAML frontmatter must be an object: {name}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
