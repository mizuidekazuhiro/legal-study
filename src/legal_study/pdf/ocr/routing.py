from __future__ import annotations

from dataclasses import asdict, dataclass

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

    def __post_init__(self) -> None:
        for name in ("full_page_dpi", "image_region_dpi", "surgical_dpi"):
            value = getattr(self, name)
            if value not in SUPPORTED_DPI:
                supported = ", ".join(str(item) for item in sorted(SUPPORTED_DPI))
                raise ValueError(f"{name} must be one of {supported}; got {value}")
        if not 0.0 <= self.scan_background_min_coverage <= 1.0:
            raise ValueError("scan_background_min_coverage must be between 0 and 1")

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

    image_regions: list[tuple[RawImageRegion, str]] = []
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
