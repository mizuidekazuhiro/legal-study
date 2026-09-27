from PIL import Image, ImageDraw

from legal_study.book.figures import detect_figure_regions
from legal_study.models import BBox


def test_ruled_figure_is_detected_as_visual_region() -> None:
    image = Image.new("RGB", (600, 840), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 300, 500, 600), outline="black", width=3)
    draw.line((100, 400, 500, 400), fill="black", width=3)
    draw.line((300, 300, 300, 600), fill="black", width=3)

    regions = detect_figure_regions(
        image,
        page_width=600,
        page_height=840,
        body_bbox=BBox(x0=60, y0=70, x1=520, y1=790),
    )

    assert len(regions) == 1
    region = regions[0]
    assert region.x0 <= 100 and region.x1 >= 500
    assert region.y0 <= 300 and region.y1 >= 600


def test_figure_crop_expands_to_connected_content_above_rules() -> None:
    image = Image.new("RGB", (600, 840), "white")
    draw = ImageDraw.Draw(image)
    draw.line((300, 250, 300, 340), fill="black", width=3)
    draw.rectangle((100, 340, 500, 600), outline="black", width=3)
    draw.line((100, 450, 500, 450), fill="black", width=3)

    regions = detect_figure_regions(
        image,
        page_width=600,
        page_height=840,
        body_bbox=BBox(x0=60, y0=70, x1=520, y1=790),
    )

    assert regions[0].y0 <= 250


def test_flowchart_text_layout_is_preserved_as_figure() -> None:
    image = Image.new("RGB", (600, 840), "white")
    boxes = [
        ("A説", BBox(x0=100, y0=200, x1=180, y1=220)),
        ("B説", BBox(x0=350, y0=200, x1=430, y1=220)),
        ("↓", BBox(x0=135, y0=225, x1=145, y1=245)),
        ("＋", BBox(x0=380, y0=225, x1=390, y1=245)),
        ("↓", BBox(x0=135, y0=250, x1=145, y1=270)),
        ("責任", BBox(x0=100, y0=275, x1=180, y1=295)),
        ("違法性", BBox(x0=350, y0=250, x1=430, y1=270)),
        ("結論", BBox(x0=350, y0=275, x1=430, y1=295)),
    ]

    regions = detect_figure_regions(
        image,
        page_width=600,
        page_height=840,
        body_bbox=BBox(x0=60, y0=70, x1=520, y1=790),
        word_boxes=boxes,
    )

    assert len(regions) == 1
    assert regions[0].y0 <= 200 and regions[0].y1 >= 295
