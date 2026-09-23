from legal_study.models import BBox
from legal_study.pdf.ocr.base import CoordinateTransform


def test_coordinate_transform_maps_crop_pixels_back_to_pdf_points() -> None:
    transform = CoordinateTransform(
        pixel_to_pdf=(0.12, 0.0, 0.0, 0.12, 10.0, 20.0),
        image_width_px=1000,
        image_height_px=500,
        pdf_bbox=BBox(x0=10.0, y0=20.0, x1=130.0, y1=80.0),
    )

    mapped = transform.map_bbox(BBox(x0=100, y0=50, x1=300, y1=150))

    assert mapped == BBox(x0=22.0, y0=26.0, x1=46.0, y1=38.0)


def test_coordinate_transform_uses_all_affine_corners() -> None:
    transform = CoordinateTransform(
        pixel_to_pdf=(0.0, 0.5, -0.5, 0.0, 100.0, 20.0),
        image_width_px=200,
        image_height_px=100,
        pdf_bbox=BBox(x0=50.0, y0=20.0, x1=100.0, y1=120.0),
        page_rotation=90,
    )

    mapped = transform.map_bbox(BBox(x0=20, y0=10, x1=60, y1=30))

    assert mapped == BBox(x0=85.0, y0=30.0, x1=95.0, y1=50.0)
