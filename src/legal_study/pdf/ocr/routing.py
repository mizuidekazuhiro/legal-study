from __future__ import annotations

from dataclasses import asdict, dataclass

from legal_study.models import PageInspection, RawImageRegion

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

    def __post_init__(self) -> None:
        for name in ("full_page_dpi", "image_region_dpi", "surgical_dpi"):
            value = getattr(self, name)
            if value not in SUPPORTED_DPI:
                supported = ", ".join(str(item) for item in sorted(SUPPORTED_DPI))
                raise ValueError(f"{name} must be one of {supported}; got {value}")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


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


def routed_image_regions(
    page: PageInspection, config: OcrRoutingConfig
) -> list[tuple[RawImageRegion, str]]:
    routed: list[tuple[RawImageRegion, str]] = []
    for region in page.raw_image_regions:
        reason = image_region_ocr_reason(
            region,
            page_width=page.width,
            page_height=page.height,
            config=config,
        )
        if reason is not None:
            routed.append((region, reason))
    return routed
