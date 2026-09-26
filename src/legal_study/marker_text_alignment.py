from __future__ import annotations

import hashlib
from typing import Any

_CONTROL_CHARS = {chr(value) for value in range(32)} - {"\n", "\r", "\t"}
_RANGE_SEMANTICS = "page_unicode_codepoints_end_exclusive"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _full_page_result(ocr_payload: dict[str, Any], page_number: int) -> dict[str, Any] | None:
    pages = ocr_payload.get("pages")
    if not isinstance(pages, dict):
        return None
    page = pages.get(str(page_number))
    if not isinstance(page, dict):
        return None
    evidence = page.get("full_page")
    if not isinstance(evidence, dict) or evidence.get("status") != "completed":
        return None
    result = evidence.get("result")
    return result if isinstance(result, dict) else None


def _contains_bad_control_text(text: str | None) -> bool:
    return bool(text and any(char in _CONTROL_CHARS for char in text))


def _canonical_page_text(
    page: dict[str, Any], ocr_payload: dict[str, Any]
) -> tuple[str, str, dict[str, Any] | None]:
    page_number = int(page["page_number"])
    full_page = _full_page_result(ocr_payload, page_number)
    if str(page.get("text_layer_trust", "")).lower() == "low":
        if full_page is not None:
            text = full_page.get("text")
            if isinstance(text, str) and text:
                return text, "independent_full_page_ocr", full_page
        return "", "unavailable_requires_visual_review", full_page
    return str(page.get("reconciled_text") or ""), "reconciled_text", full_page


def _bbox_values(value: object) -> list[float] | None:
    if isinstance(value, list) and len(value) == 4:
        return [float(item) for item in value]
    if isinstance(value, dict):
        keys = ("x0", "y0", "x1", "y1")
        if all(value.get(key) is not None for key in keys):
            return [float(value[key]) for key in keys]
    return None


def _intersects(left: list[float], right: list[float]) -> bool:
    return min(left[2], right[2]) > max(left[0], right[0]) and min(left[3], right[3]) > max(
        left[1], right[1]
    )


def _covers(outer: list[float], inner: list[float], tolerance: float = 1.0) -> bool:
    return (
        outer[0] <= inner[0] + tolerance
        and outer[1] <= inner[1] + tolerance
        and outer[2] >= inner[2] - tolerance
        and outer[3] >= inner[3] - tolerance
    )


def _paint_bbox(marker: dict[str, Any]) -> list[float] | None:
    bbox = _bbox_values(marker.get("bbox"))
    if bbox is None or marker.get("paint") != "stroke":
        return bbox
    half_width = max(float(marker.get("stroke_width") or 0.0) / 2, 0.5)
    if bbox[3] - bbox[1] < half_width * 2:
        center = (bbox[1] + bbox[3]) / 2
        bbox[1] = center - half_width
        bbox[3] = center + half_width
    return bbox


def _line_ranges(text: str, full_page: dict[str, Any] | None) -> list[dict[str, Any]]:
    if full_page is None or not isinstance(full_page.get("lines"), list):
        return []
    ranges: list[dict[str, Any]] = []
    cursor = 0
    for line in full_page["lines"]:
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            continue
        line_text = line["text"]
        start = text.find(line_text, cursor)
        if start < 0:
            # A non-sequential hit would make repeated text and column order ambiguous.
            continue
        end = start + len(line_text)
        bbox = _bbox_values(line.get("pdf_bbox"))
        if bbox is not None:
            ranges.append({"text": line_text, "start": start, "end": end, "bbox": bbox})
        cursor = end
    return ranges


def _unique_range(text: str, needle: str) -> tuple[int, int] | None:
    start = text.find(needle)
    if start < 0 or text.find(needle, start + 1) >= 0:
        return None
    return start, start + len(needle)


def _set_reference(
    marker: dict[str, Any],
    *,
    page_number: int,
    text_hash: str,
    source: str,
    start: int | None,
    end: int | None,
) -> None:
    marker["canonical_start_char"] = start
    marker["canonical_end_char_exclusive"] = end
    marker["character_range_semantics"] = _RANGE_SEMANTICS
    marker["text_reference"] = {
        "page_number": page_number,
        "field": "canonical_text",
        "text_sha256": text_hash,
        "source": source,
    }


