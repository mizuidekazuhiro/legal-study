from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from legal_study.models import BBox, DocumentInspection


class ReviewStatus(StrEnum):
    AUTO_VERIFIED = "AUTO_VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNRESOLVED = "UNRESOLVED"


class ReconciliationRecord(BaseModel):
    id: str
    page_number: int
    source_kind: str
    bbox: BBox
    native_original: str | None = None
    ocr_original: str | None = None
    normalized_native: str | None = None
    normalized_ocr: str | None = None
    similarity: float | None = None
    coordinate_match: bool | None = None
    selected_source: str | None = None
    status: ReviewStatus
    reason: str
    evidence_image: str | None = None
    ocr_confidence: float | None = None


class ReconciliationResult(BaseModel):
    schema_version: int = 1
    source_sha256: str
    records: list[ReconciliationRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


def normalize_for_comparison(text: str | None) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()


def _bbox_from_target(target: dict[str, Any]) -> BBox:
    values = target.get("bbox")
    if not isinstance(values, list) or len(values) != 4:
        raise ValueError("OCR target is missing a four-value bbox")
    return BBox(x0=values[0], y0=values[1], x1=values[2], y1=values[3])


def _bbox_dict_values(value: object) -> list[float] | None:
    if not isinstance(value, dict):
        return None
    candidate = [value.get("x0"), value.get("y0"), value.get("x1"), value.get("y1")]
    if any(item is None for item in candidate):
        return None
    return [float(item) for item in candidate]


def _rect_overlap_ratio(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = (float(value) for value in a)
    bx0, by0, bx1, by1 = (float(value) for value in b)
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    intersection = ix * iy
    area = max((ax1 - ax0) * (ay1 - ay0), 1e-9)
    return intersection / area


def _ocr_candidate(
    evidence: dict[str, Any], target: dict[str, Any]
) -> tuple[str | None, float | None, bool | None]:
    result = evidence.get("result")
    if not isinstance(result, dict):
        return None, None, False

    native_bbox = target.get("native_bbox")
    lines = result.get("lines")
    if isinstance(native_bbox, list) and len(native_bbox) == 4 and isinstance(lines, list):
        best: tuple[float, str, float | None] | None = None
        for line in lines:
            if not isinstance(line, dict):
                continue
            pdf_bbox = _bbox_dict_values(line.get("pdf_bbox"))
            text = line.get("text")
            if pdf_bbox is None or not isinstance(text, str):
                continue
            overlap = _rect_overlap_ratio(native_bbox, pdf_bbox)
            confidence = line.get("confidence")
            item = (
                overlap,
                text,
                float(confidence) if isinstance(confidence, int | float) else None,
            )
            if best is None or item[0] > best[0]:
                best = item
        if best is not None:
            overlap, text, confidence = best
            return text, confidence, overlap >= 0.2
        return None, None, False

    text = result.get("text")
    confidence = result.get("confidence")
    return (
        str(text) if isinstance(text, str) else None,
        float(confidence) if isinstance(confidence, int | float) else None,
        None,
    )


def _normalized_agreement(native: str, ocr: str) -> str | None:
    if native == ocr:
        return "exact"
    if len(native) >= 2 and native in ocr:
        return "native_contained_in_ocr"
    if len(ocr) >= 2 and ocr in native:
        return "ocr_contained_in_native"
    return None


def _record_from_ocr(
    *,
    record_id: str,
    page_number: int,
    evidence: dict[str, Any],
) -> ReconciliationRecord:
    target = evidence.get("target")
    if not isinstance(target, dict):
        raise TypeError("OCR evidence is missing target metadata")
    kind = str(target.get("kind", "unknown"))
    native = target.get("native_candidate")
    native_text = str(native) if isinstance(native, str) else None
    ocr_text, confidence, coordinate_match = _ocr_candidate(evidence, target)
    norm_native = normalize_for_comparison(native_text)
    norm_ocr = normalize_for_comparison(ocr_text)
    similarity = (
        SequenceMatcher(None, norm_native, norm_ocr).ratio()
        if norm_native and norm_ocr
        else None
    )
    agreement = _normalized_agreement(norm_native, norm_ocr) if norm_native and norm_ocr else None
    execution_status = str(evidence.get("status", "unknown"))

    if execution_status != "completed":
        status = ReviewStatus.UNRESOLVED
        reason = execution_status
        selected_source = None
    elif (
        kind == "suspect_native_text"
        and norm_native
        and agreement is not None
        and coordinate_match is True
    ):
        status = ReviewStatus.AUTO_VERIFIED
        reason = f"normalized_{agreement}_with_coordinate_overlap"
        selected_source = "native"
    elif kind == "suspect_native_text":
        status = ReviewStatus.NEEDS_REVIEW
        reason = "suspicious_native_ocr_mismatch"
        selected_source = None
    elif kind == "image_region":
        status = ReviewStatus.NEEDS_REVIEW
        reason = "ocr_only_image_region"
        selected_source = "ocr_supplement"
    elif kind == "full_page":
        status = ReviewStatus.NEEDS_REVIEW
        reason = "full_page_ocr_on_insufficient_or_low_quality_native"
        selected_source = None
    else:
        status = ReviewStatus.NEEDS_REVIEW
        reason = "unclassified_ocr_evidence"
        selected_source = None

    return ReconciliationRecord(
        id=record_id,
        page_number=page_number,
        source_kind=kind,
        bbox=_bbox_from_target(target),
        native_original=native_text,
        ocr_original=ocr_text,
        normalized_native=norm_native or None,
        normalized_ocr=norm_ocr or None,
        similarity=round(similarity, 6) if similarity is not None else None,
        coordinate_match=coordinate_match,
        selected_source=selected_source,
        status=status,
        reason=reason,
        evidence_image=str(target.get("image")) if target.get("image") else None,
        ocr_confidence=confidence,
    )


def build_reconciliation(
    inspection: DocumentInspection,
    ocr_payload: dict[str, Any],
    review_payload: dict[str, Any],
) -> ReconciliationResult:
    records: list[ReconciliationRecord] = []
    ocr_pages = ocr_payload.get("pages", {})
    if not isinstance(ocr_pages, dict):
        raise TypeError("ocr.json pages must be an object")

    for page in inspection.pages:
        page_payload = ocr_pages.get(str(page.page_number), {})
        if not isinstance(page_payload, dict):
            continue
        full_page = page_payload.get("full_page")
        if isinstance(full_page, dict):
            records.append(
                _record_from_ocr(
                    record_id=f"p{page.page_number:04d}-full-001",
                    page_number=page.page_number,
                    evidence=full_page,
                )
            )
        regions = page_payload.get("regions", [])
        if not isinstance(regions, list):
            raise TypeError("OCR page regions must be a list")
        for index, evidence in enumerate(regions, start=1):
            if not isinstance(evidence, dict):
                continue
            records.append(
                _record_from_ocr(
                    record_id=f"p{page.page_number:04d}-ocr-{index:03d}",
                    page_number=page.page_number,
                    evidence=evidence,
                )
            )

    review_pages = review_payload.get("pages", [])
    if not isinstance(review_pages, list):
        raise TypeError("review_manifest.json pages must be a list")
    for page_payload in review_pages:
        if not isinstance(page_payload, dict):
            continue
        page_number = int(page_payload["page_number"])
        crops = page_payload.get("review_crops", [])
        if not isinstance(crops, list):
            continue
        for index, crop in enumerate(crops, start=1):
            if not isinstance(crop, dict):
                continue
            values = crop.get("bbox")
            if not isinstance(values, list) or len(values) != 4:
                continue
            records.append(
                ReconciliationRecord(
                    id=f"p{page_number:04d}-red-{index:03d}",
                    page_number=page_number,
                    source_kind="red_vector_cluster",
                    bbox=BBox(x0=values[0], y0=values[1], x1=values[2], y1=values[3]),
                    status=ReviewStatus.NEEDS_REVIEW,
                    reason="red_vector_semantics_require_visual_review",
                    evidence_image=str(crop.get("image")) if crop.get("image") else None,
                )
            )

    counts = {status.value: 0 for status in ReviewStatus}
    for record in records:
        counts[record.status.value] += 1
    return ReconciliationResult(
        source_sha256=inspection.sha256,
        records=records,
        counts=counts,
    )
