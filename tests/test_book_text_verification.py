from legal_study.book.page_zones import build_page_zones
from legal_study.book.text_verification import (
    BookTextStatus,
    corruption_reasons,
    reconcile_text_record,
    should_route_surgical_ocr,
)
from legal_study.models import BBox


def test_corrupted_native_glyph_routes_to_surgical_ocr_and_preserves_raw() -> None:
    raw = "国家の政策半|｣断により"
    bbox = BBox(x0=100, y0=200, x1=340, y1=220)
    zones = build_page_zones(width=595, height=842, page_number=29)

    assert "broken_glyph_sequence" in corruption_reasons(raw)
    assert should_route_surgical_ocr(raw, bbox, zones)

    result = reconcile_text_record(
        raw_text=raw,
        ocr_text="国家の政策判断により",
        ocr_confidence=0.98,
        pdf_page=29,
        printed_page=19,
        bbox=bbox,
        ocr_evidence_id="ocr-p0029-001",
    )

    assert result.raw_text == raw
    assert result.canonical_text == "国家の政策判断により"
    assert result.status == BookTextStatus.VISUALLY_REPAIRED
    assert result.ocr_evidence_id == "ocr-p0029-001"


def test_numeric_or_citation_difference_is_never_auto_repaired() -> None:
    result = reconcile_text_record(
        raw_text="第220条",
        ocr_text="第229条",
        ocr_confidence=0.99,
        pdf_page=29,
        printed_page=19,
        bbox=BBox(x0=100, y0=200, x1=180, y1=220),
        ocr_evidence_id="ocr-p0029-002",
    )

    assert result.raw_text == "第220条"
    assert result.canonical_text is None
    assert result.status == BookTextStatus.NEEDS_REVIEW


def test_unresolved_text_is_not_silently_copied_or_invented() -> None:
    result = reconcile_text_record(
        raw_text="政策半|｣断",
        ocr_text=None,
        ocr_confidence=None,
        pdf_page=29,
        printed_page=19,
        bbox=BBox(x0=100, y0=200, x1=220, y1=220),
    )

    assert result.raw_text == "政策半|｣断"
    assert result.canonical_text is None
    assert result.status == BookTextStatus.UNRESOLVED


def test_multiline_ocr_with_handwriting_is_not_auto_repaired() -> None:
    result = reconcile_text_record(
        raw_text="ii 危険犯とは、法益侵害の危険を生じさせる",
        ocr_text="→人命\n挙動\ni 危険犯とは、法益侵害の危険を生じさせる",
        ocr_confidence=0.97,
        pdf_page=33,
        printed_page=23,
        bbox=BBox(x0=60, y0=300, x1=400, y1=315),
    )

    assert result.canonical_text is None
    assert result.status == BookTextStatus.NEEDS_REVIEW
