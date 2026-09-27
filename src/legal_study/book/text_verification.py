from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel, Field

from legal_study.book.page_zones import PageZones, zones_for_bbox
from legal_study.models import BBox
from legal_study.pdf.quality import suspicious_char_count, suspicious_token_count


class BookTextStatus(StrEnum):
    NATIVE_VERIFIED = "NATIVE_VERIFIED"
    OCR_VERIFIED = "OCR_VERIFIED"
    VISUALLY_REPAIRED = "VISUALLY_REPAIRED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNRESOLVED = "UNRESOLVED"


class BookTextRecord(BaseModel):
    id: str
    raw_text: str
    canonical_text: str | None
    pdf_page: int
    printed_page: int | None = None
    bbox: BBox
    source_method: str
    ocr_evidence_id: str | None = None
    status: BookTextStatus
    reason: str
    evidence: list[str] = Field(default_factory=list)


_BROKEN_GLYPH_RE = re.compile(r"(?:半[|｜丨][｣」]|[|｜丨][｣」]|[|｜丨]{2,})")
_PROTECTED_RE = re.compile(
    r"[0-9０-９]|第\s*[一二三四五六七八九十百千]+(?:条|項|号)|"
    r"(?:昭和|平成|令和|最判|大判|百選|事件)|[年月日条項号]"
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def corruption_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    if _BROKEN_GLYPH_RE.search(text):
        reasons.append("broken_glyph_sequence")
    if suspicious_char_count(text):
        reasons.append("suspicious_unicode_mapping")
    if suspicious_token_count(text):
        reasons.append("suspicious_latin_token")
    return reasons


def should_route_surgical_ocr(text: str, bbox: BBox, zones: PageZones) -> bool:
    return bool(corruption_reasons(text)) and "main_body" in zones_for_bbox(zones, bbox)


def reconcile_text_record(
    *,
    raw_text: str,
    ocr_text: str | None,
    ocr_confidence: float | None,
    pdf_page: int,
    printed_page: int | None,
    bbox: BBox,
    ocr_evidence_id: str | None = None,
    record_id: str | None = None,
) -> BookTextRecord:
    reasons = corruption_reasons(raw_text)
    item_id = record_id or f"p{pdf_page:04d}-{round(bbox.y0):04d}-{round(bbox.x0):04d}"
    if not reasons:
        if (
            ocr_text
            and _norm(raw_text) != _norm(ocr_text)
            and (_PROTECTED_RE.search(raw_text) or _PROTECTED_RE.search(ocr_text))
        ):
            return BookTextRecord(
                id=item_id,
                raw_text=raw_text,
                canonical_text=None,
                pdf_page=pdf_page,
                printed_page=printed_page,
                bbox=bbox,
                source_method="native_plus_surgical_ocr",
                ocr_evidence_id=ocr_evidence_id,
                status=BookTextStatus.NEEDS_REVIEW,
                reason="protected_numeric_citation_or_named_authority",
            )
        return BookTextRecord(
            id=item_id,
            raw_text=raw_text,
            canonical_text=raw_text,
            pdf_page=pdf_page,
            printed_page=printed_page,
            bbox=bbox,
            source_method="native",
            ocr_evidence_id=ocr_evidence_id,
            status=BookTextStatus.NATIVE_VERIFIED,
            reason="native_text_passed_zoned_quality_checks",
        )
    if not ocr_text or ocr_confidence is None:
        return BookTextRecord(
            id=item_id,
            raw_text=raw_text,
            canonical_text=None,
            pdf_page=pdf_page,
            printed_page=printed_page,
            bbox=bbox,
            source_method="native_plus_missing_surgical_ocr",
            ocr_evidence_id=ocr_evidence_id,
            status=BookTextStatus.UNRESOLVED,
            reason=";".join(reasons + ["ocr_evidence_unavailable"]),
        )

    native = _norm(raw_text)
    candidate = _norm(ocr_text)
    protected = bool(_PROTECTED_RE.search(raw_text) or _PROTECTED_RE.search(ocr_text))
    similarity = SequenceMatcher(None, native, candidate).ratio() if native and candidate else 0.0
    candidate_clean = not corruption_reasons(ocr_text)
    length_ratio = len(candidate) / max(len(native), 1)
    safe = (
        not protected
        and ocr_confidence >= 0.80
        and candidate_clean
        and 0.55 <= length_ratio <= 1.55
        and similarity >= 0.68
    )
    if safe:
        return BookTextRecord(
            id=item_id,
            raw_text=raw_text,
            canonical_text=ocr_text,
            pdf_page=pdf_page,
            printed_page=printed_page,
            bbox=bbox,
            source_method="native_plus_surgical_ocr",
            ocr_evidence_id=ocr_evidence_id,
            status=BookTextStatus.VISUALLY_REPAIRED,
            reason="suspicious_native_replaced_by_clean_coordinate_crop_ocr",
            evidence=[f"similarity={similarity:.6f}", f"ocr_confidence={ocr_confidence:.6f}"],
        )
    return BookTextRecord(
        id=item_id,
        raw_text=raw_text,
        canonical_text=None,
        pdf_page=pdf_page,
        printed_page=printed_page,
        bbox=bbox,
        source_method="native_plus_surgical_ocr",
        ocr_evidence_id=ocr_evidence_id,
        status=BookTextStatus.NEEDS_REVIEW,
        reason=(
            "protected_numeric_citation_or_named_authority"
            if protected
            else "surgical_ocr_did_not_meet_conservative_repair_gate"
        ),
        evidence=[f"similarity={similarity:.6f}", f"ocr_confidence={ocr_confidence:.6f}"],
    )
