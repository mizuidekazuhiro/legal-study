from pathlib import Path

from legal_study.models import DocumentInspection, PageInspection, PageMode
from legal_study.reconciliation import (
    ReviewStatus,
    build_reconciliation,
    normalize_for_comparison,
)


def _inspection() -> DocumentInspection:
    return DocumentInspection(
        source_path=Path("source.pdf"),
        sha256="a" * 64,
        page_count=1,
        pages=[
            PageInspection(
                page_number=1,
                width=200,
                height=200,
                native_text="人 々",
                native_char_count=2,
                native_quality_score=1.0,
                image_coverage=0,
                largest_image_coverage=0,
                drawing_count=0,
                annotation_count=0,
                mode=PageMode.NATIVE,
                ocr_recommended=False,
                vision_review_recommended=False,
            )
        ],
    )


def _evidence(kind: str, native: str | None, ocr: str, status: str = "completed"):
    target = {
        "kind": kind,
        "page_number": 1,
        "bbox": [10, 20, 50, 40],
        "image": "ocr_crops/evidence.png",
    }
    if native is not None:
        target["native_candidate"] = native
        target["native_bbox"] = [12, 22, 48, 38]
    return {
        "status": status,
        "target": target,
        "result": {
            "text": ocr,
            "confidence": 0.95,
            "lines": [
                {
                    "text": ocr,
                    "confidence": 0.95,
                    "pdf_bbox": {"x0": 12, "y0": 22, "x1": 48, "y1": 38},
                }
            ],
        },
    }


def test_normalization_keeps_originals_separate_but_matches_nfkc_and_whitespace() -> None:
    assert normalize_for_comparison("Ａ  B\nC") == "A B C"


def test_surgical_exact_after_normalization_is_auto_verified() -> None:
    ocr = {
        "pages": {
            "1": {
                "regions": [
                    _evidence("suspect_native_text", "Ａ  B", "A B"),
                ]
            }
        }
    }
    result = build_reconciliation(_inspection(), ocr, {"pages": []})
    record = result.records[0]

    assert record.native_original == "Ａ  B"
    assert record.ocr_original == "A B"
    assert record.status == ReviewStatus.AUTO_VERIFIED
    assert record.coordinate_match is True
    assert record.selected_source == "native"


def test_real_mismatch_requires_review() -> None:
    ocr = {
        "pages": {
            "1": {
                "regions": [
                    _evidence("suspect_native_text", "甲は乙", "甲は丙"),
                ]
            }
        }
    }
    result = build_reconciliation(_inspection(), ocr, {"pages": []})

    assert result.records[0].status == ReviewStatus.NEEDS_REVIEW
    assert result.records[0].selected_source is None


def test_ocr_only_image_region_requires_review() -> None:
    ocr = {"pages": {"1": {"regions": [_evidence("image_region", None, "画像内文字")]}}}
    result = build_reconciliation(_inspection(), ocr, {"pages": []})

    assert result.records[0].status == ReviewStatus.NEEDS_REVIEW
    assert result.records[0].selected_source == "ocr_supplement"


def test_not_executed_ocr_is_unresolved() -> None:
    ocr = {
        "pages": {
            "1": {
                "regions": [
                    _evidence(
                        "suspect_native_text",
                        "甲",
                        "",
                        status="not_executed_no_backend",
                    )
                ]
            }
        }
    }
    result = build_reconciliation(_inspection(), ocr, {"pages": []})

    assert result.records[0].status == ReviewStatus.UNRESOLVED


def test_red_review_crop_is_never_auto_interpreted() -> None:
    review = {
        "pages": [
            {
                "page_number": 1,
                "review_crops": [
                    {
                        "bbox": [1, 2, 30, 40],
                        "image": "review_crops/red.png",
                    }
                ],
            }
        ]
    }
    result = build_reconciliation(_inspection(), {"pages": {}}, review)

    assert result.records[0].source_kind == "red_vector_cluster"
    assert result.records[0].status == ReviewStatus.NEEDS_REVIEW


def test_exact_text_with_coordinate_mismatch_requires_review() -> None:
    evidence = _evidence("suspect_native_text", "甲", "甲")
    evidence["result"]["lines"][0]["pdf_bbox"] = {
        "x0": 100,
        "y0": 100,
        "x1": 120,
        "y1": 120,
    }
    ocr = {"pages": {"1": {"regions": [evidence]}}}

    result = build_reconciliation(_inspection(), ocr, {"pages": []})

    assert result.records[0].coordinate_match is False
    assert result.records[0].status == ReviewStatus.NEEDS_REVIEW


def test_surgical_native_contained_in_ocr_can_auto_verify_with_geometry() -> None:
    evidence = _evidence("suspect_native_text", "甲は乙", "前文 甲は乙 後文")
    ocr = {"pages": {"1": {"regions": [evidence]}}}

    result = build_reconciliation(_inspection(), ocr, {"pages": []})

    assert result.records[0].status == ReviewStatus.AUTO_VERIFIED
    assert result.records[0].reason.startswith("normalized_native_contained_in_ocr")


def test_surgical_context_match_ignores_nfkc_whitespace_with_geometry() -> None:
    evidence = _evidence(
        "suspect_native_text",
        "⑶ また、甲に、",
        "(3)また、甲に、故",
    )
    ocr = {"pages": {"1": {"regions": [evidence]}}}

    result = build_reconciliation(_inspection(), ocr, {"pages": []})

    assert result.records[0].status == ReviewStatus.AUTO_VERIFIED
    assert result.records[0].selected_source == "native"
    assert "ignoring_whitespace" in result.records[0].reason
