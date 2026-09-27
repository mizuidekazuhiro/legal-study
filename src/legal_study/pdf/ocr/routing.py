from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import pairwise

from legal_study.models import PageInspection, RawImageRegion, SuspectRegion, TextLayerOrigin

SUPPORTED_DPI = frozenset({300, 450, 600})


@dataclass(frozen=True)
class OcrRoutingConfig:
    full_page_dpi: int = 300
    image_region_dpi: int = 300
    surgical_dpi: int = 450
    surgical_padding_points: float = 6.0
    image_padding_points: float = 0.0
    min_image_width_px: int = 256
    min_image_height_px: int = 128
    min_image_width_points: float = 48.0
    min_image_height_points: float = 24.0
    min_image_page_coverage: float = 0.01
    suppress_surgical_on_scan_like: bool = True
    suppress_scan_background_image_region: bool = True
    scan_background_min_coverage: float = 0.80
    suppress_repetitive_edge_image_regions: bool = True
    edge_noise_band_fraction: float = 0.12
    edge_noise_max_width_fraction: float = 0.12
    edge_noise_max_height_fraction: float = 0.12
    edge_noise_aspect_ratio_min: float = 0.55
    edge_noise_aspect_ratio_max: float = 1.80
    edge_noise_size_tolerance: float = 0.35
    edge_noise_spacing_tolerance: float = 0.35
    edge_noise_min_repetitions: int = 3

    def __post_init__(self) -> None:
        for name in ("full_page_dpi", "image_region_dpi", "surgical_dpi"):
            value = getattr(self, name)
            if value not in SUPPORTED_DPI:
                supported = ", ".join(str(item) for item in sorted(SUPPORTED_DPI))
                raise ValueError(f"{name} must be one of {supported}; got {value}")
        if not 0.0 <= self.scan_background_min_coverage <= 1.0:
            raise ValueError("scan_background_min_coverage must be between 0 and 1")
        for name in (
            "edge_noise_band_fraction",
            "edge_noise_max_width_fraction",
            "edge_noise_max_height_fraction",
            "edge_noise_size_tolerance",
            "edge_noise_spacing_tolerance",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.edge_noise_aspect_ratio_min <= 0.0:
            raise ValueError("edge_noise_aspect_ratio_min must be greater than 0")
        if self.edge_noise_aspect_ratio_max < self.edge_noise_aspect_ratio_min:
            raise ValueError(
                "edge_noise_aspect_ratio_max must be >= edge_noise_aspect_ratio_min"
            )
        if self.edge_noise_min_repetitions < 3:
            raise ValueError("edge_noise_min_repetitions must be at least 3")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class OcrPageProfile:
    page_number: int
    scan_like: bool
    raster_coverage: float
    largest_raster_coverage: float
    native_text_quality: float
    text_layer_trust: str
    text_layer_origin: str
    full_page_ocr: bool
    vision_review_recommended: bool
    annotation_count: int
    vector_mark_count: int
    raw_image_region_count: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class OcrTargetSuppression:
    kind: str
    reason: str
    bbox: tuple[float, float, float, float]
    source_index: int | None = None
    page_coverage: float | None = None
    native_candidate: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "bbox": list(self.bbox),
            "source_index": self.source_index,
            "page_coverage": self.page_coverage,
            "native_candidate": self.native_candidate,
        }


@dataclass(frozen=True)
class OcrTargetPlan:
    profile: OcrPageProfile
    page_width: float
    page_height: float
    full_page_ocr: bool
    surgical_regions: tuple[SuspectRegion, ...]
    image_regions: tuple[tuple[RawImageRegion, str], ...]
    suppressed: tuple[OcrTargetSuppression, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.as_dict(),
            "full_page_ocr": self.full_page_ocr,
            "planned_targets": {
                "surgical_regions": [
                    {
                        "bbox": [
                            region.bbox.x0,
                            region.bbox.y0,
                            region.bbox.x1,
                            region.bbox.y1,
                        ],
                        "reason": region.reason,
                        "native_candidate": region.text,
                    }
                    for region in self.surgical_regions
                ],
                "image_regions": [
                    {
                        "image_index": region.image_index,
                        "xref": region.xref,
                        "digest": region.digest,
                        "bbox": [
                            region.bbox.x0,
                            region.bbox.y0,
                            region.bbox.x1,
                            region.bbox.y1,
                        ],
                        "page_coverage": _region_page_coverage(
                            region,
                            page_width=self.page_width,
                            page_height=self.page_height,
                        ),
                        "reason": reason,
                    }
                    for region, reason in self.image_regions
                ],
            },
            "suppressed_targets": [item.as_dict() for item in self.suppressed],
            "suppressed_target_count": len(self.suppressed),
        }


