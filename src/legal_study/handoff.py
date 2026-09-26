from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from legal_study.io_utils import atomic_output_path


def _safe_run_path(run_dir: Path, reference: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute():
        raise ValueError(f"Evidence path must be run-relative: {reference}")
    resolved = (run_dir / relative).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise ValueError(f"Evidence path escapes run directory: {reference}")
    return resolved


def _load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _write_marker_crop(
    *,
    page_path: Path,
    page_width: float,
    page_height: float,
    marker: dict[str, Any],
    output: Path,
) -> None:
    values = marker.get("bbox")
    if not isinstance(values, list) or len(values) != 4:
        raise ValueError("Marker bbox must contain four values")
    x0, y0, x1, y1 = (float(value) for value in values)
    stroke_width = float(marker.get("stroke_width") or 0.0)
    paint_padding = stroke_width / 2 if marker.get("paint") == "stroke" else 0.0
    context = max(12.0, paint_padding + 4.0)
    with Image.open(page_path) as source:
        image = source.convert("RGB")
    scale_x = image.width / max(page_width, 1.0)
    scale_y = image.height / max(page_height, 1.0)
    box = (
        max(0, int((x0 - context) * scale_x)),
        max(0, int((y0 - context) * scale_y)),
        min(image.width, int((x1 + context) * scale_x + 0.999)),
        min(image.height, int((y1 + context) * scale_y + 0.999)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("Marker crop is empty after coordinate conversion")
    output.parent.mkdir(parents=True, exist_ok=True)
    with atomic_output_path(output) as temporary:
        image.crop(box).save(temporary, format="PNG", optimize=True)


def _fit_width(image: Image.Image, width: int) -> Image.Image:
    if image.width <= width:
        return image.copy()
    ratio = width / image.width
    return image.resize((width, max(1, round(image.height * ratio))))


def _labeled(image: Image.Image, label: str, width: int) -> Image.Image:
    fitted = _fit_width(image, width)
    header_height = 28
    canvas = Image.new("RGB", (width, fitted.height + header_height), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 7), label, fill="black")
    canvas.paste(fitted, ((width - fitted.width) // 2, header_height))
    return canvas


def _build_sheet(
    *,
    page_number: int,
    page_image: Path,
    evidence: list[tuple[str, Path]],
    output: Path,
) -> None:
    sheet_width = 1800
    gap = 16
    column_gap = 16
    crop_column_width = (sheet_width - column_gap) // 2

    page_panel = _labeled(
        _load_rgb(page_image),
        f"PDF page {page_number} - full-page review context",
        sheet_width,
    )
    crop_panels = [
        _labeled(_load_rgb(path), label, crop_column_width)
        for label, path in evidence
        if path != page_image
    ]

    rows: list[Image.Image] = []
    for index in range(0, len(crop_panels), 2):
        left = crop_panels[index]
        right = crop_panels[index + 1] if index + 1 < len(crop_panels) else None
        row_height = max(left.height, right.height if right is not None else 0)
        row = Image.new("RGB", (sheet_width, row_height), "white")
        row.paste(left, (0, 0))
        if right is not None:
            row.paste(right, (crop_column_width + column_gap, 0))
        rows.append(row)

    total_height = page_panel.height + sum(row.height for row in rows)
    total_height += gap * len(rows)
    sheet = Image.new("RGB", (sheet_width, total_height), "white")
    y = 0
    sheet.paste(page_panel, (0, y))
    y += page_panel.height
    for row in rows:
        y += gap
        sheet.paste(row, (0, y))
        y += row.height

    output.parent.mkdir(parents=True, exist_ok=True)
    with atomic_output_path(output) as temporary:
        ImageOps.exif_transpose(sheet).save(temporary, format="PNG", optimize=True)


def create_handoff_review_sheets(
    *,
    run_dir: Path,
    canonical: dict[str, Any],
) -> dict[int, str]:
    """Collapse many crop files into one review sheet per page.

    Raw provenance remains untouched in reconciliation/repair/marker records.
    This only creates a compact upload surface for human/vision review.
    """
    pages = {
        int(page["page_number"]): page
        for page in canonical.get("pages", [])
        if isinstance(page, dict) and page.get("page_number") is not None
    }
    tasks_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in canonical.get("needs_review", []):
        if isinstance(task, dict) and task.get("page_number") is not None:
            tasks_by_page[int(task["page_number"])].append(task)

    output_refs: dict[int, str] = {}
    for page_number, tasks in tasks_by_page.items():
        page = pages.get(page_number)
        if page is None:
            continue
        page_reference = page.get("rendered_image")
        if not isinstance(page_reference, str):
            continue
        page_path = _safe_run_path(run_dir, page_reference)
        if not page_path.is_file():
            continue

        ordered_refs: list[str] = []
        for task in tasks:
            for key in ("source_evidence_images", "evidence_images"):
                value = task.get(key)
                if isinstance(value, list):
                    ordered_refs.extend(str(item) for item in value if item)
            value = task.get("evidence_image")
            if isinstance(value, str) and value:
                ordered_refs.append(value)

        unique_refs = list(dict.fromkeys(ordered_refs))
        evidence: list[tuple[str, Path]] = []
        for index, reference in enumerate(unique_refs, start=1):
            try:
                path = _safe_run_path(run_dir, reference)
            except ValueError:
                continue
            if path.is_file() and path != page_path:
                evidence.append((f"Evidence {index}: {reference}", path))

        marker_crops: dict[str, str] = {}
        for marker in canonical.get("logical_markers", []):
            if (
                not isinstance(marker, dict)
                or int(marker.get("page_number", -1)) != page_number
                or marker.get("review_status") == "AUTO_VERIFIED"
            ):
                continue
            marker_id = str(marker.get("id") or f"marker-{len(marker_crops) + 1}")
            relative_crop = Path("marker_review") / f"{marker_id}.png"
            crop_path = run_dir / relative_crop
            _write_marker_crop(
                page_path=page_path,
                page_width=float(page.get("page_width") or 1.0),
                page_height=float(page.get("page_height") or 1.0),
                marker=marker,
                output=crop_path,
            )
            evidence.append((f"Marker {marker_id}", crop_path))
            marker_crops[marker_id] = relative_crop.as_posix()
        if marker_crops:
            page["marker_review_crops"] = marker_crops

        relative = Path("handoff_review") / f"page-{page_number:04d}-review.png"
        output = run_dir / relative
        _build_sheet(
            page_number=page_number,
            page_image=page_path,
            evidence=evidence,
            output=output,
        )
        reference = relative.as_posix()
        output_refs[page_number] = reference
        for task in tasks:
            task["handoff_evidence_image"] = reference

    return output_refs
