from legal_study.marker_text_alignment import align_markers_to_canonical_pages


def _marker(
    *,
    text: str | None,
    bbox: list[float],
    status: str = "NEEDS_REVIEW",
) -> dict[str, object]:
    return {
        "id": "p0001-logical-mark-001",
        "page_number": 1,
        "color": "yellow",
        "bbox": bbox,
        "exact_text": text,
        "start_char": 0,
        "end_char": max(len(text or "") - 1, 0),
        "review_status": status,
        "reason": "legacy_native_boundary",
    }


def _full_page_ocr(text: str, lines: list[tuple[str, list[float]]]) -> dict[str, object]:
    return {
        "pages": {
            "1": {
                "full_page": {
                    "status": "completed",
                    "result": {
                        "text": text,
                        "confidence": 0.99,
                        "lines": [
                            {
                                "text": line,
                                "confidence": 0.99,
                                "pdf_bbox": {
                                    "x0": bbox[0],
                                    "y0": bbox[1],
                                    "x1": bbox[2],
                                    "y1": bbox[3],
                                },
                            }
                            for line, bbox in lines
                        ],
                    },
                }
            }
        }
    }


def test_corrupt_native_marker_references_independent_ocr_text_without_guessing() -> None:
    pages = [
        {
            "page_number": 1,
            "text_layer_trust": "low",
            "reconciled_text": "\x01" * 8,
        }
    ]
    markers = [_marker(text="\x01" * 4, bbox=[10, 10, 45, 20])]
    ocr = _full_page_ocr("（甲）重要な本文。\n次の行", [("（甲）重要な本文。", [0, 8, 100, 22])])

    aligned = align_markers_to_canonical_pages(pages, markers, ocr)

    page = pages[0]
    assert page["canonical_text"] == "（甲）重要な本文。\n次の行"
    assert page["canonical_text_source"] == "independent_full_page_ocr"
    assert "\x01" not in (aligned[0].get("exact_text") or "")
    assert aligned[0]["text_reference"]["page_number"] == 1
    assert aligned[0]["text_reference"]["text_sha256"] == page["canonical_text_sha256"]
    assert aligned[0]["review_status"] == "NEEDS_REVIEW"
    assert aligned[0]["reason"] == "ocr_line_geometry_cannot_prove_partial_character_boundary"


def test_valid_native_marker_uses_exclusive_canonical_range() -> None:
    pages = [
        {
            "page_number": 1,
            "text_layer_trust": "high",
            "reconciled_text": "前文（重要）後文",
        }
    ]
    markers = [_marker(text="（重要）", bbox=[10, 10, 50, 20])]

    aligned = align_markers_to_canonical_pages(pages, markers, {"pages": {}})

    marker = aligned[0]
    start = marker["canonical_start_char"]
    end = marker["canonical_end_char_exclusive"]
    assert pages[0]["canonical_text"][start:end] == marker["exact_text"] == "（重要）"
    assert marker["character_range_semantics"] == "page_unicode_codepoints_end_exclusive"
    assert marker["review_status"] == "AUTO_VERIFIED"


def test_repeated_text_without_geometric_character_boundary_stays_review() -> None:
    pages = [{"page_number": 1, "text_layer_trust": "low", "reconciled_text": "\x01" * 4}]
    markers = [_marker(text=None, bbox=[50, 10, 80, 20])]
    ocr = _full_page_ocr("同文、同文。", [("同文、同文。", [0, 8, 100, 22])])

    aligned = align_markers_to_canonical_pages(pages, markers, ocr)

    assert aligned[0]["exact_text"] is None
    assert aligned[0]["canonical_start_char"] is None
    assert aligned[0]["canonical_end_char_exclusive"] is None
    assert aligned[0]["review_status"] == "NEEDS_REVIEW"


def test_full_ocr_line_coverage_can_reference_punctuation_exactly() -> None:
    pages = [{"page_number": 1, "text_layer_trust": "low", "reconciled_text": "\x01" * 4}]
    markers = [_marker(text=None, bbox=[0, 8, 100, 22])]
    ocr = _full_page_ocr("（全部）。\n次段", [("（全部）。", [0, 8, 100, 22])])

    aligned = align_markers_to_canonical_pages(pages, markers, ocr)

    marker = aligned[0]
    assert marker["exact_text"] == "（全部）。"
    assert (
        pages[0]["canonical_text"][
            marker["canonical_start_char"] : marker["canonical_end_char_exclusive"]
        ]
        == marker["exact_text"]
    )
    assert marker["review_status"] == "NEEDS_REVIEW"
    assert marker["position_status"] == "VERIFIED"
    assert marker["text_accuracy_status"] == "NEEDS_REVIEW"


def test_multicolumn_ocr_preserves_engine_reading_order() -> None:
    pages = [{"page_number": 1, "text_layer_trust": "low", "reconciled_text": "\x01" * 4}]
    markers = [_marker(text=None, bbox=[200, 0, 300, 20])]
    ocr = _full_page_ocr(
        "右段\n左段",
        [("右段", [200, 0, 300, 20]), ("左段", [0, 0, 100, 20])],
    )

    aligned = align_markers_to_canonical_pages(pages, markers, ocr)

    assert pages[0]["canonical_text"] == "右段\n左段"
    assert aligned[0]["exact_text"] == "右段"
    assert aligned[0]["canonical_start_char"] == 0


def test_same_corrupt_native_values_never_auto_verify() -> None:
    pages = [{"page_number": 1, "text_layer_trust": "low", "reconciled_text": "\x01\x01"}]
    markers = [_marker(text="\x01\x01", bbox=[0, 0, 20, 10], status="AUTO_VERIFIED")]
    ocr = _full_page_ocr("正常", [("正常", [0, 0, 20, 10])])

    aligned = align_markers_to_canonical_pages(pages, markers, ocr)

    assert aligned[0]["review_status"] == "NEEDS_REVIEW"
    assert aligned[0]["exact_text"] == "正常"


def test_zero_height_stroke_uses_its_painted_width_for_line_overlap() -> None:
    pages = [{"page_number": 1, "text_layer_trust": "low", "reconciled_text": "\x01" * 2}]
    marker = _marker(text=None, bbox=[0, 15, 40, 15])
    marker.update({"paint": "stroke", "stroke_width": 10.0})
    ocr = _full_page_ocr("対象", [("対象", [0, 10, 40, 20])])

    aligned = align_markers_to_canonical_pages(pages, [marker], ocr)

    assert aligned[0]["exact_text"] == "対象"
    assert aligned[0]["position_status"] == "VERIFIED"
