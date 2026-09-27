from legal_study.book.page_zones import build_page_zones, zones_for_bbox
from legal_study.models import BBox, NativeSpan


def test_binding_margin_noise_is_separate_from_main_body() -> None:
    spans = [
        NativeSpan(text="へ", bbox=BBox(x0=4, y0=y, x1=12, y1=y + 8))
        for y in (90, 210, 330, 450)
    ]
    spans.append(
        NativeSpan(
            text="国家の政策判断により",
            bbox=BBox(x0=110, y0=180, x1=310, y1=198),
        )
    )

    zones = build_page_zones(width=595, height=842, page_number=29, spans=spans)

    assert zones.binding_side == "left"
    assert "binding" in zones_for_bbox(zones, spans[0].bbox)
    assert zones_for_bbox(zones, spans[-1].bbox) == {"main_body"}


def test_binding_side_can_switch_on_facing_page() -> None:
    spans = [
        NativeSpan(text="里", bbox=BBox(x0=575, y0=y, x1=588, y1=y + 9))
        for y in (100, 230, 360, 490)
    ]

    zones = build_page_zones(width=595, height=842, page_number=30, spans=spans)

    assert zones.binding_side == "right"
    assert "binding" in zones_for_bbox(zones, spans[0].bbox)

