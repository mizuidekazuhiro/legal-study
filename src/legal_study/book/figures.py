from __future__ import annotations

from PIL import Image

from legal_study.models import BBox


def _longest_dark_run(row: list[int], threshold: int) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    start: int | None = None
    for index, value in enumerate([*row, 255]):
        if value < threshold and start is None:
            start = index
        elif value >= threshold and start is not None:
            if best is None or index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    return best


def _overlap_ratio(left: tuple[int, int], right: tuple[int, int]) -> float:
    overlap = max(0, min(left[1], right[1]) - max(left[0], right[0]))
    return overlap / max(min(left[1] - left[0], right[1] - right[0]), 1)


def detect_figure_regions(
    image: Image.Image,
    *,
    page_width: float,
    page_height: float,
    body_bbox: BBox,
) -> list[BBox]:
    """Detect ruled diagrams/tables so they remain images rather than linear OCR text."""
    target_width = min(700, image.width)
    scale = target_width / image.width
    resized = image.convert("L").resize(
        (target_width, max(1, round(image.height * scale))), Image.Resampling.BILINEAR
    )
    x0 = max(0, round(body_bbox.x0 / page_width * resized.width))
    x1 = min(resized.width, round(body_bbox.x1 / page_width * resized.width))
    y0 = max(0, round(body_bbox.y0 / page_height * resized.height))
    y1 = min(resized.height, round(body_bbox.y1 / page_height * resized.height))
    minimum_run = max(20, round((x1 - x0) * 0.35))
    candidates: list[tuple[int, int, int]] = []
    pixels = resized.load()
    for y in range(y0, y1):
        run = _longest_dark_run([pixels[x, y] for x in range(x0, x1)], 90)
        if run is not None and run[1] - run[0] >= minimum_run:
            candidates.append((y, x0 + run[0], x0 + run[1]))

    lines: list[tuple[int, int, int]] = []
    for y, left, right in candidates:
        if lines and y <= lines[-1][0] + 2 and _overlap_ratio(
            (left, right), (lines[-1][1], lines[-1][2])
        ) >= 0.5:
            previous = lines[-1]
            lines[-1] = (y, min(previous[1], left), max(previous[2], right))
        else:
            lines.append((y, left, right))

    clusters: list[list[tuple[int, int, int]]] = []
    maximum_gap = round((y1 - y0) * 0.45)
    for line in lines:
        if not clusters:
            clusters.append([line])
            continue
        previous = clusters[-1][-1]
        if line[0] - previous[0] <= maximum_gap and _overlap_ratio(
            (line[1], line[2]), (previous[1], previous[2])
        ) >= 0.45:
            clusters[-1].append(line)
        else:
            clusters.append([line])

    output: list[BBox] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        top, bottom = cluster[0][0], cluster[-1][0]
        left = min(item[1] for item in cluster)
        right = max(item[2] for item in cluster)
        if bottom - top < resized.height * 0.04:
            continue
        padding = 3
        output.append(
            BBox(
                x0=max(0, left - padding) / resized.width * page_width,
                y0=max(0, top - padding) / resized.height * page_height,
                x1=min(resized.width, right + padding) / resized.width * page_width,
                y1=min(resized.height, bottom + padding) / resized.height * page_height,
            )
        )
    return output
