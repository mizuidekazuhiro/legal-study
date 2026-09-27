from pathlib import Path

from PIL import Image

from legal_study.handoff import _write_marker_crop


def test_marker_crop_rotates_pdf_bbox_into_rendered_page_space(tmp_path: Path) -> None:
    page_path = tmp_path / "rotated-page.png"
    page = Image.new("RGB", (200, 100), "white")
    # Unrotated [10, 20, 30, 40] becomes rendered [160, 10, 180, 30] at 90 degrees.
    for x in range(160, 180):
        for y in range(10, 30):
            page.putpixel((x, y), (255, 0, 0))
    page.save(page_path)

    output = tmp_path / "crop.png"
    _write_marker_crop(
        page_path=page_path,
        page_width=200,
        page_height=100,
        page_rotation=90,
        marker={"bbox": [10, 20, 30, 40], "paint": "fill"},
        output=output,
    )

    with Image.open(output) as crop:
        colors = crop.convert("RGB").getcolors(maxcolors=crop.width * crop.height)
        assert colors is not None
        assert any(color == (255, 0, 0) for _count, color in colors)
