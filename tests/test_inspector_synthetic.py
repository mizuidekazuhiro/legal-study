from pathlib import Path

import fitz
import pytest

from legal_study.pdf.inspector import PdfInspector


def test_inspector_detects_flattened_vector_marks(tmp_path: Path) -> None:
    pdf_path = tmp_path / "synthetic.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((50, 80), "ABC DEF GHI JKL MNO", fontsize=12)
    shape = page.new_shape()
    shape.draw_line((50, 77), (145, 77))
    shape.finish(color=(1.0, 1.0, 0.5137255), width=7, stroke_opacity=0.5)
    shape.commit()
    shape = page.new_shape()
    shape.draw_line((200, 100), (220, 110))
    shape.finish(color=(1.0, 0.1647059, 0.1333181), width=1)
    shape.commit()
    doc.save(pdf_path)
    doc.close()

    result = PdfInspector(min_native_chars=5).inspect(pdf_path)
    inspected = result.pages[0]
    kinds = {mark.kind for mark in inspected.vector_marks}
    assert "marker_candidate" in kinds
    assert "red_vector_evidence" in kinds
    assert inspected.vision_review_recommended is True


def test_inspector_preserves_annotation_vertices_without_crashing(tmp_path: Path) -> None:
    pdf_path = tmp_path / "annotation.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((50, 80), "ABC DEF GHI", fontsize=12)
    annotation = page.add_highlight_annot(fitz.Rect(48, 67, 130, 84))
    annotation.update()
    doc.save(pdf_path)
    doc.close()

    result = PdfInspector(min_native_chars=1).inspect(pdf_path)

    annotations = result.pages[0].annotations
    assert len(annotations) == 1
    assert annotations[0].type_name == "Highlight"
    assert annotations[0].vertices == [
        (48.0, 67.0),
        (130.0, 67.0),
        (48.0, 84.0),
        (130.0, 84.0),
    ]


def test_inspector_preserves_raw_pdf_evidence(tmp_path: Path) -> None:
    pdf_path = tmp_path / "raw-evidence.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((50, 80), "ABC DEF", fontsize=12)
    annotation = page.add_highlight_annot(fitz.Rect(48, 67, 130, 84))
    annotation.set_info(content="review note", title="reviewer")
    annotation.update()
    shape = page.new_shape()
    shape.draw_line((50, 100), (145, 100))
    shape.finish(color=(0.1, 0.2, 0.3), width=4, stroke_opacity=0.4)
    shape.commit()
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(50, 120, 145, 140))
    shape.finish(color=None, fill=(0.3450828, 0.6941177, 1.0), fill_opacity=0.5)
    shape.commit()
    shape = page.new_shape()
    shape.draw_line((50, 160), (145, 160))
    shape.finish(color=(1.0, 0.1647059, 0.1333181), width=9)
    shape.commit()
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(50, 180, 145, 200))
    shape.finish(color=None, fill=(1.0, 0.1647059, 0.1333181), fill_opacity=0.6)
    shape.commit()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
    pixmap.clear_with(255)
    page.insert_image(fitz.Rect(200, 200, 250, 250), pixmap=pixmap)
    doc.save(pdf_path)
    doc.close()

    inspected = PdfInspector(min_native_chars=1).inspect(pdf_path).pages[0]

    raw_chars = inspected.raw_native["blocks"][0]["lines"][0]["spans"][0]["chars"]
    assert "".join(char["c"] for char in raw_chars) == "ABC DEF"
    assert raw_chars[0]["bbox"]

    raw_annotation = inspected.annotations[0].raw
    assert raw_annotation["type"] == {"code": 8, "name": "Highlight"}
    assert raw_annotation["info"]["content"] == "review note"
    assert raw_annotation["info"]["title"] == "reviewer"
    assert raw_annotation["vertices"]

    assert len(inspected.raw_vector_drawings) == inspected.drawing_count
    assert len(inspected.raw_vector_drawings) >= 4
    unknown_stroke = next(
        drawing
        for drawing in inspected.raw_vector_drawings
        if drawing.raw.get("color") == pytest.approx([0.1, 0.2, 0.3])
    )
    assert unknown_stroke.raw["width"] == 4.0
    assert unknown_stroke.raw["stroke_opacity"] == pytest.approx(0.4)
    assert unknown_stroke.raw["items"]
    unknown_index = unknown_stroke.drawing_index
    assert all(mark.drawing_index != unknown_index for mark in inspected.vector_marks)

    blue_fill = next(
        mark
        for mark in inspected.vector_marks
        if mark.kind == "marker_candidate" and mark.color_name == "blue"
    )
    assert blue_fill.paint == "fill"
    assert blue_fill.opacity == pytest.approx(0.5)
    thick_red = next(
        mark
        for mark in inspected.vector_marks
        if mark.kind == "red_vector_evidence" and mark.width == 9.0
    )
    assert thick_red.paint == "stroke"
    red_fill = next(
        mark
        for mark in inspected.vector_marks
        if mark.kind == "red_vector_evidence" and mark.paint == "fill"
    )
    assert red_fill.opacity == pytest.approx(0.6)

    assert len(inspected.raw_image_regions) == 1
    image = inspected.raw_image_regions[0]
    assert image.bbox.model_dump() == {"x0": 200.0, "y0": 200.0, "x1": 250.0, "y1": 250.0}
    assert image.digest
    assert image.raw["digest"] == {"type": "bytes", "hex": image.digest}
    assert image.raw["width"] == 2
    assert image.raw["height"] == 2
