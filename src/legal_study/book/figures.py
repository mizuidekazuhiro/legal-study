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


def _expand_up_to_nearby_ink(
    image: Image.Image, *, top: int, left: int, right: int, floor: int
) -> int:
    pixels = image.load()
    blank_limit = max(8, round(image.height * 0.018))
    blank = 0
    expanded = top
    for y in range(top - 1, floor - 1, -1):
        dark = sum(1 for x in range(left, right) if pixels[x, y] < 140)
        if dark >= 3:
            expanded = y
            blank = 0
        else:
            blank += 1
            if blank >= blank_limit:
                break
    return expanded


def detect_figure_regions(
    image: Image.Image,
    *,
    page_width: float,
    page_height: float,
    body_bbox: BBox,
    word_boxes: list[tuple[str, BBox]] | None = None,
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
        top = _expand_up_to_nearby_ink(
            resized,
            top=top,
            left=max(x0, left - 4),
            right=min(x1, right + 4),
            floor=y0,
        )
        padding = 3
        output.append(
            BBox(
                x0=max(0, left - padding) / resized.width * page_width,
                y0=max(0, top - padding) / resized.height * page_height,
                x1=min(resized.width, right + padding) / resized.width * page_width,
                y1=min(resized.height, bottom + padding) / resized.height * page_height,
            )
        )
    layout_words = [
        (text.strip(), bbox)
        for text, bbox in (word_boxes or [])
        if body_bbox.x0 <= (bbox.x0 + bbox.x1) / 2 <= body_bbox.x1
        and body_bbox.y0 <= (bbox.y0 + bbox.y1) / 2 <= body_bbox.y1
    ]
    symbols = [
        bbox
        for text, bbox in layout_words
        if text.replace(" ", "") in {"↓", "↑", "→", "←", "+", "＋", "⇩", "⇒"}
    ]
    if len(symbols) >= 3:
        seed_top = min(item.y0 for item in symbols)
        seed_bottom = max(item.y1 for item in symbols)
        band = [
            bbox
            for _, bbox in layout_words
            if bbox.y1 >= seed_top - page_height * 0.08
            and bbox.y0 <= seed_bottom + page_height * 0.04
        ]
        if len(band) >= 8:
            left = min(item.x0 for item in band)
            right = max(item.x1 for item in band)
            if right - left >= (body_bbox.x1 - body_bbox.x0) * 0.35:
                output.append(
                    BBox(
                        x0=max(body_bbox.x0, left - page_width * 0.015),
                        y0=max(0.0, min(item.y0 for item in band) - 4),
                        x1=min(body_bbox.x1, right + page_width * 0.015),
                        y1=min(body_bbox.y1, max(item.y1 for item in band) + 4),
                    )
                )

    merged: list[BBox] = []
    for region in sorted(output, key=lambda item: (item.y0, item.x0)):
        match = next(
            (
                item
                for item in merged
                if not (
                    region.x1 < item.x0
                    or item.x1 < region.x0
                    or region.y1 < item.y0
                    or item.y1 < region.y0
                )
            ),
            None,
        )
        if match is None:
            merged.append(region)
            continue
        merged.remove(match)
        merged.append(
            BBox(
                x0=min(match.x0, region.x0),
                y0=min(match.y0, region.y0),
                x1=max(match.x1, region.x1),
                y1=max(match.y1, region.y1),
            )
        )
    return sorted(merged, key=lambda item: (item.y0, item.x0))