def image_region_ocr_reason(
    region: RawImageRegion,
    *,
    page_width: float,
    page_height: float,
    config: OcrRoutingConfig,
) -> str | None:
    """Return a routing reason for a substantive image, independent of native text."""
    raw_width = region.raw.get("width")
    raw_height = region.raw.get("height")
    if not isinstance(raw_width, int | float) or not isinstance(raw_height, int | float):
        return None
    width = region.bbox.x1 - region.bbox.x0
    height = region.bbox.y1 - region.bbox.y0
    page_area = max(page_width * page_height, 1.0)
    coverage = max(width, 0.0) * max(height, 0.0) / page_area
    if raw_width < config.min_image_width_px or raw_height < config.min_image_height_px:
        return None
    if width < config.min_image_width_points or height < config.min_image_height_points:
        return None
    if coverage < config.min_image_page_coverage:
        return None
    return "substantive_image_region"


def _region_page_coverage(
    region: RawImageRegion, *, page_width: float, page_height: float
) -> float:
    width = max(region.bbox.x1 - region.bbox.x0, 0.0)
    height = max(region.bbox.y1 - region.bbox.y0, 0.0)
    page_area = max(page_width * page_height, 1.0)
    return width * height / page_area


def _bbox_tuple(region: SuspectRegion | RawImageRegion) -> tuple[float, float, float, float]:
    return (
        region.bbox.x0,
        region.bbox.y0,
        region.bbox.x1,
        region.bbox.y1,
    )


def _bbox_intersects(a: object, b: object) -> bool:
    return not (
        a.x1 <= b.x0
        or b.x1 <= a.x0
        or a.y1 <= b.y0
        or b.y1 <= a.y0
    )


def _protected_visual_overlap(page: PageInspection, region: RawImageRegion) -> bool:
    """Keep OCR when an edge image overlaps known annotation/vector evidence."""
    return any(
        _bbox_intersects(region.bbox, annotation.rect)
        for annotation in page.annotations
    ) or any(
        _bbox_intersects(region.bbox, mark.rect)
        for mark in page.vector_marks
    )


def _edge_noise_side(
    region: RawImageRegion,
    *,
    page_width: float,
    page_height: float,
    config: OcrRoutingConfig,
) -> str | None:
    """Return the page edge for a compact, roughly circular/oval image placement."""
    width = max(region.bbox.x1 - region.bbox.x0, 0.0)
    height = max(region.bbox.y1 - region.bbox.y0, 0.0)
    if width <= 0.0 or height <= 0.0:
        return None
    if width > page_width * config.edge_noise_max_width_fraction:
        return None
    if height > page_height * config.edge_noise_max_height_fraction:
        return None

    aspect_ratio = width / height
    if not config.edge_noise_aspect_ratio_min <= aspect_ratio <= config.edge_noise_aspect_ratio_max:
        return None

    raw_width = region.raw.get("width")
    raw_height = region.raw.get("height")
    if isinstance(raw_width, int | float) and isinstance(raw_height, int | float):
        if raw_width <= 0 or raw_height <= 0:
            return None
        raw_aspect_ratio = float(raw_width) / float(raw_height)
        if not (
            config.edge_noise_aspect_ratio_min
            <= raw_aspect_ratio
            <= config.edge_noise_aspect_ratio_max
        ):
            return None

    edge_band = page_width * config.edge_noise_band_fraction
    if region.bbox.x0 <= edge_band:
        return "left"
    if region.bbox.x1 >= page_width - edge_band:
        return "right"
    return None


def _relative_difference(a: float, b: float) -> float:
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / scale


