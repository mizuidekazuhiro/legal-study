from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from legal_study.io_utils import atomic_write_json, atomic_write_text, file_sha256
from legal_study.models import BBox, DocumentInspection, PageInspection, VectorMark
from legal_study.reconciliation import (
    ReconciliationResult,
    ReviewStatus,
    normalize_for_comparison,
)
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


def build_canonical_source(
    manifest: RunManifest,
    inspection: DocumentInspection,
    reconciliation: ReconciliationResult,
    ocr_payload: dict[str, Any],
) -> dict[str, Any]:
    if inspection.sha256 != manifest.source.sha256:
        raise ValueError("Inspection/source SHA mismatch")
    markers: list[dict[str, Any]] = []
    page_payloads: list[dict[str, Any]] = []
    for page in inspection.pages:
        page_markers = [
            _marker_record(page, mark, index)
            for index, mark in enumerate(
                (item for item in page.vector_marks if item.kind == "marker_candidate"),
                start=1,
            )
        ]
        markers.extend(page_markers)
        page_payloads.append(
            {
                "page_number": page.page_number,
                "native_text": page.native_text,
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

    reconciliation_records = [
        record.model_dump(mode="json") for record in reconciliation.records
    ]
    needs_review: list[dict[str, Any]] = [
        {
            "id": record.id,
            "page_number": record.page_number,
            "source_kind": record.source_kind,
            "bbox": _bbox_values(record.bbox),
            "reason": record.reason,
            "native_candidate": record.native_original,
            "ocr_candidate": record.ocr_original,
            "evidence_image": record.evidence_image,
            "status": record.status.value,
        }
        for record in reconciliation.records
        if record.status != ReviewStatus.AUTO_VERIFIED
    ]
    needs_review.extend(
        {
            "id": marker["id"],
            "page_number": marker["page_number"],
            "source_kind": "marker_boundary",
            "bbox": marker["bbox"],
            "reason": marker["reason"],
            "native_candidate": marker["exact_text"],
            "ocr_candidate": None,
            "evidence_image": marker["evidence_image"],
            "status": marker["review_status"],
        }
        for marker in markers
        if marker["review_status"] != ReviewStatus.AUTO_VERIFIED.value
    )

    ocr_supplements = [
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

    return {
        "schema_version": 1,
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
        "markers": markers,
        "ocr_supplements": ocr_supplements,
        "needs_review": needs_review,
        "provenance": {
            "inspection": "inspection.json",
            "ocr": "ocr.json",
            "review_manifest": "review_manifest.json",
            "reconciliation": "reconciliation.json",
        },
        "ocr_backend": ocr_payload.get("backend"),
    }


def problem_markdown_filename(subject: str, question: str) -> str:
    safe_subject = safe_path_component(subject, fallback="unknown")
    safe_question = safe_path_component(question, fallback="adhoc")
    return f"{safe_subject}_{safe_question}_problem.md"


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
        "# Extracted / Reconciled Text",
        "",
    ]
    for page in pages:
        lines.extend(
            [
                f"## PDF page {page['page_number']}",
                "",
                str(page["native_text"]).rstrip(),
                "",
            ]
        )

    lines.extend(["# PDF Marking Record", ""])
    markers = canonical["markers"]
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
                f"- BBox: {marker['bbox']}",
                f"- Exact text: {marker['exact_text']!r}",
                f"- Word candidate: {marker['word_candidate']!r}",
                f"- Start/end char: {marker['start_char']} / {marker['end_char']}",
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
                f"- BBox: {item['bbox']}",
                f"- Reason: {item['reason']}",
                f"- Status: {item['status']}",
                f"- Evidence image: {item['evidence_image'] or 'none'}",
                f"- Native candidate: {item['native_candidate']!r}",
                f"- OCR candidate: {item['ocr_candidate']!r}",
                "",
            ]
        )

    lines.extend(
        [
            "# Extraction Notes",
            "",
            "- Native PDF text is preserved verbatim; OCR does not replace it automatically.",
            "- OCR and native text remain separate evidence until reconciliation.",
            "- Marker colors are recorded without inferring legal meaning.",
            "- Red vector evidence is never interpreted as a correction automatically.",
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
    checks["ocr_supplements_present"] = all(
        str(item["text"]).rstrip() in markdown
        for item in canonical["ocr_supplements"]
        if item.get("text")
    )
    checks["marker_records_present"] = all(
        str(item["id"]) in markdown for item in canonical["markers"]
    )
    checks["needs_review_records_present"] = all(
        str(item["id"]) in markdown for item in canonical["needs_review"]
    )

    evidence_refs = [
        str(item["evidence_image"])
        for item in canonical["needs_review"]
        if item.get("evidence_image")
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
    provenance_refs = [
        canonical["provenance"].get(key)
        for key in ("inspection", "ocr", "review_manifest", "reconciliation")
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
    else:
        checks["frontmatter_needs_review_count_matches"] = False
        checks["frontmatter_source_sha_matches"] = False

    valid = all(checks.values())
    upload_files = [markdown_path.name, *sorted(set(evidence_refs))]
    return {
        "schema_version": 1,
        "valid": valid,
        "checks": checks,
        "canonical_sha256": file_sha256(run_dir / "canonical_source.json"),
        "markdown_sha256": file_sha256(markdown_path),
        "needs_review_count": len(canonical["needs_review"]),
        "upload_files": upload_files,
    }


def write_problem_packet(
    *,
    run_dir: Path,
    manifest: RunManifest,
    inspection: DocumentInspection,
    reconciliation: ReconciliationResult,
    ocr_payload: dict[str, Any],
) -> tuple[Path, Path, dict[str, Any]]:
    canonical_path = run_dir / "canonical_source.json"
    canonical = build_canonical_source(manifest, inspection, reconciliation, ocr_payload)
    atomic_write_json(canonical_path, canonical)
    markdown_path = run_dir / problem_markdown_filename(manifest.subject, manifest.question)
    atomic_write_text(markdown_path, render_problem_markdown(canonical))
    validation = validate_problem_packet(
        run_dir=run_dir,
        manifest=manifest,
        inspection=inspection,
        canonical=canonical,
        markdown_path=markdown_path,
    )
    atomic_write_json(run_dir / "problem_validation.json", validation)
    if not validation["valid"]:
        raise RuntimeError("Problem packet validation failed")
    return canonical_path, markdown_path, validation
