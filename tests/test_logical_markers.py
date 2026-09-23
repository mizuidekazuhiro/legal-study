from legal_study.models import BBox
from legal_study.problem_packet import build_logical_markers, build_ocr_supplements
from legal_study.reconciliation import (
    ReconciliationRecord,
    ReconciliationResult,
    ReviewStatus,
)


def _marker(
    marker_id: str,
    *,
    start: int,
    text: str,
    x0: float,
    x1: float,
    y: float = 20.0,
    color: str = "yellow",
    raw_id: int,
) -> dict[str, object]:
    chars = [
        {
            "index": start + offset,
            "char": char,
            "bbox": [x0 + offset * 5, y - 5, x0 + (offset + 1) * 5, y + 5],
            "block": 0,
            "line": 0,
            "span": 0,
            "char_in_span": start + offset,
        }
        for offset, char in enumerate(text)
    ]
    return {
        "id": marker_id,
        "page_number": 1,
        "paint": "stroke",
        "width": 7.0,
        "opacity": 0.5,
        "color": color,
        "bbox": [x0, y, x1, y],
        "exact_text": text,
        "word_candidate": None,
        "start_char": start,
        "end_char": start + len(text) - 1,
        "native_chars": chars,
        "boundary_confidence": 0.65,
        "review_status": "NEEDS_REVIEW",
        "reason": "marker_boundary_requires_review",
        "raw_vector_ids": [raw_id],
        "evidence_image": "renders/page-0001.png",
    }


def test_multiple_vector_fragments_form_one_logical_marker() -> None:
    markers = [
        _marker("raw-1", start=0, text="甲乙", x0=10, x1=20, raw_id=3),
        _marker("raw-2", start=2, text="丙丁", x0=20, x1=30, raw_id=4),
    ]

    logical = build_logical_markers(markers)

    assert len(logical) == 1
    assert logical[0]["exact_text"] == "甲乙丙丁"
    assert logical[0]["constituent_marker_ids"] == ["raw-1", "raw-2"]
    assert logical[0]["raw_vector_ids"] == [3, 4]


def test_visually_separate_markers_are_not_merged() -> None:
    markers = [
        _marker("raw-1", start=0, text="甲乙", x0=10, x1=20, y=20, raw_id=3),
        _marker("raw-2", start=2, text="丙丁", x0=20, x1=30, y=40, raw_id=4),
    ]

    assert len(build_logical_markers(markers)) == 2


def test_same_color_with_unmarked_character_gap_is_not_merged() -> None:
    markers = [
        _marker("raw-1", start=0, text="甲乙", x0=10, x1=20, raw_id=3),
        _marker("raw-2", start=3, text="丁戊", x0=25, x1=35, raw_id=4),
    ]

    assert len(build_logical_markers(markers)) == 2


def test_surgical_ocr_is_not_an_ocr_supplement() -> None:
    records = [
        ReconciliationRecord(
            id="surgical",
            page_number=1,
            source_kind="suspect_native_text",
            bbox=BBox(x0=0, y0=0, x1=10, y1=10),
            native_original="甲",
            ocr_original="甲",
            status=ReviewStatus.AUTO_VERIFIED,
            reason="matched",
        ),
        ReconciliationRecord(
            id="image",
            page_number=1,
            source_kind="image_region",
            bbox=BBox(x0=0, y0=0, x1=10, y1=10),
            ocr_original="画像内文字",
            status=ReviewStatus.NEEDS_REVIEW,
            reason="ocr_only_image_region",
        ),
    ]
    reconciliation = ReconciliationResult(
        source_sha256="a" * 64,
        records=records,
        counts={"AUTO_VERIFIED": 1, "NEEDS_REVIEW": 1, "UNRESOLVED": 0},
    )

    supplements = build_ocr_supplements(reconciliation)

    assert [item["id"] for item in supplements] == ["image"]
