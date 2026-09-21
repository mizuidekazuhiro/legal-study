from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Iterable

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


def extract_vector_marks(page: fitz.Page) -> list[VectorMark]:
    marks: list[VectorMark] = []
    for drawing in page.get_drawings():
        color = drawing.get("color")
        if color is None:
            continue
        rgb = tuple(float(v) for v in color)
        name = nearest_color(rgb)
        if name is None:
            continue

        width = float(drawing.get("width") or 0.0)
        opacity = float(drawing.get("stroke_opacity") or 1.0)
        rect = fitz.Rect(drawing["rect"])

        if name in {"yellow", "blue", "orange", "purple"} and width >= 3.0:
            text = _extract_words(page, rect, width)
            marks.append(
                VectorMark(
                    kind="highlight_stroke",
                    color_name=name,
                    color_rgb=rgb,
                    rect=_bbox(rect),
                    width=width,
                    opacity=opacity,
                    extracted_text=text,
                    confidence=(
                        0.95 if text and suspicious_char_count(text) == 0
                        else 0.75 if text
                        else 0.70
                    ),
                )
            )
        elif name == "red" and width < 3.0:
            # Red thin strokes are intentionally NOT interpreted as a correction,
            # deletion, person marker, underline, circle, etc. They are evidence
            # for a later crop-based Vision pass.
            marks.append(
                VectorMark(
                    kind="red_pen_stroke",
                    color_name=name,
                    color_rgb=rgb,
                    rect=_bbox(rect),
                    width=width,
                    opacity=opacity,
                    confidence=0.99,
                )
            )
    return marks


def cluster_red_pen_marks(marks: Iterable[VectorMark], gap: float = 12.0) -> list[BBox]:
    """Cluster thin red vector strokes into crop-sized regions for Vision review."""
    rects = [
        fitz.Rect(m.rect.x0, m.rect.y0, m.rect.x1, m.rect.y1)
        for m in marks
        if m.kind == "red_pen_stroke"
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
