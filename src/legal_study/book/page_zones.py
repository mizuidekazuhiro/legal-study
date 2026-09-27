from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from legal_study.models import BBox, NativeSpan, RawImageRegion, RawVectorDrawing

BindingSide = Literal["left", "right"]


@dataclass(frozen=True)
class PageZoneConfig:
    header_height_ratio: float = 0.08
    footer_height_ratio: float = 0.06
    margin_width_ratio: float = 0.10
    annotation_width_ratio: float = 0.14
    binding_width_ratio: float = 0.075


@dataclass(frozen=True)
class PageZones:
    width: float
    height: float
    page_number: int
    binding_side: BindingSide
    boxes: dict[str, BBox] = field(default_factory=dict)


def _center(box: BBox) -> tuple[float, float]:
    return ((box.x0 + box.x1) / 2.0, (box.y0 + box.y1) / 2.0)


def _edge_noise_score(
    spans: list[NativeSpan], *, width: float, side: BindingSide, band_ratio: float
) -> float:
    edge = width * band_ratio
    score = 0.0
    for span in spans:
        x, _ = _center(span.bbox)
        in_band = x <= edge if side == "left" else x >= width - edge
        compact = "".join(span.text.split())
        if in_band and 0 < len(compact) <= 3:
            score += 1.0
    return score


def infer_binding_side(
    *,
    width: float,
    page_number: int,
    spans: list[NativeSpan],
    drawings: list[RawVectorDrawing],
    images: list[RawImageRegion],
    config: PageZoneConfig,
) -> BindingSide:
    """Infer the bound edge while retaining parity as a conservative fallback."""
    left = _edge_noise_score(
        spans, width=width, side="left", band_ratio=config.binding_width_ratio
    )
    right = _edge_noise_score(
        spans, width=width, side="right", band_ratio=config.binding_width_ratio
    )
    edge = width * config.binding_width_ratio
    for item in [*drawings, *images]:
        x, _ = _center(item.rect if isinstance(item, RawVectorDrawing) else item.bbox)
        if x <= edge:
            left += 0.25
        elif x >= width - edge:
            right += 0.25
    if abs(left - right) >= 1.0:
        return "left" if left > right else "right"
    return "left" if page_number % 2 else "right"


def build_page_zones(
    *,
    width: float,
    height: float,
    page_number: int,
    spans: list[NativeSpan] | None = None,
    drawings: list[RawVectorDrawing] | None = None,
    images: list[RawImageRegion] | None = None,
    config: PageZoneConfig | None = None,
) -> PageZones:
    cfg = config or PageZoneConfig()
    span_items = list(spans or [])
    drawing_items = list(drawings or [])
    image_items = list(images or [])
    binding_side = infer_binding_side(
        width=width,
        page_number=page_number,
        spans=span_items,
        drawings=drawing_items,
        images=image_items,
        config=cfg,
    )
    header_bottom = height * cfg.header_height_ratio
    footer_top = height * (1.0 - cfg.footer_height_ratio)
    left_inner = width * cfg.margin_width_ratio
    right_inner = width * (1.0 - cfg.annotation_width_ratio)
    binding_width = width * cfg.binding_width_ratio
    boxes = {
        "header": BBox(x0=0, y0=0, x1=width, y1=header_bottom),
        "footer": BBox(x0=0, y0=footer_top, x1=width, y1=height),
        "main_body": BBox(
            x0=left_inner,
            y0=header_bottom,
            x1=right_inner,
            y1=footer_top,
        ),
        "left_margin": BBox(x0=0, y0=header_bottom, x1=left_inner, y1=footer_top),
        "right_margin": BBox(x0=right_inner, y0=header_bottom, x1=width, y1=footer_top),
        "annotation_memo": BBox(
            x0=right_inner,
            y0=header_bottom,
            x1=width,
            y1=footer_top,
        ),
    }
    boxes["binding"] = (
        BBox(x0=0, y0=0, x1=binding_width, y1=height)
        if binding_side == "left"
        else BBox(x0=width - binding_width, y0=0, x1=width, y1=height)
    )
    outer_start, outer_end = (
        (width * 0.55, width) if binding_side == "left" else (0.0, width * 0.45)
    )
    boxes["printed_page_number"] = BBox(
        x0=outer_start,
        y0=footer_top,
        x1=outer_end,
        y1=height,
    )
    return PageZones(
        width=width,
        height=height,
        page_number=page_number,
        binding_side=binding_side,
        boxes=boxes,
    )


def _contains_point(box: BBox, x: float, y: float) -> bool:
    return box.x0 <= x <= box.x1 and box.y0 <= y <= box.y1


def zones_for_bbox(zones: PageZones, bbox: BBox) -> set[str]:
    x, y = _center(bbox)
    matches: set[str] = set()
    if _contains_point(zones.boxes["binding"], x, y):
        matches.add("binding")
    if _contains_point(zones.boxes["printed_page_number"], x, y):
        matches.add("printed_page_number")
    if _contains_point(zones.boxes["header"], x, y):
        matches.add("header")
    elif _contains_point(zones.boxes["footer"], x, y):
        matches.add("footer")
    elif _contains_point(zones.boxes["main_body"], x, y):
        matches.add("main_body")
    elif _contains_point(zones.boxes["left_margin"], x, y):
        matches.add("left_margin")
    elif _contains_point(zones.boxes["right_margin"], x, y):
        matches.update({"right_margin", "annotation_memo"})
    return matches

