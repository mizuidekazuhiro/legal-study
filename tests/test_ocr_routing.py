import pytest

from legal_study.models import (
    BBox,
    PageInspection,
    PageMode,
    RawImageRegion,
    SuspectRegion,
    TextLayerOrigin,
    TextLayerTrust,
)
from legal_study.pdf.ocr.routing import (
    OcrRoutingConfig,
    image_region_ocr_reason,
    plan_ocr_targets,
    routed_image_regions,
    routed_suspect_native_regions,
)


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


def _scan_like_page(*, image_regions: list[RawImageRegion]) -> PageInspection:
    return PageInspection(
        page_number=1,
        width=595,
        height=842,
        native_text="\x01" * 50,
        native_char_count=50,
        native_quality_score=0.1,
        text_layer_trust=TextLayerTrust.LOW,
        text_layer_origin=TextLayerOrigin.SCAN_LIKE,
        suspect_native_regions=[
            SuspectRegion(
                text="\x01",
                bbox=BBox(x0=20, y0=100, x1=40, y1=120),
                reason="suspicious_glyphs=1",
            )
        ],
        image_coverage=1.0,
        largest_image_coverage=1.0,
        drawing_count=0,
        annotation_count=0,
        mode=PageMode.OCR_REQUIRED,
        ocr_recommended=True,
        vision_review_recommended=True,
        raw_image_regions=image_regions,
    )


def test_scan_like_page_suppresses_redundant_surgical_regions() -> None:
    page = _scan_like_page(image_regions=[])

    assert routed_suspect_native_regions(page, OcrRoutingConfig()) == []


def test_scan_like_page_suppresses_full_page_raster_but_keeps_smaller_image() -> None:
    background = _image(
        width_px=2480,
        height_px=3508,
        bbox=BBox(x0=0, y0=0, x1=595, y1=842),
    )
    pasted = _image(
        width_px=1200,
        height_px=800,
        bbox=BBox(x0=80, y0=250, x1=300, y1=400),
    )
    pasted.image_index = 1
    page = _scan_like_page(image_regions=[background, pasted])

    routed = routed_image_regions(page, OcrRoutingConfig())

    assert len(routed) == 1
    assert routed[0][0].image_index == 1
    assert routed[0][1] == "substantive_image_region"


def test_scan_like_noise_suppression_can_be_disabled_for_diagnostics() -> None:
    background = _image(
        width_px=2480,
        height_px=3508,
        bbox=BBox(x0=0, y0=0, x1=595, y1=842),
    )
    page = _scan_like_page(image_regions=[background])
    config = OcrRoutingConfig(
        suppress_surgical_on_scan_like=False,
        suppress_scan_background_image_region=False,
    )

    assert len(routed_suspect_native_regions(page, config)) == 1
    assert len(routed_image_regions(page, config)) == 1


def test_target_plan_records_suppression_and_keeps_vision_review_independent() -> None:
    background = _image(
        width_px=2480,
        height_px=3508,
        bbox=BBox(x0=0, y0=0, x1=595, y1=842),
    )
    pasted = _image(
        width_px=1200,
        height_px=800,
        bbox=BBox(x0=80, y0=250, x1=300, y1=400),
    )
    pasted.image_index = 1
    page = _scan_like_page(image_regions=[background, pasted]).model_copy(
        update={"annotation_count": 1, "vision_review_recommended": True}
    )

    plan = plan_ocr_targets(page, OcrRoutingConfig())
    payload = plan.as_dict()

    assert plan.full_page_ocr is True
    assert len(plan.surgical_regions) == 0
    assert [item[0].image_index for item in plan.image_regions] == [1]
    assert {
        item["reason"] for item in payload["suppressed_targets"]
    } == {
        "redundant_with_full_page_ocr_scan_like_text_layer",
        "duplicate_of_full_page_ocr_background_image",
    }
    assert payload["profile"]["vision_review_recommended"] is True
    assert payload["profile"]["annotation_count"] == 1
    assert payload["planned_targets"]["image_regions"][0]["page_coverage"] < 0.80


