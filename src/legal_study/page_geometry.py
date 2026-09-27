from __future__ import annotations

from collections.abc import Sequence


def pdf_bbox_to_rendered_bbox(
    bbox: Sequence[float],
    *,
    rendered_page_width: float,
    rendered_page_height: float,
    page_rotation: int,
) -> list[float]:
    """Map an unrotated PyMuPDF page bbox into rendered-page coordinates.

    PyMuPDF reports text, drawing and annotation coordinates in unrotated page
    space, while ``Page.get_pixmap()`` respects page rotation.  The stored page
    width and height describe that rendered (``Page.rect``) space.
    """

    if len(bbox) != 4:
        raise ValueError("bbox must contain four values")
    x0, y0, x1, y1 = (float(value) for value in bbox)
    rotation = int(page_rotation) % 360
    if rotation not in {0, 90, 180, 270}:
        raise ValueError(f"Unsupported page rotation: {page_rotation}")

    if rotation == 0:
        points = ((x0, y0), (x1, y1))
    elif rotation == 90:
        unrotated_height = float(rendered_page_width)
        points = ((unrotated_height - y0, x0), (unrotated_height - y1, x1))
    elif rotation == 180:
        unrotated_width = float(rendered_page_width)
        unrotated_height = float(rendered_page_height)
        points = (
            (unrotated_width - x0, unrotated_height - y0),
            (unrotated_width - x1, unrotated_height - y1),
        )
    else:
        unrotated_width = float(rendered_page_height)
        points = ((y0, unrotated_width - x0), (y1, unrotated_width - x1))

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]