def _regular_vertical_spacing(
    regions: list[RawImageRegion], *, tolerance: float
) -> bool:
    if len(regions) < 3:
        return False
    centers = sorted((region.bbox.y0 + region.bbox.y1) / 2.0 for region in regions)
    gaps = [b - a for a, b in pairwise(centers)]
    if not gaps or min(gaps) <= 0.0:
        return False
    ordered = sorted(gaps)
    median_gap = ordered[len(ordered) // 2]
    if median_gap <= 0.0:
        return False
    return all(abs(gap - median_gap) / median_gap <= tolerance for gap in gaps)


def repetitive_edge_noise_image_indices(
    page: PageInspection,
    regions: list[RawImageRegion],
    config: OcrRoutingConfig,
) -> set[int]:
    """Identify repeated edge image placements that are likely binder/punch holes.

    This is deliberately conservative: a candidate must be compact, roughly
    circular/oval, near one page edge, repeated at least three times with similar
    size and regular vertical spacing, and must not overlap known annotation or
    vector-mark evidence. Raw evidence is retained; only OCR execution is
    suppressed by the caller.
    """
    if not config.suppress_repetitive_edge_image_regions:
        return set()

    by_side: dict[str, list[RawImageRegion]] = {"left": [], "right": []}
    for region in regions:
        side = _edge_noise_side(
            region,
            page_width=page.width,
            page_height=page.height,
            config=config,
        )
        if side is None or _protected_visual_overlap(page, region):
            continue
        by_side[side].append(region)

    suppressed: set[int] = set()
    for side_regions in by_side.values():
        for seed in side_regions:
            seed_width = seed.bbox.x1 - seed.bbox.x0
            seed_height = seed.bbox.y1 - seed.bbox.y0
            cluster = [
                region
                for region in side_regions
                if _relative_difference(
                    region.bbox.x1 - region.bbox.x0, seed_width
                )
                <= config.edge_noise_size_tolerance
                and _relative_difference(
                    region.bbox.y1 - region.bbox.y0, seed_height
                )
                <= config.edge_noise_size_tolerance
            ]
            if len(cluster) < config.edge_noise_min_repetitions:
                continue
            if not _regular_vertical_spacing(
                cluster, tolerance=config.edge_noise_spacing_tolerance
            ):
                continue
            suppressed.update(region.image_index for region in cluster)

    return suppressed


def page_profile(page: PageInspection) -> OcrPageProfile:
    """Describe page evidence without deciding whether Vision review may be skipped."""
    return OcrPageProfile(
        page_number=page.page_number,
        scan_like=page.text_layer_origin == TextLayerOrigin.SCAN_LIKE,
        raster_coverage=page.image_coverage,
        largest_raster_coverage=page.largest_image_coverage,
        native_text_quality=page.native_quality_score,
        text_layer_trust=page.text_layer_trust.value,
        text_layer_origin=page.text_layer_origin.value,
        full_page_ocr=page.ocr_recommended,
        vision_review_recommended=page.vision_review_recommended,
        annotation_count=page.annotation_count,
        vector_mark_count=len(page.vector_marks),
        raw_image_region_count=len(page.raw_image_regions),
    )


def plan_ocr_targets(page: PageInspection, config: OcrRoutingConfig) -> OcrTargetPlan:
    """Plan OCR once per page while preserving all raw evidence.

    Suppression affects only OCR execution. It never removes annotations, vectors,
    raw image metadata, rendered review images, or the independent Vision-review
    recommendation stored on PageInspection.
    """
    profile = page_profile(page)
    full_page_ocr = profile.full_page_ocr
    suppressed: list[OcrTargetSuppression] = []

    surgical_regions: list[SuspectRegion] = []
    if config.suppress_surgical_on_scan_like and full_page_ocr and profile.scan_like:
        suppressed.extend(
            OcrTargetSuppression(
                kind="suspect_native_text",
                reason="redundant_with_full_page_ocr_scan_like_text_layer",
                bbox=_bbox_tuple(region),
                native_candidate=region.text,
            )
            for region in page.suspect_native_regions
        )
    else:
        surgical_regions.extend(page.suspect_native_regions)

    image_candidates: list[tuple[RawImageRegion, str, float]] = []
    for region in page.raw_image_regions:
        reason = image_region_ocr_reason(
            region,
            page_width=page.width,
            page_height=page.height,
            config=config,
        )
        if reason is None:
            continue
        coverage = _region_page_coverage(
            region, page_width=page.width, page_height=page.height
        )
        image_candidates.append((region, reason, coverage))

    repetitive_edge_noise = repetitive_edge_noise_image_indices(
        page,
        [region for region, _, _ in image_candidates],
        config,
    )

    image_regions: list[tuple[RawImageRegion, str]] = []
    for region, reason, coverage in image_candidates:
        if region.image_index in repetitive_edge_noise:
            suppressed.append(
                OcrTargetSuppression(
                    kind="image_region",
                    reason="repetitive_edge_image_noise_likely_binder_hole",
                    bbox=_bbox_tuple(region),
                    source_index=region.image_index,
                    page_coverage=coverage,
                )
            )
            continue
        if (
            config.suppress_scan_background_image_region
            and full_page_ocr
            and profile.scan_like
            and coverage >= config.scan_background_min_coverage
        ):
            suppressed.append(
                OcrTargetSuppression(
                    kind="image_region",
                    reason="duplicate_of_full_page_ocr_background_image",
                    bbox=_bbox_tuple(region),
                    source_index=region.image_index,
                    page_coverage=coverage,
                )
            )
            continue
        image_regions.append((region, reason))

    return OcrTargetPlan(
        profile=profile,
        page_width=page.width,
        page_height=page.height,
        full_page_ocr=full_page_ocr,
        surgical_regions=tuple(surgical_regions),
        image_regions=tuple(image_regions),
        suppressed=tuple(suppressed),
    )


def routed_suspect_native_regions(
    page: PageInspection, config: OcrRoutingConfig
) -> list[SuspectRegion]:
    """Backward-compatible view of the centralized OCR target plan."""
    return list(plan_ocr_targets(page, config).surgical_regions)


def routed_image_regions(
    page: PageInspection, config: OcrRoutingConfig
) -> list[tuple[RawImageRegion, str]]:
    """Backward-compatible view of the centralized OCR target plan."""
    return list(plan_ocr_targets(page, config).image_regions)
