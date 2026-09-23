from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from legal_study.completion.done_marker import (
    STAMP_VERSION,
    build_done_stamp_image,
    detect_done_markers,
    done_stamp_png_bytes,
    save_done_stamp,
)


def _plain_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=400, height=550)
    page.insert_text((40, 80), "ordinary study page")
    document.save(path)
    document.close()


def test_done_stamp_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    first_hash = save_done_stamp(first)
    second_hash = save_done_stamp(second)

    assert first.read_bytes() == second.read_bytes()
    assert first_hash == second_hash
    assert build_done_stamp_image().size == (600, 220)


def test_detects_done_stamp_as_embedded_image(tmp_path: Path) -> None:
    pdf = tmp_path / "embedded.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=550)
    page.insert_text((40, 80), "question content")
    page.insert_image(
        pymupdf.Rect(250, 470, 370, 514),
        stream=done_stamp_png_bytes(),
    )
    document.save(pdf)
    document.close()

    result = detect_done_markers(pdf, pages=[1])[0]

    assert result.detected is True
    assert result.method == "embedded_image_signature"
    assert result.confidence >= 0.88
    assert result.stamp_version == STAMP_VERSION


def test_detects_done_stamp_after_page_is_flattened(tmp_path: Path) -> None:
    page_image = Image.new("RGB", (800, 1100), "white")
    draw = ImageDraw.Draw(page_image)
    draw.text((80, 120), "question content", fill="black")
    stamp = build_done_stamp_image().resize((128, 47), Image.Resampling.LANCZOS)
    page_image.paste(stamp, (640, 940))

    flattened_png = tmp_path / "flattened.png"
    page_image.save(flattened_png)

    pdf = tmp_path / "flattened.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=550)
    page.insert_image(page.rect, filename=str(flattened_png))
    document.save(pdf)
    document.close()

    result = detect_done_markers(pdf, pages=[1])[0]

    assert result.detected is True
    assert result.method == "rendered_template"
    assert result.confidence >= 0.84
    assert result.bbox_px is not None


def test_plain_page_does_not_trigger_done(tmp_path: Path) -> None:
    pdf = tmp_path / "plain.pdf"
    _plain_pdf(pdf)

    result = detect_done_markers(pdf, pages=[1])[0]

    assert result.detected is False
