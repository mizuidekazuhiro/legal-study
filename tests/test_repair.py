from pathlib import Path

from legal_study.models import (
    BBox,
    DocumentInspection,
    PageInspection,
    PageMode,
    TextLayerOrigin,
    TextLayerTrust,
)
from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.quality import suspicious_token_count
from legal_study.reconciliation import (
    ReconciliationRecord,
    ReconciliationResult,
    ReviewStatus,
)
from legal_study.repair import RepairStatus, build_repairs


def _inspection(native_text: str, *, trust: TextLayerTrust) -> DocumentInspection:
    return DocumentInspection(
        source_path=Path("source.pdf"),
        sha256="a" * 64,
        page_count=1,
        pages=[
            PageInspection(
                page_number=1,
                width=500,
                height=700,
                native_text=native_text,
                native_char_count=len(native_text),
                native_quality_score=0.9,
                suspicious_char_count=1,
                suspicious_char_ratio=0.05,
                text_layer_trust=trust,
                text_layer_origin=TextLayerOrigin.EMBEDDED_OCR_OR_CORRUPT_MAPPING_LIKELY,
                image_coverage=0.0,
                largest_image_coverage=0.0,
                drawing_count=0,
                annotation_count=0,
                mode=PageMode.OCR_REQUIRED,
                ocr_recommended=True,
                vision_review_recommended=True,
            )
        ],
    )


def _record(
    *,
    record_id: str,
    kind: str,
    native: str | None,
    ocr: str | None,
    confidence: float = 0.98,
) -> ReconciliationRecord:
    return ReconciliationRecord(
        id=record_id,
        page_number=1,
        source_kind=kind,
        bbox=BBox(x0=10, y0=10, x1=100, y1=30),
        native_original=native,
        ocr_original=ocr,
        normalized_native=native,
        normalized_ocr=ocr,
        similarity=0.9,
        coordinate_match=True,
        selected_source=None,
        status=ReviewStatus.NEEDS_REVIEW,
        reason="test",
        ocr_confidence=confidence,
    )


def test_low_trust_mapping_noise_routes_full_page_ocr() -> None:
    trust, origin = PdfInspector._text_layer_assessment(
        native_count=1000,
        quality=0.95,
        suspicious_count=15,
        suspicious_tokens=0,
        suspicious_ratio=0.015,
        suspect_region_count=5,
        largest_image_coverage=0.03,
    )
    assert trust == TextLayerTrust.LOW
    assert origin == TextLayerOrigin.EMBEDDED_OCR_OR_CORRUPT_MAPPING_LIKELY


def test_lowercase_helper_tokens_are_suspicious() -> None:
    assert suspicious_token_count("甲は num see Ｖに送付した") == 2
    assert suspicious_token_count("A Rank Stock MEMO") == 0


def test_context_repair_requires_full_page_corroboration_on_low_trust_page() -> None:
    inspection = _inspection("因果関係は᮲件関係がある。", trust=TextLayerTrust.LOW)
    reconciliation = ReconciliationResult(
        source_sha256=inspection.sha256,
        records=[
            _record(
                record_id="p0001-full-001",
                kind="full_page",
                native=None,
                ocr="因果関係は条件関係がある。",
            ),
            _record(
                record_id="p0001-ocr-001",
                kind="suspect_native_text",
                native="᮲件関係",
                ocr="条件関係",
            ),
        ],
        counts={},
    )

    result = build_repairs(inspection, reconciliation)
    page = result.pages[0]
    assert page.reconciled_text == "因果関係は条件関係がある。"
    assert page.decisions[0].status == RepairStatus.AUTO_REPAIRED
    assert page.decisions[0].full_page_support is True


