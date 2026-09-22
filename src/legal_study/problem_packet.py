from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from legal_study.handoff import create_handoff_review_sheets
from legal_study.io_utils import atomic_write_json, atomic_write_text, file_sha256
from legal_study.models import BBox, DocumentInspection, PageInspection, VectorMark
from legal_study.reconciliation import (
    ReconciliationResult,
    ReviewStatus,
    normalize_for_comparison,
)
from legal_study.repair import RepairResult, RepairStatus
from legal_study.run_manifest import RunManifest
from legal_study.workspace import safe_path_component


def _bbox_values(box: BBox) -> list[float]:
    return [box.x0, box.y0, box.x1, box.y1]


def _iter_native_chars(page: PageInspection) -> list[dict[str, Any]]:
    if page.native_chars:
        return [
            {
                "index": item.index,
                "char": item.char,
                "bbox": _bbox_values(item.bbox),
                "block": item.block,
                "line": item.line,
                "span": item.span,
                "char_in_span": item.char_in_span,
            }
            for item in page.native_chars
        ]

    # P1-B inspection artifacts predate the explicit reading-order native_chars
    # field. Preserve resume compatibility by recovering character geometry from
    # raw_native rather than forcing a 20-minute OCR rerun. The fallback order is
    # geometric and is used only for marker-boundary indexing.
    recovered: list[dict[str, Any]] = []
    blocks = page.raw_native.get("blocks", [])
    if not isinstance(blocks, list):
        return recovered
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            if not isinstance(line, dict):
                continue
            for span_index, span in enumerate(line.get("spans", [])):
                if not isinstance(span, dict):
                    continue
                for char_index, char in enumerate(span.get("chars", [])):
                    if not isinstance(char, dict):
                        continue
                    value = char.get("c")
                    bbox = char.get("bbox")
                    if isinstance(value, str) and isinstance(bbox, list) and len(bbox) == 4:
                        recovered.append(
                            {
                                "char": value,
                                "bbox": [float(item) for item in bbox],
                                "block": block_index,
                                "line": line_index,
                                "span": span_index,
                                "char_in_span": char_index,
                            }
                        )
    recovered.sort(key=lambda item: (round(item["bbox"][1], 1), item["bbox"][0]))
    for index, item in enumerate(recovered):
        item["index"] = index
    return recovered


def _intersects_marker(char_bbox: list[float], mark: VectorMark) -> bool:
    x0, y0, x1, y1 = char_bbox
    width = mark.width or 0.0
    pad = width / 2 + 1.0 if mark.paint == "stroke" else 0.5
    mx0 = mark.rect.x0 - 0.5
    mx1 = mark.rect.x1 + 0.5
    my0 = mark.rect.y0 - pad
    my1 = mark.rect.y1 + pad
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    if mx0 <= cx <= mx1 and my0 <= cy <= my1:
        return True
    ix = max(0.0, min(x1, mx1) - max(x0, mx0))
    iy = max(0.0, min(y1, my1) - max(y0, my0))
    return ix > 0 and iy > 0


def _marker_record(page: PageInspection, mark: VectorMark, index: int) -> dict[str, Any]:
    chars = [char for char in _iter_native_chars(page) if _intersects_marker(char["bbox"], mark)]
    exact_text = "".join(str(char["char"]) for char in chars) or None
    candidate = mark.extracted_text
    compact_exact = "".join(normalize_for_comparison(exact_text).split())
    compact_candidate = "".join(normalize_for_comparison(candidate).split())
    independently_agrees = bool(
        compact_exact and compact_candidate and compact_exact == compact_candidate
    )
    status = ReviewStatus.AUTO_VERIFIED if independently_agrees else ReviewStatus.NEEDS_REVIEW
    return {
        "id": f"p{page.page_number:04d}-mark-{index:03d}",
        "page_number": page.page_number,
        "drawing_index": mark.drawing_index,
        "paint": mark.paint,
        "width": mark.width,
        "opacity": mark.opacity,
        "color": mark.color_name,
        "bbox": _bbox_values(mark.rect),
        "exact_text": exact_text,
        "word_candidate": candidate,
        "start_char": chars[0]["index"] if chars else None,
        "end_char": chars[-1]["index"] if chars else None,
        "native_chars": chars,
        "boundary_confidence": 0.98 if independently_agrees else 0.65 if chars else 0.0,
        "review_status": status.value,
        "reason": (
            "char_geometry_agrees_with_word_candidate"
            if independently_agrees
            else "marker_boundary_requires_review"
        ),
        "raw_vector_ids": [mark.drawing_index],
        "evidence_image": page.rendered_image if status != ReviewStatus.AUTO_VERIFIED else None,
    }


def _marker_paint_band(marker: dict[str, Any]) -> tuple[float, float]:
    x0, y0, x1, y1 = (float(value) for value in marker["bbox"])
    del x0, x1
    if marker.get("paint") == "stroke":
        center = (y0 + y1) / 2
        half_width = max(float(marker.get("width") or 0.0) / 2, 0.5)
        return center - half_width, center + half_width
    return y0, y1


def _marker_char_indexes(marker: dict[str, Any]) -> set[int]:
    return {
        int(char["index"])
        for char in marker.get("native_chars", [])
        if isinstance(char, dict) and isinstance(char.get("index"), int)
    }