def _align_marker(
    marker: dict[str, Any],
    *,
    page: dict[str, Any],
    canonical_text: str,
    canonical_source: str,
    line_ranges: list[dict[str, Any]],
) -> dict[str, Any]:
    aligned = dict(marker)
    page_number = int(page["page_number"])
    text_hash = str(page["canonical_text_sha256"])
    old_exact = aligned.get("exact_text")
    usable_exact = (
        str(old_exact)
        if isinstance(old_exact, str) and old_exact and not _contains_bad_control_text(old_exact)
        else None
    )
    selected: tuple[int, int] | None = None

    old_start = aligned.get("start_char")
    old_end = aligned.get("end_char")
    if (
        usable_exact is not None
        and isinstance(old_start, int)
        and isinstance(old_end, int)
        and canonical_text[old_start : old_end + 1] == usable_exact
    ):
        selected = (old_start, old_end + 1)
    elif usable_exact is not None:
        selected = _unique_range(canonical_text, usable_exact)

    marker_bbox = _paint_bbox(aligned)
    intersecting = (
        [line for line in line_ranges if _intersects(marker_bbox, line["bbox"])]
        if marker_bbox is not None
        else []
    )
    fully_covered = (
        [line for line in intersecting if _covers(marker_bbox, line["bbox"])]
        if marker_bbox is not None
        else []
    )

    if selected is None and len(intersecting) == 1 and len(fully_covered) == 1:
        line = fully_covered[0]
        selected = (int(line["start"]), int(line["end"]))
        aligned["exact_text"] = canonical_text[selected[0] : selected[1]]
        aligned["position_status"] = "VERIFIED"
        aligned["text_accuracy_status"] = "NEEDS_REVIEW"
        aligned["review_status"] = "NEEDS_REVIEW"
        aligned["reason"] = "ocr_primary_text_requires_independent_accuracy_review"
    elif selected is None:
        aligned["exact_text"] = None
        aligned["position_status"] = "NEEDS_REVIEW"
        aligned["text_accuracy_status"] = "NEEDS_REVIEW"
        aligned["review_status"] = "NEEDS_REVIEW"
        aligned["reason"] = "ocr_line_geometry_cannot_prove_partial_character_boundary"
    else:
        aligned["exact_text"] = canonical_text[selected[0] : selected[1]]
        if canonical_source == "reconciled_text" and not _contains_bad_control_text(
            aligned["exact_text"]
        ):
            aligned["position_status"] = "VERIFIED"
            aligned["text_accuracy_status"] = "VERIFIED"
            aligned["review_status"] = "AUTO_VERIFIED"
            aligned["reason"] = "trusted_canonical_text_and_native_character_geometry_agree"
        else:
            aligned["position_status"] = "VERIFIED"
            aligned["text_accuracy_status"] = "NEEDS_REVIEW"
            aligned["review_status"] = "NEEDS_REVIEW"
            aligned["reason"] = "ocr_primary_text_requires_independent_accuracy_review"

    _set_reference(
        aligned,
        page_number=page_number,
        text_hash=text_hash,
        source=canonical_source,
        start=selected[0] if selected else None,
        end=selected[1] if selected else None,
    )
    return aligned


def align_markers_to_canonical_pages(
    pages: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    ocr_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reference marker ranges to the exact page text handed to ChatGPT.

    New character ranges count Python/Unicode code points within one page and use an
    end-exclusive boundary. Legacy native ``start_char``/``end_char`` fields remain
    untouched as raw evidence and are never reinterpreted as OCR offsets.
    """

    page_context: dict[int, tuple[dict[str, Any], str, str, list[dict[str, Any]]]] = {}
    for page in pages:
        page_number = int(page["page_number"])
        text, source, full_page = _canonical_page_text(page, ocr_payload)
        page["canonical_text"] = text
        page["canonical_text_source"] = source
        page["canonical_text_sha256"] = _sha256_text(text)
        page["canonical_text_range_semantics"] = _RANGE_SEMANTICS
        page_context[page_number] = (page, text, source, _line_ranges(text, full_page))

    aligned: list[dict[str, Any]] = []
    for marker in markers:
        context = page_context.get(int(marker["page_number"]))
        if context is None:
            aligned.append(dict(marker))
            continue
        page, text, source, ranges = context
        aligned.append(
            _align_marker(
                marker,
                page=page,
                canonical_text=text,
                canonical_source=source,
                line_ranges=ranges,
            )
        )
    return aligned
