from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import sqrt

import fitz

from legal_study.models import BBox, VectorMark
from legal_study.pdf.quality import suspicious_char_count


@dataclass(frozen=True)
class KnownColor:
    name: str
    rgb: tuple[float, float, float]


KNOWN_COLORS = (
    KnownColor("yellow", (1.0, 1.0, 0.5137255)),
    KnownColor("blue", (0.3450828, 0.6941177, 1.0)),
    KnownColor("orange", (1.0, 0.7254902, 0.3294118)),
    KnownColor("red", (1.0, 0.1647059, 0.1333181)),
    KnownColor("purple", (0.75, 0.5, 0.95)),
)


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def nearest_color(rgb: tuple[float, float, float] | None, tolerance: float = 0.18) -> str | None:
    if rgb is None:
        return None
    candidate = min(KNOWN_COLORS, key=lambda c: _distance(rgb, c.rgb))
    return candidate.name if _distance(rgb, candidate.rgb) <= tolerance else None


def _bbox(rect: fitz.Rect) -> BBox:
    return BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)


def _extract_words(page: fitz.Page, rect: fitz.Rect, stroke_width: float) -> str | None:
    # Highlighter strokes run through the middle of glyphs. Expanding vertically
    # gives a stable intersection against PDF word boxes while preserving x bounds.
    probe = fitz.Rect(rect)
    probe.y0 -= stroke_width / 2 + 1.0
    probe.y1 += stroke_width / 2 + 1.0

    hits: list[tuple[int, int, int, str]] = []
    for word in page.get_text("words", sort=True):
        x0, y0, x1, y1, text, block_no, line_no, word_no = word[:8]
        wr = fitz.Rect(x0, y0, x1, y1)
        if wr.is_empty:
            continue
        intersection = wr & probe
        if intersection.is_empty:
            continue
        x_overlap = max(0.0, min(wr.x1, probe.x1) - max(wr.x0, probe.x0))
        x_ratio = x_overlap / max(wr.width, 0.1)
        y_center = (wr.y0 + wr.y1) / 2
        if x_ratio >= 0.15 and probe.y0 <= y_center <= probe.y1:
            hits.append((int(block_no), int(line_no), int(word_no), str(text)))

    if not hits:
        return None
    hits.sort(key=lambda x: (x[0], x[1], x[2]))
    # Japanese PDF word extraction often returns phrase-sized tokens. A single
    # space is kept between tokens because exact character-boundary recovery is
    # deferred to the verification stage.
    return " ".join(x[3] for x in hits).strip() or None


def _opacity(value: object) -> float:
    return 1.0 if value is None else float(value)


def extract_vector_marks(
    page: fitz.Page, *, drawings: Sequence[dict[str, object]] | None = None
) -> list[VectorMark]:
    """Classify vector candidates without controlling raw evidence retention.

    PdfInspector stores every drawing separately before this function runs. An
    absent result here therefore means "unclassified", never "discarded".
    """
    marks: list[VectorMark] = []
    source_drawings = drawings if drawings is not None else page.get_drawings()
    for drawing_index, drawing in enumerate(source_drawings):
        rect = fitz.Rect(drawing["rect"])
        width_value = drawing.get("width")
        width = float(width_value) if width_value is not None else None
        paint_values = (
            ("stroke", drawing.get("color"), drawing.get("stroke_opacity")),
            ("fill", drawing.get("fill"), drawing.get("fill_opacity")),
        )
        for paint, color, opacity_value in paint_values:
            if color is None:
                continue
            rgb = tuple(float(value) for value in color)
            if len(rgb) != 3:
                continue
            name = nearest_color(rgb)
            if name is None:
                continue

            if name == "red":
                # Red is deliberately not interpreted as a correction, deletion,
                # person marker, underline, circle, or any other legal meaning.
                marks.append(
                    VectorMark(
                        drawing_index=drawing_index,
                        paint=paint,
                        kind="red_vector_evidence",
                        color_name=name,
                        color_rgb=rgb,
                        rect=_bbox(rect),
                        width=width,
                        opacity=_opacity(opacity_value),
                        confidence=0.99,
                    )
                )
                continue

            marker_stroke = paint == "stroke" and width is not None and width >= 3.0
            marker_fill = paint == "fill"
            if name in {"yellow", "blue", "orange", "purple"} and (
                marker_stroke or marker_fill
            ):
                text = _extract_words(page, rect, width or 0.0)
                marks.append(
                    VectorMark(
                        drawing_index=drawing_index,
                        paint=paint,
                        kind="marker_candidate",
                        color_name=name,
                        color_rgb=rgb,
                        rect=_bbox(rect),
                        width=width,
                        opacity=_opacity(opacity_value),
                        extracted_text=text,
                        confidence=(
                            0.95
                            if text and suspicious_char_count(text) == 0
                            else 0.75
                            if text
                            else 0.70
                        ),
                    )
                )
    return marks


def cluster_red_vector_evidence(
    marks: Iterable[VectorMark], gap: float = 12.0
) -> list[BBox]:
    """Cluster red stroke/fill evidence into crop-sized regions for Vision review."""
    rects = [
        fitz.Rect(m.rect.x0, m.rect.y0, m.rect.x1, m.rect.y1)
        for m in marks
        if m.kind == "red_vector_evidence"
    ]
    clusters: list[fitz.Rect] = []
    for rect in rects:
        expanded = fitz.Rect(rect.x0 - gap, rect.y0 - gap, rect.x1 + gap, rect.y1 + gap)
        merged_index = None
        for i, cluster in enumerate(clusters):
            if not (expanded & cluster).is_empty:
                merged_index = i
                break
        if merged_index is None:
            clusters.append(rect)
        else:
            clusters[merged_index] |= rect

    # One additional merge pass catches transitive stroke groups.
    changed = True
    while changed:
        changed = False
        out: list[fitz.Rect] = []
        while clusters:
            current = clusters.pop()
            expanded = fitz.Rect(
                current.x0 - gap, current.y0 - gap, current.x1 + gap, current.y1 + gap
            )
            hit = None
            for i, other in enumerate(clusters):
                if not (expanded & other).is_empty:
                    hit = i
                    break
            if hit is not None:
                current |= clusters.pop(hit)
                clusters.append(current)
                changed = True
            else:
                out.append(current)
        clusters = out

    return [_bbox(r) for r in sorted(clusters, key=lambda r: (r.y0, r.x0))]
