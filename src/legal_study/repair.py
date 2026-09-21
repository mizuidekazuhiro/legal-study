from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel, Field

from legal_study.models import DocumentInspection, PageInspection, TextLayerTrust
from legal_study.pdf.quality import suspicious_char_count, suspicious_token_count
from legal_study.reconciliation import ReconciliationRecord, ReconciliationResult


class RepairStatus(StrEnum):
    AUTO_REPAIRED = "AUTO_REPAIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNCHANGED = "UNCHANGED"


class RepairDecision(BaseModel):
    id: str
    page_number: int
    source_record_id: str
    native_original: str
    ocr_original: str
    repair_candidate: str | None = None
    status: RepairStatus
    reason: str
    similarity: float | None = None
    coordinate_match: bool | None = None
    full_page_support: bool | None = None
    protected_content: bool = False
    ocr_confidence: float | None = None


class PageRepairResult(BaseModel):
    page_number: int
    text_layer_trust: str
    text_layer_origin: str
    embedded_text: str
    reconciled_text: str
    full_page_ocr_text: str | None = None
    decisions: list[RepairDecision] = Field(default_factory=list)
    auto_repaired_count: int = 0
    review_required_count: int = 0


class RepairResult(BaseModel):
    schema_version: int = 1
    source_sha256: str
    pages: list[PageRepairResult] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


