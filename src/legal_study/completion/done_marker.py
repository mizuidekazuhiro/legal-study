from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat, UnidentifiedImageError
from pydantic import BaseModel, Field

STAMP_VERSION = "LEGAL-STUDY-DONE-V1"
_STAMP_SIZE = (600, 220)
_STAMP_ASPECT = _STAMP_SIZE[0] / _STAMP_SIZE[1]

_GLYPHS: dict[str, tuple[str, ...]] = {
    "D": (
        "11110",
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "11110",
    ),
    "O": (
        "01110",
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "01110",
    ),
    "N": (
        "10001",
        "11001",
        "10101",
        "10011",
        "10001",
        "10001",
        "10001",
    ),
    "E": (
        "11111",
        "10000",
        "10000",
        "11110",
        "10000",
        "10000",
        "11111",
    ),
}


class DoneDetection(BaseModel):
    page_number: int
    detected: bool
    confidence: float = Field(ge=0.0, le=1.0)
    method: str | None = None
    bbox_px: tuple[int, int, int, int] | None = None
    stamp_version: str = STAMP_VERSION


def _draw_bitmap_word(
    draw: ImageDraw.ImageDraw,
    word: str,
    *,
    origin: tuple[int, int],
    cell: int,
    gap: int,
) -> None:
    x, y = origin
    for character in word:
        glyph = _GLYPHS[character]
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    x0 = x + column * cell
                    y0 = y + row * cell
                    draw.rectangle(
                        (x0, y0, x0 + cell - 1, y0 + cell - 1),
                        fill="black",
                    )
        x += 5 * cell + gap