def test_target_plan_diagnostic_mode_preserves_all_ocr_candidates() -> None:
    background = _image(
        width_px=2480,
        height_px=3508,
        bbox=BBox(x0=0, y0=0, x1=595, y1=842),
    )
    page = _scan_like_page(image_regions=[background])
    config = OcrRoutingConfig(
        suppress_surgical_on_scan_like=False,
        suppress_scan_background_image_region=False,
    )

    plan = plan_ocr_targets(page, config)

    assert len(plan.surgical_regions) == 1
    assert len(plan.image_regions) == 1
    assert plan.suppressed == ()


def _born_digital_page(*, image_regions: list[RawImageRegion]) -> PageInspection:
    return PageInspection(
        page_number=1,
        width=595,
        height=842,
        native_text="刑法の本文",
        native_char_count=5,
        native_quality_score=1.0,
        text_layer_trust=TextLayerTrust.HIGH,
        text_layer_origin=TextLayerOrigin.BORN_DIGITAL_LIKELY,
        image_coverage=0.10,
        largest_image_coverage=0.05,
        drawing_count=0,
        annotation_count=0,
        mode=PageMode.HYBRID,
        ocr_recommended=False,
        vision_review_recommended=True,
        raw_image_regions=image_regions,
    )


def _edge_hole(*, image_index: int, y0: float) -> RawImageRegion:
    return RawImageRegion(
        image_index=image_index,
        bbox=BBox(x0=520, y0=y0, x1=590, y1=y0 + 80),
        raw={"width": 320, "height": 320},
    )


def test_repetitive_edge_holes_are_suppressed_but_raw_evidence_is_retained() -> None:
    holes = [
        _edge_hole(image_index=1, y0=80),
        _edge_hole(image_index=2, y0=260),
        _edge_hole(image_index=3, y0=440),
        _edge_hole(image_index=4, y0=620),
    ]
    substantive = _image(
        width_px=1200,
        height_px=800,
        bbox=BBox(x0=180, y0=300, x1=400, y1=450),
    )
    substantive.image_index = 10
    page = _born_digital_page(image_regions=[*holes, substantive])

    plan = plan_ocr_targets(page, OcrRoutingConfig())
    payload = plan.as_dict()

    assert [item[0].image_index for item in plan.image_regions] == [10]
    assert {
        item.source_index
        for item in plan.suppressed
        if item.reason == "repetitive_edge_image_noise_likely_binder_hole"
    } == {1, 2, 3, 4}
    assert [region.image_index for region in page.raw_image_regions] == [1, 2, 3, 4, 10]
    assert payload["profile"]["raw_image_region_count"] == 5


def test_irregular_edge_images_are_not_suppressed_as_binder_holes() -> None:
    images = [
        _edge_hole(image_index=1, y0=80),
        _edge_hole(image_index=2, y0=260),
        _edge_hole(image_index=3, y0=600),
    ]
    page = _born_digital_page(image_regions=images)

    plan = plan_ocr_targets(page, OcrRoutingConfig())

    assert [item[0].image_index for item in plan.image_regions] == [1, 2, 3]
    assert not any(
        item.reason == "repetitive_edge_image_noise_likely_binder_hole"
        for item in plan.suppressed
    )


def test_repetitive_edge_suppression_can_be_disabled_for_diagnostics() -> None:
    holes = [
        _edge_hole(image_index=1, y0=80),
        _edge_hole(image_index=2, y0=260),
        _edge_hole(image_index=3, y0=440),
    ]
    page = _born_digital_page(image_regions=holes)
    config = OcrRoutingConfig(suppress_repetitive_edge_image_regions=False)

    plan = plan_ocr_targets(page, config)

    assert [item[0].image_index for item in plan.image_regions] == [1, 2, 3]
    assert plan.suppressed == ()