_PROTECTED_RE = re.compile(
    r"(?:第\s*[0-9０-９一二三四五六七八九十百]+(?:条|項|号)?)|"
    r"(?:[0-9０-９一二三四五六七八九十百]+(?:条|項|号|年|月|日))|"
    r"最判|大判|判例|事件|昭和|平成|令和"
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _contains_protected_content(native: str, candidate: str) -> bool:
    if _PROTECTED_RE.search(native) or _PROTECTED_RE.search(candidate):
        return True
    # Circled / parenthesized numbers normalize to ASCII digits.
    return any(char.isdigit() for char in unicodedata.normalize("NFKC", candidate))


def _trim_ocr_candidate(native: str, ocr: str) -> str:
    """Project the native span boundaries onto a slightly wider OCR line.

    Surgical crops intentionally include context.  Use matching blocks to remove
    obvious prefix/suffix context before deciding whether OCR can repair a span.
    """
    native_n = unicodedata.normalize("NFKC", native).strip()
    ocr_n = unicodedata.normalize("NFKC", ocr).strip()
    if not native_n or not ocr_n:
        return ocr_n
    matcher = SequenceMatcher(None, native_n, ocr_n)
    blocks = [block for block in matcher.get_matching_blocks() if block.size >= 2]
    if not blocks:
        return ocr_n
    first = blocks[0]
    last = blocks[-1]
    start = max(0, first.b - first.a)
    native_tail = len(native_n) - (last.a + last.size)
    end = min(len(ocr_n), last.b + last.size + native_tail)
    if end <= start:
        return ocr_n
    candidate = ocr_n[start:end].strip()
    return candidate or ocr_n


def _full_page_support(candidate: str, full_page_ocr: str | None) -> bool | None:
    if not full_page_ocr:
        return None
    compact = _norm(candidate)
    full = _norm(full_page_ocr)
    return bool(compact) and compact in full


def _decision_for_record(
    page: PageInspection,
    record: ReconciliationRecord,
    full_page_ocr: str | None,
) -> RepairDecision:
    native = record.native_original or ""
    ocr = record.ocr_original or ""
    candidate = _trim_ocr_candidate(native, ocr) if native and ocr else None
    if not native or not ocr or candidate is None:
        return RepairDecision(
            id=f"{record.id}-repair",
            page_number=page.page_number,
            source_record_id=record.id,
            native_original=native,
            ocr_original=ocr,
            repair_candidate=candidate,
            status=RepairStatus.REVIEW_REQUIRED,
            reason="missing_native_or_ocr_candidate",
            coordinate_match=record.coordinate_match,
            ocr_confidence=record.ocr_confidence,
        )

    native_norm = _norm(native)
    candidate_norm = _norm(candidate)
    similarity = SequenceMatcher(None, native_norm, candidate_norm).ratio()
    native_suspicious = suspicious_char_count(native) + suspicious_token_count(native)
    candidate_suspicious = suspicious_char_count(candidate) + suspicious_token_count(candidate)
    protected = _contains_protected_content(native, candidate)
    support = _full_page_support(candidate, full_page_ocr)
    length_ratio = len(candidate_norm) / max(len(native_norm), 1)
    confidence_ok = record.ocr_confidence is not None and record.ocr_confidence >= 0.80
    structural_ok = (
        record.coordinate_match is True
        and 0.55 <= length_ratio <= 1.55
        and similarity >= 0.68
        and native_suspicious > 0
        and candidate_suspicious < native_suspicious
        and confidence_ok
    )
    if page.text_layer_trust == TextLayerTrust.LOW:
        corroborated = support is True
    else:
        corroborated = support is True or (support is None and similarity >= 0.88)

    if protected:
        status = RepairStatus.REVIEW_REQUIRED
        reason = "protected_legal_or_numeric_content"
    elif structural_ok and corroborated:
        status = RepairStatus.AUTO_REPAIRED
        reason = "ocr_geometry_context_and_independent_page_ocr_agree"
    else:
        status = RepairStatus.REVIEW_REQUIRED
        reasons: list[str] = []
        if record.coordinate_match is not True:
            reasons.append("coordinate_mismatch")
        if native_suspicious <= candidate_suspicious:
            reasons.append("ocr_does_not_reduce_suspicious_content")
        if similarity < 0.68:
            reasons.append("low_context_similarity")
        if not confidence_ok:
            reasons.append("low_ocr_confidence")
        if page.text_layer_trust == TextLayerTrust.LOW and support is not True:
            reasons.append("no_full_page_corroboration")
        reason = ";".join(reasons) or "repair_not_safe_to_auto_apply"

    return RepairDecision(
        id=f"{record.id}-repair",
        page_number=page.page_number,
        source_record_id=record.id,
        native_original=native,
        ocr_original=ocr,
        repair_candidate=candidate,
        status=status,
        reason=reason,
        similarity=round(similarity, 6),
        coordinate_match=record.coordinate_match,
        full_page_support=support,
        protected_content=protected,
        ocr_confidence=record.ocr_confidence,
    )


def _apply_repairs(text: str, decisions: list[RepairDecision]) -> tuple[str, list[RepairDecision]]:
    replacements: list[tuple[int, int, str, RepairDecision]] = []
    adjusted: list[RepairDecision] = []
    for decision in decisions:
        if decision.status != RepairStatus.AUTO_REPAIRED or not decision.repair_candidate:
            adjusted.append(decision)
            continue
        target = decision.native_original
        if text.count(target) != 1:
            stripped = target.strip()
            if not stripped or text.count(stripped) != 1:
                adjusted.append(
                    decision.model_copy(
                        update={
                            "status": RepairStatus.REVIEW_REQUIRED,
                            "reason": "native_span_not_unique_in_page_text",
                        }
                    )
                )
                continue
            target = stripped
        start = text.index(target)
        replacements.append(
            (start, start + len(target), decision.repair_candidate, decision)
        )
        adjusted.append(decision)

    reconciled = text
    for start, end, replacement, _decision in sorted(
        replacements, key=lambda item: item[0], reverse=True
    ):
        reconciled = reconciled[:start] + replacement + reconciled[end:]
    return reconciled, adjusted


def build_repairs(
    inspection: DocumentInspection,
    reconciliation: ReconciliationResult,
) -> RepairResult:
    records_by_page: dict[int, list[ReconciliationRecord]] = {}
    full_page_ocr: dict[int, str] = {}
    for record in reconciliation.records:
        records_by_page.setdefault(record.page_number, []).append(record)
        if record.source_kind == "full_page" and record.ocr_original:
            full_page_ocr[record.page_number] = record.ocr_original

    pages: list[PageRepairResult] = []
    totals = {status.value: 0 for status in RepairStatus}
    for page in inspection.pages:
        decisions = [
            _decision_for_record(page, record, full_page_ocr.get(page.page_number))
            for record in records_by_page.get(page.page_number, [])
            if record.source_kind == "suspect_native_text"
        ]
        reconciled, decisions = _apply_repairs(page.native_text, decisions)
        for decision in decisions:
            totals[decision.status.value] += 1
        pages.append(
            PageRepairResult(
                page_number=page.page_number,
                text_layer_trust=page.text_layer_trust.value,
                text_layer_origin=page.text_layer_origin.value,
                embedded_text=page.native_text,
                reconciled_text=reconciled,
                full_page_ocr_text=full_page_ocr.get(page.page_number),
                decisions=decisions,
                auto_repaired_count=sum(
                    item.status == RepairStatus.AUTO_REPAIRED for item in decisions
                ),
                review_required_count=sum(
                    item.status == RepairStatus.REVIEW_REQUIRED for item in decisions
                ),
            )
        )
    return RepairResult(
        source_sha256=inspection.sha256,
        pages=pages,
        counts=totals,
    )