def _marker_lines(marker: dict[str, Any]) -> set[tuple[int, int]]:
    return {
        (int(char["block"]), int(char["line"]))
        for char in marker.get("native_chars", [])
        if isinstance(char, dict)
        and isinstance(char.get("block"), int)
        and isinstance(char.get("line"), int)
    }


def _can_merge_marker_fragments(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("page_number") != b.get("page_number") or a.get("color") != b.get("color"):
        return False
    if not (_marker_lines(a) & _marker_lines(b)):
        return False
    a_band = _marker_paint_band(a)
    b_band = _marker_paint_band(b)
    if min(a_band[1], b_band[1]) < max(a_band[0], b_band[0]):
        return False

    a_indexes = _marker_char_indexes(a)
    b_indexes = _marker_char_indexes(b)
    if not a_indexes or not b_indexes:
        return False
    if a_indexes & b_indexes:
        return True

    a_box = [float(value) for value in a["bbox"]]
    b_box = [float(value) for value in b["bbox"]]
    if a_box[2] <= b_box[0]:
        return max(a_indexes) + 1 == min(b_indexes)
    if b_box[2] <= a_box[0]:
        return max(b_indexes) + 1 == min(a_indexes)
    return False


def build_logical_markers(markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate only provably contiguous fragments while retaining raw provenance."""
    parents = list(range(len(markers)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left, marker in enumerate(markers):
        for right in range(left + 1, len(markers)):
            if _can_merge_marker_fragments(marker, markers[right]):
                union(left, right)

    components: dict[int, list[dict[str, Any]]] = {}
    for index, marker in enumerate(markers):
        components.setdefault(find(index), []).append(marker)

    logical: list[dict[str, Any]] = []
    page_counts: dict[int, int] = {}
    for fragments in components.values():
        page_number = int(fragments[0]["page_number"])
        page_counts[page_number] = page_counts.get(page_number, 0) + 1
        native_chars_by_index: dict[int, dict[str, Any]] = {}
        for fragment in fragments:
            for char in fragment.get("native_chars", []):
                if isinstance(char, dict) and isinstance(char.get("index"), int):
                    native_chars_by_index.setdefault(int(char["index"]), char)
        native_chars = [native_chars_by_index[key] for key in sorted(native_chars_by_index)]
        bboxes = [[float(value) for value in fragment["bbox"]] for fragment in fragments]
        union_bbox = [
            min(box[0] for box in bboxes),
            min(box[1] for box in bboxes),
            max(box[2] for box in bboxes),
            max(box[3] for box in bboxes),
        ]
        statuses = {str(fragment["review_status"]) for fragment in fragments}
        status = (
            ReviewStatus.AUTO_VERIFIED.value
            if statuses == {ReviewStatus.AUTO_VERIFIED.value}
            else ReviewStatus.NEEDS_REVIEW.value
        )
        candidates = list(
            dict.fromkeys(
                str(fragment["word_candidate"])
                for fragment in fragments
                if fragment.get("word_candidate")
            )
        )
        raw_vector_ids = sorted(
            {
                int(raw_id)
                for fragment in fragments
                for raw_id in fragment.get("raw_vector_ids", [])
            }
        )
        logical.append(
            {
                "id": f"p{page_number:04d}-logical-mark-{page_counts[page_number]:03d}",
                "page_number": page_number,
                "paint": fragments[0]["paint"],
                "color": fragments[0]["color"],
                "bbox": union_bbox,
                "bbox_list": bboxes,
                "exact_text": "".join(str(char["char"]) for char in native_chars) or None,
                "word_candidates": candidates,
                "start_char": native_chars[0]["index"] if native_chars else None,
                "end_char": native_chars[-1]["index"] if native_chars else None,
                "native_chars": native_chars,
                "boundary_confidence": min(
                    float(fragment["boundary_confidence"]) for fragment in fragments
                ),
                "review_status": status,
                "reason": (
                    "all_fragment_boundaries_auto_verified"
                    if status == ReviewStatus.AUTO_VERIFIED.value
                    else "logical_marker_boundary_requires_review"
                ),
                "constituent_marker_ids": [str(fragment["id"]) for fragment in fragments],
                "raw_vector_ids": raw_vector_ids,
                "evidence_image": next(
                    (
                        str(fragment["evidence_image"])
                        for fragment in fragments
                        if fragment.get("evidence_image")
                    ),
                    None,
                ),
            }
        )
    return logical


def build_ocr_supplements(
    reconciliation: ReconciliationResult,
) -> list[dict[str, Any]]:
    return [
        {
            "id": record.id,
            "page_number": record.page_number,
            "source_kind": record.source_kind,
            "bbox": _bbox_values(record.bbox),
            "text": record.ocr_original,
            "confidence": record.ocr_confidence,
            "status": record.status.value,
            "evidence_image": record.evidence_image,
        }
        for record in reconciliation.records
        if record.ocr_original and record.source_kind in {"image_region", "full_page"}
    ]


def _review_task(
    *,
    page_number: int,
    source_kind: str,
    reason: str,
    issues: list[dict[str, Any]],
    rendered_image: str | None,
) -> dict[str, Any]:
    evidence_images = list(
        dict.fromkeys(
            str(issue["evidence_image"])
            for issue in issues
            if issue.get("evidence_image")
        )
    )
    if rendered_image:
        evidence_images.insert(0, rendered_image)
        evidence_images = list(dict.fromkeys(evidence_images))
    status = (
        ReviewStatus.UNRESOLVED.value
        if any(issue.get("status") == ReviewStatus.UNRESOLVED.value for issue in issues)
        else ReviewStatus.NEEDS_REVIEW.value
    )
    return {
        "id": f"p{page_number:04d}-{source_kind}",
        "page_number": page_number,
        "source_kind": source_kind,
        "bbox": None,
        "reason": reason,
        "native_candidate": None,
        "ocr_candidate": None,
        "evidence_image": rendered_image,
        "source_evidence_images": evidence_images,
        "status": status,
        "issue_count": len(issues),
        "issues": issues,
    }


def build_grouped_needs_review(
    *,
    inspection: DocumentInspection,
    reconciliation: ReconciliationResult,
    repair: RepairResult,
    logical_markers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create page-level review tasks while retaining granular raw provenance."""
    rendered_by_page = {
        page.page_number: page.rendered_image for page in inspection.pages
    }
    reconciliation_by_id = {record.id: record for record in reconciliation.records}
    tasks: list[dict[str, Any]] = []

    repair_issues: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for repair_page in repair.pages:
        for decision in repair_page.decisions:
            if decision.status != RepairStatus.REVIEW_REQUIRED:
                continue
            record = reconciliation_by_id.get(decision.source_record_id)
            repair_issues[repair_page.page_number].append(
                {
                    "id": decision.id,
                    "source_record_id": decision.source_record_id,
                    "bbox": _bbox_values(record.bbox) if record is not None else None,
                    "reason": decision.reason,
                    "native_candidate": decision.native_original,
                    "ocr_candidate": decision.ocr_original,
                    "repair_candidate": decision.repair_candidate,
                    "protected_content": decision.protected_content,
                    "evidence_image": record.evidence_image if record is not None else None,
                    "status": ReviewStatus.NEEDS_REVIEW.value,
                }
            )
    for page_number, issues in sorted(repair_issues.items()):
        tasks.append(
            _review_task(
                page_number=page_number,
                source_kind="text_repair_page",
                reason=f"review_required_text_repairs={len(issues)}",
                issues=issues,
                rendered_image=rendered_by_page.get(page_number),
            )
        )

    image_issues: dict[int, list[dict[str, Any]]] = defaultdict(list)
    generic_issues: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in reconciliation.records:
        if record.status == ReviewStatus.AUTO_VERIFIED:
            continue
        if record.source_kind == "suspect_native_text":
            # Every surgical mismatch is represented by its RepairDecision above.
            continue
        if record.source_kind == "full_page" and record.status != ReviewStatus.UNRESOLVED:
            # Successful full-page OCR is corroborating evidence, not a separate
            # human task. It remains preserved in reconciliation/ocr supplements.
            continue
        issue = {
            "id": record.id,
            "bbox": _bbox_values(record.bbox),
            "reason": record.reason,
            "native_candidate": record.native_original,
            "ocr_candidate": record.ocr_original,
            "evidence_image": record.evidence_image,
            "status": record.status.value,
        }
        if record.source_kind == "image_region":
            image_issues[record.page_number].append(issue)
        elif record.source_kind != "red_vector_cluster":
            generic_issues[record.page_number].append(issue)

    for page_number, issues in sorted(image_issues.items()):
        tasks.append(
            _review_task(
                page_number=page_number,
                source_kind="image_region_page",
                reason=f"ocr_only_image_regions={len(issues)}",
                issues=issues,
                rendered_image=rendered_by_page.get(page_number),
            )
        )
    for page_number, issues in sorted(generic_issues.items()):
        tasks.append(
            _review_task(
                page_number=page_number,
                source_kind="unresolved_evidence_page",
                reason=f"unresolved_evidence_items={len(issues)}",
                issues=issues,
                rendered_image=rendered_by_page.get(page_number),
            )
        )

    visual_issues: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for marker in logical_markers:
        if marker["review_status"] == ReviewStatus.AUTO_VERIFIED.value:
            continue
        page_number = int(marker["page_number"])
        visual_issues[page_number].append(
            {
                "id": marker["id"],
                "kind": "marker_boundary",
                "bbox": marker["bbox"],
                "bbox_list": marker["bbox_list"],
                "reason": marker["reason"],
                "native_candidate": marker["exact_text"],
                "ocr_candidate": None,
                "evidence_image": marker["evidence_image"],
                "status": marker["review_status"],
            }
        )
    for record in reconciliation.records:
        if (
            record.source_kind == "red_vector_cluster"
            and record.status != ReviewStatus.AUTO_VERIFIED
        ):
            visual_issues[record.page_number].append(
                {
                    "id": record.id,
                    "kind": "red_vector_cluster",
                    "bbox": _bbox_values(record.bbox),
                    "reason": record.reason,
                    "native_candidate": None,
                    "ocr_candidate": None,
                    "evidence_image": record.evidence_image,
                    "status": record.status.value,
                }
            )
    for page_number, issues in sorted(visual_issues.items()):
        tasks.append(
            _review_task(
                page_number=page_number,
                source_kind="visual_markup_page",
                reason=f"visual_markup_items={len(issues)}",
                issues=issues,
                rendered_image=rendered_by_page.get(page_number),
            )
        )

    page_tasks: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        page_tasks[int(task["page_number"])].append(task)

    consolidated: list[dict[str, Any]] = []
    for page_number, page_level_tasks in sorted(page_tasks.items()):
        categories = sorted(
            {
                str(task["source_kind"])
                for task in page_level_tasks
                if task.get("source_kind")
            }
        )
        issues: list[dict[str, Any]] = []
        for task in page_level_tasks:
            category = str(task.get("source_kind", "unknown"))
            for issue in task.get("issues", []):
                if not isinstance(issue, dict):
                    continue
                issues.append(
                    {
                        **issue,
                        "review_category": category,
                    }
                )
        consolidated_task = _review_task(
            page_number=page_number,
            source_kind="page_review",
            reason=(
                f"review_items={len(issues)};"
                f"categories={','.join(categories)}"
            ),
            issues=issues,
            rendered_image=rendered_by_page.get(page_number),
        )
        consolidated_task["review_categories"] = categories
        consolidated.append(consolidated_task)

    return consolidated


def build_canonical_source(
    manifest: RunManifest,
    inspection: DocumentInspection,
    reconciliation: ReconciliationResult,
    repair: RepairResult,
    ocr_payload: dict[str, Any],
) -> dict[str, Any]:
    if inspection.sha256 != manifest.source.sha256:
        raise ValueError("Inspection/source SHA mismatch")
    markers: list[dict[str, Any]] = []
    page_payloads: list[dict[str, Any]] = []
    repair_by_page = {page.page_number: page for page in repair.pages}
    for page in inspection.pages:
        page_markers = [
            _marker_record(page, mark, index)
            for index, mark in enumerate(
                (item for item in page.vector_marks if item.kind == "marker_candidate"),
                start=1,
            )
        ]
        markers.extend(page_markers)
        repaired_page = repair_by_page.get(page.page_number)
        page_payloads.append(
            {
                "page_number": page.page_number,
                "embedded_text": page.native_text,
                "native_text": page.native_text,
                "reconciled_text": (
                    repaired_page.reconciled_text if repaired_page else page.native_text
                ),
                "text_layer_trust": page.text_layer_trust.value,
                "text_layer_origin": page.text_layer_origin.value,
                "repair_auto_count": (
                    repaired_page.auto_repaired_count if repaired_page else 0
                ),
                "repair_review_count": (
                    repaired_page.review_required_count if repaired_page else 0
                ),
                "native_quality_score": page.native_quality_score,
                "native_char_count": page.native_char_count,
                "raw_native_char_count": len(_iter_native_chars(page)),
                "annotations": [item.model_dump(mode="json") for item in page.annotations],
                "raw_vector_count": len(page.raw_vector_drawings),
                "raw_image_region_count": len(page.raw_image_regions),
                "marker_ids": [item["id"] for item in page_markers],
                "rendered_image": page.rendered_image,
            }
        )
    logical_markers = build_logical_markers(markers)

    reconciliation_records = [
        record.model_dump(mode="json") for record in reconciliation.records
    ]
    needs_review = build_grouped_needs_review(
        inspection=inspection,
        reconciliation=reconciliation,
        repair=repair,
        logical_markers=logical_markers,
    )
    review_issue_count = sum(int(item.get("issue_count", 0)) for item in needs_review)
    ocr_supplements = build_ocr_supplements(reconciliation)

    return {
        "schema_version": 3,
        "subject": manifest.subject,
        "question": manifest.question,
        "source": {
            "filename": manifest.source.original_filename,
            "sha256": manifest.source.sha256,
            "size": manifest.source.source_size,
            "page_count": inspection.page_count,
            "requested_pages": manifest.requested_pages,
        },
        "extractor": {
            "app_version": manifest.app_version,
            "python_version": manifest.python_version,
            "pymupdf_version": manifest.pymupdf_version,
            "pipeline_config": manifest.pipeline_config,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "pages": page_payloads,
        "reconciliation": {
            "counts": reconciliation.counts,
            "records": reconciliation_records,
        },
        "repair": repair.model_dump(mode="json"),
        "markers": markers,
        "logical_markers": logical_markers,
        "ocr_supplements": ocr_supplements,
        "needs_review": needs_review,
        "review_issue_count": review_issue_count,
        "provenance": {
            "inspection": "inspection.json",
            "ocr": "ocr.json",
            "review_manifest": "review_manifest.json",
            "reconciliation": "reconciliation.json",
            "repair": "repair.json",
        },
        "ocr_backend": ocr_payload.get("backend"),
    }


def problem_markdown_filename(subject: str, question: str) -> str:
    safe_subject = safe_path_component(subject, fallback="unknown")
    safe_question = safe_path_component(question, fallback="adhoc")
    return f"{safe_subject}_{safe_question}_problem.md"


def handoff_markdown_filename(subject: str, question: str) -> str:
    safe_subject = safe_path_component(subject, fallback="unknown")
    safe_question = safe_path_component(question, fallback="adhoc")
    return f"{safe_subject}_{safe_question}_handoff.md"


def _yaml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_problem_markdown(canonical: dict[str, Any]) -> str:
    source = canonical["source"]
    pages = canonical["pages"]
    needs_review = canonical["needs_review"]
    lines = [
        "---",
        f"schema_version: {canonical['schema_version']}",
        f"subject: {_yaml_value(canonical['subject'])}",
        f"question: {_yaml_value(canonical['question'])}",
        f"source_file: {_yaml_value(source['filename'])}",
        f"source_sha256: {_yaml_value(source['sha256'])}",
        f"source_pages: {_yaml_value(source['requested_pages'])}",
        f"verification_status: {_yaml_value('needs_review' if needs_review else 'verified')}",
        f"needs_review_count: {len(needs_review)}",
        f"review_issue_count: {int(canonical.get('review_issue_count', len(needs_review)))}",
        f"extractor_version: {_yaml_value(canonical['extractor']['app_version'])}",
        "---",
        "",
        "# Source Manifest",
        "",
        f"- Source: {source['filename']}",
        f"- SHA-256: {source['sha256']}",
        f"- PDF page count: {source['page_count']}",
        f"- Requested pages: {source['requested_pages']}",
        "",
        "# Reconciled Text",
        "",
    ]
    for page in pages:
        lines.extend(
            [
                f"## PDF page {page['page_number']}",
                "",
                f"- Text-layer trust: {page['text_layer_trust']}",
                f"- Text-layer origin: {page['text_layer_origin']}",
                f"- Auto repairs: {page['repair_auto_count']}",
                f"- Repair review: {page['repair_review_count']}",
                "",
                str(page["reconciled_text"]).rstrip(),
                "",
            ]
        )

    lines.extend(["# Embedded Text Evidence", ""])
    for page in pages:
        lines.extend(
            [
                f"## Embedded PDF page {page['page_number']}",
                "",
                str(page["embedded_text"]).rstrip(),
                "",
            ]
        )

    lines.extend(["# PDF Marking Record", ""])
    markers = canonical["logical_markers"]
    if not markers:
        lines.extend(["No classified marker candidates.", ""])
    for marker in markers:
        lines.extend(
            [
                f"## {marker['id']}",
                "",
                f"- Page: {marker['page_number']}",
                f"- Color: {marker['color']}",
                f"- Paint: {marker['paint']}",
                f"- Union BBox: {marker['bbox']}",
                f"- Fragment BBoxes: {marker['bbox_list']}",
                f"- Exact text: {marker['exact_text']!r}",
                f"- Word candidates: {marker['word_candidates']!r}",
                f"- Start/end char: {marker['start_char']} / {marker['end_char']}",
                f"- Raw vector IDs: {marker['raw_vector_ids']}",
                f"- Constituent marker IDs: {marker['constituent_marker_ids']}",
                f"- Boundary confidence: {marker['boundary_confidence']}",
                f"- Review status: {marker['review_status']}",
                "",
            ]
        )

    lines.extend(["# OCR Supplements", ""])
    supplements = canonical["ocr_supplements"]
    if not supplements:
        lines.extend(["No OCR-only/full-page supplements.", ""])
    for item in supplements:
        lines.extend(
            [
                f"## {item['id']}",
                "",
                f"- Page: {item['page_number']}",
                f"- Source kind: {item['source_kind']}",
                f"- BBox: {item['bbox']}",
                f"- OCR confidence: {item['confidence']}",
                f"- Review status: {item['status']}",
                "",
                str(item["text"]).rstrip(),
                "",
            ]
        )

    lines.extend(["# Needs Review", ""])
    if not needs_review:
        lines.extend(["No unresolved or review-required evidence.", ""])
    for item in needs_review:
        lines.extend(
            [
                f"## {item['id']}",
                "",
                f"- Page: {item['page_number']}",
                f"- Source kind: {item['source_kind']}",
                f"- Reason: {item['reason']}",
                f"- Status: {item['status']}",
                f"- Issue count: {item.get('issue_count', 1)}",
                (
                    f"- Review sheet: {item.get('handoff_evidence_image') or item.get('evidence_image') or 'none'}"
                ),
                "",
            ]
        )
        for issue in item.get("issues", []):
            lines.extend(
                [
                    f"### {issue.get('id', 'review-item')}",
                    "",
                    f"- BBox: {issue.get('bbox')}",
                    f"- Reason: {issue.get('reason')}",
                    f"- Native candidate: {issue.get('native_candidate')!r}",
                    f"- OCR candidate: {issue.get('ocr_candidate')!r}",
                    f"- Repair candidate: {issue.get('repair_candidate')!r}",
                    f"- Source evidence: {issue.get('evidence_image') or 'none'}",
                    "",
                ]
            )

    lines.extend(
        [
            "# Extraction Notes",
            "",
            "- Embedded PDF text is preserved verbatim as audit evidence.",
            "- Reconciled text may contain only provenance-recorded AUTO_REPAIRED substitutions.",
            "- Numeric/legal citations and ambiguous repairs remain review-required.",
            "- OCR and embedded text remain separate evidence in canonical artifacts.",
            "- Marker colors are recorded without inferring legal meaning.",
            "- Red vector evidence is never interpreted as a correction automatically.",
            "",
        ]
    )
    return "\n".join(lines)


def render_handoff_markdown(canonical: dict[str, Any]) -> str:
    """Render the compact ChatGPT handoff while keeping audit detail in canonical/problem.md."""
    source = canonical["source"]
    pages = canonical["pages"]
    supplements_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in canonical.get("ocr_supplements", []):
        if isinstance(item, dict) and item.get("page_number") is not None:
            supplements_by_page[int(item["page_number"])].append(item)

    markers_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for marker in canonical.get("logical_markers", []):
        if isinstance(marker, dict) and marker.get("page_number") is not None:
            markers_by_page[int(marker["page_number"])].append(marker)

    reviews_by_page = {
        int(item["page_number"]): item
        for item in canonical.get("needs_review", [])
        if isinstance(item, dict) and item.get("page_number") is not None
    }
    sheets = canonical.get("handoff_review_sheets", {})
    lines = [
        "---",
        "handoff_schema_version: 2",
        f"subject: {_yaml_value(canonical['subject'])}",
        f"question: {_yaml_value(canonical['question'])}",
        f"source_file: {_yaml_value(source['filename'])}",
        f"source_sha256: {_yaml_value(source['sha256'])}",
        f"source_pages: {_yaml_value(source['requested_pages'])}",
        f"page_review_count: {len(canonical.get('needs_review', []))}",
        f"review_issue_count: {int(canonical.get('review_issue_count', 0))}",
        "---",
        "",
        "# Handoff Instructions",
        "",
        "- This file is the compact reading surface. canonical_source.json and *_problem.md retain the full audit trail.",
        "- The review sheet for each page is the visual authority for handwriting, red marks, marker colors, marker boundaries, pasted material, and any disagreement with extracted text.",
        "- On LOW-trust text-layer pages, the primary reading text below is independent full-page OCR; the corrupt embedded/reconciled layer is intentionally omitted from this compact handoff.",
        "- On HIGH/MEDIUM-trust pages, the primary reading text is reconciled embedded text; OCR-only pasted/image material is shown separately.",
        "- Statute numbers, dates, quantities, names, and other protected legal/numeric content must still be confirmed visually when a repair remains review-required.",
        "- Marker text snippets are intentionally omitted from this compact handoff when visual review is needed; use the page review sheet for exact color and boundary confirmation.",
        "- Do not infer the legal role of a color or red vector from color alone.",
        "",
        "# Page Reading Pack",
        "",
    ]

    for page in pages:
        page_number = int(page["page_number"])
        review = reviews_by_page.get(page_number)
        sheet = None
        if isinstance(sheets, dict):
            sheet = sheets.get(page_number) or sheets.get(str(page_number))
        supplements = supplements_by_page.get(page_number, [])
        full_page_ocr = next(
            (
                item
                for item in supplements
                if item.get("source_kind") == "full_page" and item.get("text")
            ),
            None,
        )
        low_trust = str(page["text_layer_trust"]).lower() == "low"
        if low_trust and full_page_ocr is not None:
            primary_mode = "independent_full_page_ocr"
            primary_text = str(full_page_ocr["text"]).rstrip()
        elif low_trust:
            primary_mode = "unavailable_requires_visual_review"
            primary_text = "[No independent full-page OCR available. Inspect the review sheet.]"
        else:
            primary_mode = "reconciled_text"
            primary_text = str(page["reconciled_text"]).rstrip()

        lines.extend(
            [
                f"## PDF page {page_number}",
                "",
                f"- Text-layer trust: {page['text_layer_trust']}",
                f"- Text-layer origin: {page['text_layer_origin']}",
                f"- Primary text source: {primary_mode}",
                f"- Auto repairs: {page['repair_auto_count']}",
                f"- Repair review: {page['repair_review_count']}",
                f"- Review sheet: {sheet or 'none'}",
                (
                    f"- Review categories: {review.get('review_categories', [])}"
                    if review
                    else "- Review categories: []"
                ),
                "",
                "### Primary Reading Text",
                "",
                primary_text,
                "",
            ]
        )

        image_supplements = [
            item
            for item in supplements
            if item.get("source_kind") == "image_region" and item.get("text")
        ]
        if image_supplements:
            lines.extend(["### OCR-only Pasted / Image Material", ""])
            for item in image_supplements:
                lines.extend(
                    [
                        f"#### {item['id']}",
                        "",
                        f"- Confidence: {item['confidence']}",
                        f"- Review status: {item['status']}",
                        "",
                        str(item["text"]).rstrip(),
                        "",
                    ]
                )

        markers = markers_by_page.get(page_number, [])
        if markers:
            marker_counts: dict[tuple[str, str], int] = defaultdict(int)
            for marker in markers:
                marker_counts[
                    (str(marker.get("color")), str(marker.get("review_status")))
                ] += 1
            lines.extend(["### PDF Marking Summary", ""])
            for (color, status), count in sorted(marker_counts.items()):
                lines.append(f"- {color} / {status}: {count}")
            lines.extend(
                [
                    "- Exact marker text and boundaries: inspect the page review sheet.",
                    "",
                ]
            )

        if review:
            issues = [
                issue
                for issue in review.get("issues", [])
                if isinstance(issue, dict)
            ]
            text_issues = [
                issue
                for issue in issues
                if issue.get("review_category")
                in {"text_repair_page", "unresolved_evidence_page"}
            ]
            visual_count = sum(
                1
                for issue in issues
                if issue.get("review_category") == "visual_markup_page"
            )
            image_count = sum(
                1
                for issue in issues
                if issue.get("review_category") == "image_region_page"
            )
            lines.extend(["### Review Summary", ""])
            if visual_count:
                lines.append(
                    f"- Visual markup items: {visual_count}; inspect the page review sheet."
                )
            if image_count:
                lines.append(
                    f"- OCR-only image items: {image_count}; inspect the image OCR and review sheet."
                )
            for issue in text_issues:
                lines.append(
                    "- "
                    + f"{issue.get('review_category')} / {issue.get('id')}: "
                    + f"ocr={issue.get('ocr_candidate')!r}; "
                    + f"candidate={issue.get('repair_candidate')!r}; "
                    + f"reason={issue.get('reason')}"
                )
            lines.append("")

    lines.extend(
        [
            "# Completion Gate",
            "",
            "- Do not treat a page as visually verified until its review sheet has been inspected.",
            "- If a review-required text conflict cannot be resolved from the sheet, report it as unresolved rather than guessing.",
            "",
        ]
    )
    return "\n".join(lines)


def _resolve_evidence(run_dir: Path, reference: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute():
        raise ValueError(f"Evidence path must be run-relative: {reference}")
    resolved = (run_dir / relative).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise ValueError(f"Evidence path escapes run directory: {reference}")
    return resolved


def validate_problem_packet(
    *,
    run_dir: Path,
    manifest: RunManifest,
    inspection: DocumentInspection,
    canonical: dict[str, Any],
    markdown_path: Path,
    handoff_path: Path,
) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    checks["source_sha_matches"] = (
        canonical["source"]["sha256"] == manifest.source.sha256 == inspection.sha256
    )
    inspected_pages = [page.page_number for page in inspection.pages]
    checks["page_range_matches"] = (
        canonical["source"]["requested_pages"] == manifest.requested_pages
        and (
            manifest.requested_pages is None
            or inspected_pages == manifest.requested_pages
        )
    )
    markdown_bytes = markdown_path.read_bytes()
    markdown = markdown_bytes.decode("utf-8")
    checks["markdown_utf8"] = True
    handoff_bytes = handoff_path.read_bytes()
    handoff = handoff_bytes.decode("utf-8")
    checks["handoff_markdown_utf8"] = True
    checks["handoff_has_instructions"] = "# Handoff Instructions" in handoff
    checks["handoff_has_page_reading_pack"] = "# Page Reading Pack" in handoff
    checks["handoff_source_sha_present"] = manifest.source.sha256 in handoff
    checks["handoff_reconciled_text_present"] = all(
        str(page["reconciled_text"]).rstrip() in handoff
        for page in canonical["pages"]
    )
    checks["handoff_review_sheets_present"] = all(
        str(reference) in handoff
        for reference in canonical.get("handoff_review_sheets", {}).values()
    )
    if not markdown.startswith("---\n"):
        checks["yaml_parseable"] = False
    else:
        end = markdown.find("\n---\n", 4)
        try:
            frontmatter = yaml.safe_load(markdown[4:end]) if end >= 0 else None
            checks["yaml_parseable"] = isinstance(frontmatter, dict)
        except yaml.YAMLError:
            checks["yaml_parseable"] = False
    checks["native_text_present"] = all(
        page.native_text.rstrip() in markdown for page in inspection.pages
    )
    canonical_pages = {
        int(page["page_number"]): page for page in canonical["pages"]
    }
    checks["reconciled_text_present"] = all(
        str(canonical_pages[page.page_number]["reconciled_text"]).rstrip() in markdown
        for page in inspection.pages
    )
    checks["text_layer_provenance_present"] = all(
        canonical_pages[page.page_number].get("text_layer_trust")
        and canonical_pages[page.page_number].get("text_layer_origin")
        for page in inspection.pages
    )
    repair_payload = canonical.get("repair", {})
    checks["repair_source_sha_matches"] = (
        isinstance(repair_payload, dict)
        and repair_payload.get("source_sha256") == manifest.source.sha256
    )
    auto_repairs_safe = True
    low_trust_auto_repairs_corroborated = True
    protected_never_auto = True
    if isinstance(repair_payload, dict):
        for repair_page in repair_payload.get("pages", []):
            if not isinstance(repair_page, dict):
                auto_repairs_safe = False
                continue
            trust = repair_page.get("text_layer_trust")
            for decision in repair_page.get("decisions", []):
                if not isinstance(decision, dict):
                    auto_repairs_safe = False
                    continue
                if decision.get("status") != "AUTO_REPAIRED":
                    continue
                auto_repairs_safe = auto_repairs_safe and bool(
                    decision.get("source_record_id")
                    and decision.get("native_original")
                    and decision.get("ocr_original")
                    and decision.get("repair_candidate")
                    and decision.get("coordinate_match") is True
                )
                protected_never_auto = protected_never_auto and not bool(
                    decision.get("protected_content")
                )
                if trust == "low":
                    low_trust_auto_repairs_corroborated = (
                        low_trust_auto_repairs_corroborated
                        and decision.get("full_page_support") is True
                    )
    checks["auto_repairs_have_provenance"] = auto_repairs_safe
    checks["protected_content_never_auto_repaired"] = protected_never_auto
    checks["low_trust_auto_repairs_have_full_page_support"] = (
        low_trust_auto_repairs_corroborated
    )
    checks["ocr_supplements_present"] = all(
        str(item["text"]).rstrip() in markdown
        for item in canonical["ocr_supplements"]
        if item.get("text")
    )
    checks["logical_marker_records_present"] = all(
        str(item["id"]) in markdown for item in canonical["logical_markers"]
    )
    checks["needs_review_records_present"] = all(
        str(item["id"]) in markdown for item in canonical["needs_review"]
    )

    evidence_refs = [
        str(item.get("handoff_evidence_image") or item.get("evidence_image"))
        for item in canonical["needs_review"]
        if item.get("handoff_evidence_image") or item.get("evidence_image")
    ]
    evidence_ok = True
    for reference in evidence_refs:
        try:
            evidence_ok = evidence_ok and _resolve_evidence(run_dir, reference).is_file()
        except ValueError:
            evidence_ok = False
    checks["evidence_paths_valid"] = evidence_ok

    page_by_number = {page.page_number: page for page in inspection.pages}
    raw_refs_ok = True
    for marker in canonical["markers"]:
        page = page_by_number.get(int(marker["page_number"]))
        drawing_index = int(marker["drawing_index"])
        raw_refs_ok = raw_refs_ok and page is not None and any(
            raw.drawing_index == drawing_index for raw in page.raw_vector_drawings
        )
    checks["raw_vector_refs_valid"] = raw_refs_ok
    raw_marker_ids = {str(marker["id"]) for marker in canonical["markers"]}
    logical_refs_ok = True
    for marker in canonical["logical_markers"]:
        page = page_by_number.get(int(marker["page_number"]))
        logical_refs_ok = logical_refs_ok and page is not None
        logical_refs_ok = logical_refs_ok and all(
            str(marker_id) in raw_marker_ids
            for marker_id in marker["constituent_marker_ids"]
        )
        logical_refs_ok = logical_refs_ok and all(
            any(raw.drawing_index == int(raw_id) for raw in page.raw_vector_drawings)
            for raw_id in marker["raw_vector_ids"]
        )
    checks["logical_marker_refs_valid"] = logical_refs_ok
    checks["ocr_supplements_exclude_surgical"] = all(
        item["source_kind"] in {"image_region", "full_page"}
        for item in canonical["ocr_supplements"]
    )
    provenance_refs = [
        canonical["provenance"].get(key)
        for key in ("inspection", "ocr", "review_manifest", "reconciliation", "repair")
    ]
    checks["provenance_present"] = all(provenance_refs)
    provenance_files_ok = True
    for reference in provenance_refs:
        if not isinstance(reference, str):
            provenance_files_ok = False
            continue
        try:
            provenance_files_ok = (
                provenance_files_ok and _resolve_evidence(run_dir, reference).is_file()
            )
        except ValueError:
            provenance_files_ok = False
    checks["provenance_files_exist"] = provenance_files_ok

    if checks["yaml_parseable"]:
        checks["frontmatter_needs_review_count_matches"] = (
            int(frontmatter.get("needs_review_count", -1))
            == len(canonical["needs_review"])
        )
        checks["frontmatter_source_sha_matches"] = (
            frontmatter.get("source_sha256") == manifest.source.sha256
        )
        checks["frontmatter_review_issue_count_matches"] = (
            int(frontmatter.get("review_issue_count", -1))
            == int(canonical.get("review_issue_count", len(canonical["needs_review"])))
        )
    else:
        checks["frontmatter_needs_review_count_matches"] = False
        checks["frontmatter_source_sha_matches"] = False
        checks["frontmatter_review_issue_count_matches"] = False

    upload_files = [handoff_path.name, *sorted(set(evidence_refs))]
    checks["upload_files_unique"] = len(upload_files) == len(set(upload_files))
    valid = all(checks.values())
    return {
        "schema_version": 1,
        "valid": valid,
        "checks": checks,
        "canonical_sha256": file_sha256(run_dir / "canonical_source.json"),
        "markdown_sha256": file_sha256(markdown_path),
        "handoff_markdown_sha256": file_sha256(handoff_path),
        "needs_review_count": len(canonical["needs_review"]),
        "review_issue_count": int(
            canonical.get("review_issue_count", len(canonical["needs_review"]))
        ),
        "upload_files": upload_files,
    }


def write_problem_packet(
    *,
    run_dir: Path,
    manifest: RunManifest,
    inspection: DocumentInspection,
    reconciliation: ReconciliationResult,
    repair: RepairResult,
    ocr_payload: dict[str, Any],
) -> tuple[Path, Path, dict[str, Any]]:
    canonical_path = run_dir / "canonical_source.json"
    canonical = build_canonical_source(
        manifest, inspection, reconciliation, repair, ocr_payload
    )
    canonical["handoff_review_sheets"] = create_handoff_review_sheets(
        run_dir=run_dir,
        canonical=canonical,
    )
    atomic_write_json(canonical_path, canonical)
    markdown_path = run_dir / problem_markdown_filename(manifest.subject, manifest.question)
    atomic_write_text(markdown_path, render_problem_markdown(canonical))
    handoff_path = run_dir / handoff_markdown_filename(
        manifest.subject, manifest.question
    )
    atomic_write_text(handoff_path, render_handoff_markdown(canonical))
    validation = validate_problem_packet(
        run_dir=run_dir,
        manifest=manifest,
        inspection=inspection,
        canonical=canonical,
        markdown_path=markdown_path,
        handoff_path=handoff_path,
    )
    atomic_write_json(run_dir / "problem_validation.json", validation)
    if not validation["valid"]:
        raise RuntimeError("Problem packet validation failed")
    return canonical_path, markdown_path, validation
