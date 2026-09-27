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

