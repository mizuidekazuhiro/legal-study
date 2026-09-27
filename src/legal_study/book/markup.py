from __future__ import annotations

import re
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter
from pydantic import BaseModel, Field

from legal_study.book.text_verification import BookTextStatus
from legal_study.models import BBox
from legal_study.pdf.vector_marks import classify_color_family


class WordBox(BaseModel):
    text: str
    bbox: BBox
    order: int


class MarkupEvidence(BaseModel):
    id: str
    raw_rgb: tuple[float, float, float]
    color_family: str
    kind: str
    bbox: BBox
    linked_text: str | None = None
    source_type: str
    opacity: float | None = None
    status: BookTextStatus = BookTextStatus.NEEDS_REVIEW
    evidence_image: str | None = None


class AnnotationEvidence(BaseModel):
    id: str
    text: str | None = None
    raw_text: str | None = None
    page: int
    bbox: BBox
    color: str | None = None
    raw_rgb: tuple[float, float, float] | None = None
    source_type: str
    status: BookTextStatus
    linked_heading: str | None = None
    evidence: list[str] = Field(default_factory=list)


def _intersection_ratio(inner: BBox, outer: BBox) -> float:
    width = max(0.0, min(inner.x1, outer.x1) - max(inner.x0, outer.x0))
    height = max(0.0, min(inner.y1, outer.y1) - max(inner.y0, outer.y0))
    area = max((inner.x1 - inner.x0) * (inner.y1 - inner.y0), 1e-9)
    return width * height / area


def link_words_to_bbox(
    words: list[WordBox], bbox: BBox, *, min_word_overlap: float = 0.10
) -> str | None:
    selected = [
        word for word in words if _intersection_ratio(word.bbox, bbox) >= min_word_overlap
    ]
    selected.sort(key=lambda item: item.order)
    text = " ".join(item.text for item in selected).strip()
    return text or None


_RANK_RE = re.compile(r"\b([ABC])\s*(\+)?\s*Rank\b", re.IGNORECASE)


def _canonical_rank(text: str | None) -> str | None:
    if not text:
        return None
    match = _RANK_RE.search(text)
    if match is None:
        return None
    return f"{match.group(1).upper()}{'+' if match.group(2) else ''} Rank"


def extract_rank_annotation(
    *,
    raw_text: str | None,
    ocr_text: str | None,
    ocr_confidence: float | None,
    pdf_page: int,
    bbox: BBox,
    linked_heading: str | None = None,
) -> AnnotationEvidence:
    native_rank = _canonical_rank(raw_text)
    ocr_rank = _canonical_rank(ocr_text)
    if native_rank is not None:
        text = native_rank
        status = BookTextStatus.NATIVE_VERIFIED
        source = "pdf_text_object"
    elif ocr_rank is not None and ocr_confidence is not None and ocr_confidence >= 0.80:
        text = ocr_rank
        status = BookTextStatus.OCR_VERIFIED
        source = "annotation_crop_ocr"
    else:
        text = None
        status = BookTextStatus.NEEDS_REVIEW
        source = "annotation_visual_evidence"
    return AnnotationEvidence(
        id=f"p{pdf_page:04d}-rank-{round(bbox.y0):04d}",
        text=text,
        raw_text=raw_text,
        page=pdf_page,
        bbox=bbox,
        color="red",
        source_type=source,
        status=status,
        linked_heading=linked_heading if text is not None else None,
        evidence=(
            [f"ocr_confidence={ocr_confidence:.6f}"]
            if ocr_confidence is not None
            else []
        ),
    )


def _components(mask: Image.Image, *, minimum_pixels: int) -> list[tuple[int, int, int, int, int]]:
    width, height = mask.size
    values = bytearray(mask.tobytes())
    components: list[tuple[int, int, int, int, int]] = []
    for start, value in enumerate(values):
        if not value:
            continue
        values[start] = 0
        queue: deque[int] = deque([start])
        x0 = x1 = start % width
        y0 = y1 = start // width
        count = 0
        while queue:
            index = queue.popleft()
            x = index % width
            y = index // width
            count += 1
            x0, x1 = min(x0, x), max(x1, x)
            y0, y1 = min(y0, y), max(y1, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    adjacent = ny * width + nx
                    if values[adjacent]:
                        values[adjacent] = 0
                        queue.append(adjacent)
        if count >= minimum_pixels:
            components.append((x0, y0, x1 + 1, y1 + 1, count))
    return components


def detect_raster_markup(
    image_path: Path,
    *,
    page_width: float,
    page_height: float,
    words: list[WordBox],
) -> list[MarkupEvidence]:
    """Detect saturated raster marks while retaining raw page color evidence."""
    with Image.open(image_path) as opened:
        original = opened.convert("RGB")
    scale = min(1.0, 700.0 / max(original.width, 1))
    size = (max(1, round(original.width * scale)), max(1, round(original.height * scale)))
    image = original.resize(size, Image.Resampling.BILINEAR)
    pixels = list(image.getdata())
    masks: dict[str, bytearray] = {}
    for index, (red, green, blue) in enumerate(pixels):
        family = classify_color_family((red / 255.0, green / 255.0, blue / 255.0))
        if family is None:
            continue
        masks.setdefault(family, bytearray(len(pixels)))[index] = 255

    output: list[MarkupEvidence] = []
    for family, values in masks.items():
        mask = Image.frombytes("L", size, bytes(values)).filter(ImageFilter.MaxFilter(5))
        for component_index, (x0, y0, x1, y1, count) in enumerate(
            _components(mask, minimum_pixels=18), start=1
        ):
            if x1 - x0 < 3 or y1 - y0 < 2:
                continue
            bbox = BBox(
                x0=x0 / size[0] * page_width,
                y0=y0 / size[1] * page_height,
                x1=x1 / size[0] * page_width,
                y1=y1 / size[1] * page_height,
            )
            linked = link_words_to_bbox(words, bbox)
            is_rank = bool(linked and _RANK_RE.search(linked))
            kind = (
                "highlight"
                if linked
                and not is_rank
                and bbox.y1 - bbox.y0 <= page_height * 0.065
                and bbox.x1 - bbox.x0 >= (bbox.y1 - bbox.y0) * 1.25
                else "annotation"
            )
            samples = [
                pixels[y * size[0] + x]
                for y in range(y0, y1)
                for x in range(x0, x1)
                if values[y * size[0] + x]
            ]
            if not samples:
                continue
            raw_rgb = tuple(
                sum(sample[channel] for sample in samples) / (255.0 * len(samples))
                for channel in range(3)
            )
            output.append(
                MarkupEvidence(
                    id=f"raster-{family}-{component_index:03d}",
                    raw_rgb=raw_rgb,  # type: ignore[arg-type]
                    color_family=family,
                    kind=kind,
                    bbox=bbox,
                    linked_text=linked,
                    source_type="page_raster_palette",
                    status=(
                        BookTextStatus.NATIVE_VERIFIED
                        if linked is not None
                        else BookTextStatus.NEEDS_REVIEW
                    ),
                )
            )
    deduplicated: list[MarkupEvidence] = []
    for item in sorted(
        output,
        key=lambda value: (
            -((value.bbox.x1 - value.bbox.x0) * (value.bbox.y1 - value.bbox.y0)),
            value.id,
        ),
    ):
        if any(
            existing.color_family == item.color_family
            and existing.linked_text == item.linked_text
            and _intersection_ratio(item.bbox, existing.bbox) >= 0.80
            for existing in deduplicated
        ):
            continue
        deduplicated.append(item)
    return sorted(deduplicated, key=lambda item: (item.bbox.y0, item.bbox.x0, item.id))