def build_done_stamp_image() -> Image.Image:
    """Create the canonical high-contrast DONE marker without external fonts."""
    image = Image.new("L", _STAMP_SIZE, 255)
    draw = ImageDraw.Draw(image)

    draw.rectangle((6, 6, 593, 213), outline=0, width=10)

    # Asymmetric corner fiducials make the marker visually and mechanically distinct.
    draw.rectangle((28, 28, 66, 66), fill=0)
    draw.ellipse((532, 27, 574, 69), fill=0)
    draw.polygon(((32, 183), (53, 143), (74, 183)), fill=0)
    draw.rectangle((529, 145, 575, 184), fill=0)
    draw.rectangle((540, 154, 565, 175), fill=255)

    # Fixed machine bars. Their asymmetry protects against accidental text-only matches.
    bars = (1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1, 1)
    bx = 98
    for bit in bars:
        draw.rectangle(
            (bx, 174, bx + 12, 190 if bit else 181),
            fill=0,
        )
        bx += 19

    cell = 13
    gap = 14
    word_width = 4 * 5 * cell + 3 * gap
    _draw_bitmap_word(
        draw,
        "DONE",
        origin=((600 - word_width) // 2, 59),
        cell=cell,
        gap=gap,
    )
    return image.convert("RGB")


def done_stamp_png_bytes() -> bytes:
    buffer = io.BytesIO()
    build_done_stamp_image().save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def save_done_stamp(path: Path) -> str:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = done_stamp_png_bytes()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _binary_signature(image: Image.Image, size: tuple[int, int] = (120, 44)) -> Image.Image:
    grayscale = ImageOps.autocontrast(image.convert("L"))
    normalized = grayscale.resize(size, Image.Resampling.LANCZOS)
    return normalized.point(lambda value: 255 if value >= 180 else 0, mode="1").convert("L")



def _similarity(left: Image.Image, right: Image.Image) -> float:
    a = _binary_signature(left)
    b = _binary_signature(right)
    difference = ImageChops.difference(a, b)
    mean = ImageStat.Stat(difference).mean[0]
    return max(0.0, min(1.0, 1.0 - mean / 255.0))


def _embedded_image_detection(document: pymupdf.Document, page: pymupdf.Page) -> DoneDetection | None:
    best = 0.0
    for item in page.get_images(full=True):
        xref = int(item[0])
        try:
            extracted = document.extract_image(xref)
            image = Image.open(io.BytesIO(extracted["image"])).convert("RGB")
        except (KeyError, OSError, RuntimeError, UnidentifiedImageError):
            continue
        width, height = image.size
        if width < 80 or height < 28 or height == 0:
            continue
        aspect = width / height
        if abs(aspect - _STAMP_ASPECT) / _STAMP_ASPECT > 0.30:
            continue
        score = _similarity(image, build_done_stamp_image())
        best = max(best, score)
        if score >= 0.88:
            return DoneDetection(
                page_number=page.number + 1,
                detected=True,
                confidence=score,
                method="embedded_image_signature",
            )
    if best:
        return DoneDetection(
            page_number=page.number + 1,
            detected=False,
            confidence=best,
            method="embedded_image_signature",
        )
    return None


def _rendered_template_detection(page: pymupdf.Page, *, dpi: int = 144) -> DoneDetection:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    rendered = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    grayscale = ImageOps.autocontrast(rendered.convert("L"))

    # DONE is operationally placed near the lower-right of the question's last page.
    search_left = int(grayscale.width * 0.50)
    search_top = int(grayscale.height * 0.55)
    search = grayscale.crop((search_left, search_top, grayscale.width, grayscale.height))

    template = build_done_stamp_image().convert("L")
    page_width = grayscale.width
    candidate_widths = sorted(
        {
            max(90, int(page_width * ratio))
            for ratio in (0.10, 0.12, 0.14, 0.16, 0.18)
        }
    )

    best_score = 0.0
    best_box: tuple[int, int, int, int] | None = None
    for width in candidate_widths:
        height = max(30, round(width / _STAMP_ASPECT))
        if width > search.width or height > search.height:
            continue
        scaled = template.resize((width, height), Image.Resampling.LANCZOS)
        x_step = max(6, width // 10)
        y_step = max(5, height // 5)
        max_x = search.width - width
        max_y = search.height - height
        xs = list(range(0, max_x + 1, x_step))
        ys = list(range(0, max_y + 1, y_step))
        if not xs or xs[-1] != max_x:
            xs.append(max_x)
        if not ys or ys[-1] != max_y:
            ys.append(max_y)

        scale_best_score = 0.0
        scale_best_xy: tuple[int, int] | None = None
        for y in ys:
            for x in xs:
                crop = search.crop((x, y, x + width, y + height))
                score = _similarity(crop, scaled)
                if score > scale_best_score:
                    scale_best_score = score
                    scale_best_xy = (x, y)

        if scale_best_xy is not None:
            coarse_x, coarse_y = scale_best_xy
            refine_left = max(0, coarse_x - x_step)
            refine_right = min(max_x, coarse_x + x_step)
            refine_top = max(0, coarse_y - y_step)
            refine_bottom = min(max_y, coarse_y + y_step)
            for y in range(refine_top, refine_bottom + 1, 2):
                for x in range(refine_left, refine_right + 1, 2):
                    crop = search.crop((x, y, x + width, y + height))
                    score = _similarity(crop, scaled)
                    if score > scale_best_score:
                        scale_best_score = score
                        scale_best_xy = (x, y)

        if scale_best_xy is not None and scale_best_score > best_score:
            x, y = scale_best_xy
            best_score = scale_best_score
            best_box = (
                search_left + x,
                search_top + y,
                search_left + x + width,
                search_top + y + height,
            )

    return DoneDetection(
        page_number=page.number + 1,
        detected=best_score >= 0.84,
        confidence=best_score,
        method="rendered_template",
        bbox_px=best_box,
    )


def detect_done_markers(
    pdf: str | Path,
    *,
    pages: list[int] | None = None,
) -> list[DoneDetection]:
    document = pymupdf.open(pdf)
    try:
        selected = pages or list(range(1, len(document) + 1))
        results: list[DoneDetection] = []
        for page_number in selected:
            if page_number < 1 or page_number > len(document):
                raise ValueError(f"Page out of range: {page_number}")
            page = document[page_number - 1]
            embedded = _embedded_image_detection(document, page)
            if embedded is not None and embedded.detected:
                results.append(embedded)
                continue
            rendered = _rendered_template_detection(page)
            if embedded is not None and embedded.confidence > rendered.confidence:
                results.append(embedded)
            else:
                results.append(rendered)
        return results
    finally:
        document.close()


def detect_embedded_done_markers(
    pdf: str | Path, *, pages: list[int] | None = None
) -> list[DoneDetection]:
    """Find canonical embedded DONE stamps without rendering or OCRing pages."""
    document = pymupdf.open(pdf)
    try:
        results: list[DoneDetection] = []
        selected = pages or list(range(1, len(document) + 1))
        for page_number in selected:
            if page_number < 1 or page_number > len(document):
                raise ValueError(f"Page out of range: {page_number}")
            page = document[page_number - 1]
            embedded = _embedded_image_detection(document, page)
            if embedded is not None and embedded.detected:
                results.append(embedded)
        return results
    finally:
        document.close()