def test_numeric_legal_citation_is_never_auto_repaired() -> None:
    inspection = _inspection("殺人未遂罪（20᮲）が成立する。", trust=TextLayerTrust.LOW)
    reconciliation = ReconciliationResult(
        source_sha256=inspection.sha256,
        records=[
            _record(
                record_id="p0001-full-001",
                kind="full_page",
                native=None,
                ocr="殺人未遂罪（203条）が成立する。",
            ),
            _record(
                record_id="p0001-ocr-001",
                kind="suspect_native_text",
                native="20᮲",
                ocr="203条",
            ),
        ],
        counts={},
    )

    result = build_repairs(inspection, reconciliation)
    decision = result.pages[0].decisions[0]
    assert decision.status == RepairStatus.REVIEW_REQUIRED
    assert decision.protected_content is True
    assert result.pages[0].reconciled_text == "殺人未遂罪（20᮲）が成立する。"


def test_low_trust_repair_without_full_page_support_stays_review() -> None:
    inspection = _inspection("科学的見ᆅから判断する。", trust=TextLayerTrust.LOW)
    reconciliation = ReconciliationResult(
        source_sha256=inspection.sha256,
        records=[
            _record(
                record_id="p0001-ocr-001",
                kind="suspect_native_text",
                native="科学的見ᆅから",
                ocr="科学的見地から",
            )
        ],
        counts={},
    )

    result = build_repairs(inspection, reconciliation)
    assert result.pages[0].decisions[0].status == RepairStatus.REVIEW_REQUIRED
    assert result.pages[0].reconciled_text == "科学的見ᆅから判断する。"


def test_context_repair_keeps_expanding_replacement_at_span_start() -> None:
    inspection = _inspection(
        "判断の♏として、科学的見ᆅから。", trust=TextLayerTrust.LOW
    )
    reconciliation = ReconciliationResult(
        source_sha256=inspection.sha256,
        records=[
            _record(
                record_id="p0001-full-001",
                kind="full_page",
                native=None,
                ocr="判断の基礎として、科学的見地から。",
            ),
            _record(
                record_id="p0001-ocr-001",
                kind="suspect_native_text",
                native="♏として、科学的見ᆅから。",
                ocr="基礎として、科学的見地から。",
            ),
        ],
        counts={},
    )

    result = build_repairs(inspection, reconciliation)
    decision = result.pages[0].decisions[0]
    assert decision.repair_candidate == "基礎として、科学的見地から。"
    assert decision.status == RepairStatus.AUTO_REPAIRED
    assert result.pages[0].reconciled_text == "判断の基礎として、科学的見地から。"


def test_context_repair_trims_verified_leading_page_context() -> None:
    inspection = _inspection("利用行為の㛤ጞ時点である。", trust=TextLayerTrust.LOW)
    reconciliation = ReconciliationResult(
        source_sha256=inspection.sha256,
        records=[
            _record(
                record_id="p0001-full-001",
                kind="full_page",
                native=None,
                ocr="利用行為の開始時点である。",
            ),
            _record(
                record_id="p0001-ocr-001",
                kind="suspect_native_text",
                native="㛤ጞ時点である。",
                ocr="の開始時点である。",
            ),
        ],
        counts={},
    )

    result = build_repairs(inspection, reconciliation)
    decision = result.pages[0].decisions[0]
    assert decision.repair_candidate == "開始時点である。"
    assert decision.status == RepairStatus.AUTO_REPAIRED
    assert result.pages[0].reconciled_text == "利用行為の開始時点である。"


def test_corrupt_article_marker_stays_protected_after_candidate_trimming() -> None:
    inspection = _inspection(
        "殺人罪（᮲前ẁ）が成立する。", trust=TextLayerTrust.LOW
    )
    reconciliation = ReconciliationResult(
        source_sha256=inspection.sha256,
        records=[
            _record(
                record_id="p0001-full-001",
                kind="full_page",
                native=None,
                ocr="殺人罪（211条前段）が成立する。",
            ),
            _record(
                record_id="p0001-ocr-001",
                kind="suspect_native_text",
                native="᮲前ẁ）が成立する。",
                ocr="1条前段）が成立する。",
            ),
        ],
        counts={},
    )

    result = build_repairs(inspection, reconciliation)
    decision = result.pages[0].decisions[0]
    assert decision.status == RepairStatus.REVIEW_REQUIRED
    assert decision.protected_content is True
    assert result.pages[0].reconciled_text == "殺人罪（᮲前ẁ）が成立する。"
