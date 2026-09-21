from pathlib import Path

import fitz

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
    assert "highlight_stroke" in kinds
    assert "red_pen_stroke" in kinds
    assert inspected.vision_review_recommended is True
