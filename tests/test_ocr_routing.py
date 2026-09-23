import pytest

from legal_study.models import BBox, RawImageRegion
from legal_study.pdf.ocr.routing import OcrRoutingConfig, image_region_ocr_reason


def _image(*, width_px: int, height_px: int, bbox: BBox) -> RawImageRegion:
    return RawImageRegion(
        image_index=0,
        bbox=bbox,
        raw={"width": width_px, "height": height_px},
    )


def test_substantive_image_region_routes_without_page_text_signal() -> None:
    region = _image(
        width_px=2048,
        height_px=989,
        bbox=BBox(x0=66, y0=255, x1=271, y1=355),
    )

    reason = image_region_ocr_reason(
        region,
        page_width=595,
        page_height=842,
        config=OcrRoutingConfig(),
    )

    assert reason == "substantive_image_region"


@pytest.mark.parametrize(
    ("region", "reason"),
    [
        (
            _image(
                width_px=49,
                height_px=646,
                bbox=BBox(x0=563, y0=50, x1=568, y1=696),
            ),
            "thin decorative strip",
        ),
        (
            _image(
                width_px=2048,
                height_px=989,
                bbox=BBox(x0=10, y0=10, x1=30, y1=20),
            ),
            "tiny PDF placement",
        ),
    ],
)
def test_non_substantive_images_do_not_route(
    region: RawImageRegion, reason: str
) -> None:
    assert (
        image_region_ocr_reason(
            region,
            page_width=595,
            page_height=842,
            config=OcrRoutingConfig(),
        )
        is None
    ), reason


@pytest.mark.parametrize("dpi", [300, 450, 600])
def test_supported_dpi_values_are_configurable(dpi: int) -> None:
    config = OcrRoutingConfig(
        full_page_dpi=dpi,
        image_region_dpi=dpi,
        surgical_dpi=dpi,
    )
    assert config.full_page_dpi == dpi


def test_unsupported_dpi_is_rejected() -> None:
    with pytest.raises(ValueError, match="300, 450, 600"):
        OcrRoutingConfig(surgical_dpi=500)
