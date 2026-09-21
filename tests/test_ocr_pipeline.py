import json
from pathlib import Path

import pymupdf

from legal_study.models import BBox, DocumentInspection, PageInspection, PageMode, SuspectRegion
from legal_study.pdf.ocr.base import OcrLine, OcrResult
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


class FakeOcrEngine:
    name = "fake"

    def recognize(self, _image_path: Path) -> OcrResult:
        return OcrResult(
            engine=self.name,
            text="OCR evidence",
            confidence=0.95,
            lines=[
                OcrLine(
                    text="OCR evidence",
                    confidence=0.95,
                    bbox=BBox(x0=10, y0=10, x1=110, y1=40),
                )
            ],
        )


def _hybrid_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    for index in range(20):
        page.insert_text((40, 40 + index * 20), "native text remains authoritative 0123456789")
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 512, 256), False)
    pixmap.clear_with(255)
    page.insert_image(pymupdf.Rect(60, 500, 360, 650), pixmap=pixmap)
    second = document.new_page(width=595, height=842)
    second.insert_text((40, 40), "x")
    document.save(path)
    document.close()


def test_pipeline_routes_full_page_and_image_region_as_separate_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hybrid.pdf"
    _hybrid_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    pipeline = PdfIngestPipeline(ocr_engine=FakeOcrEngine())
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="routing",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    inspection = pipeline.run(snapshot, prepared)
    payload = json.loads((prepared.output_dir / "ocr.json").read_text(encoding="utf-8"))

    assert inspection.pages[0].ocr_recommended is False
    assert inspection.pages[1].ocr_recommended is True
    assert payload["native_text_replaced"] is False
    assert payload["pages"]["1"]["routing"] == {
        "native_text_preserved": True,
        "full_page_ocr": False,
        "surgical_region_count": 0,
        "image_region_count": 1,
    }
    page_one_region = payload["pages"]["1"]["regions"][0]
    assert page_one_region["status"] == "completed"
    assert page_one_region["target"]["kind"] == "image_region"
    assert page_one_region["target"]["dpi"] == 300
    assert not Path(page_one_region["target"]["image"]).is_absolute()
    assert page_one_region["result"]["input"]["image_sha256"]
    assert page_one_region["result"]["lines"][0]["pdf_bbox"] is not None
    page_two = payload["pages"]["2"]
    assert page_two["routing"]["full_page_ocr"] is True
    assert page_two["full_page"]["status"] == "completed"
    assert page_two["full_page"]["target"]["dpi"] == 300


def test_surgical_crop_defaults_to_450_dpi_and_records_transform(tmp_path: Path) -> None:
    source = tmp_path / "surgical.pdf"
    document = pymupdf.open()
    document.new_page(width=200, height=200)
    document.save(source)
    document.close()
    inspection = DocumentInspection(
        source_path=source,
        sha256="0" * 64,
        page_count=1,
        pages=[
            PageInspection(
                page_number=1,
                width=200,
                height=200,
                native_text="bad glyph",
                native_char_count=8,
                native_quality_score=0.5,
                suspect_native_regions=[
                    SuspectRegion(
                        text="�",
                        bbox=BBox(x0=40, y0=50, x1=80, y1=70),
                        reason="suspicious_glyphs=1",
                    )
                ],
                image_coverage=0,
                largest_image_coverage=0,
                drawing_count=0,
                annotation_count=0,
                mode=PageMode.OCR_REQUIRED,
                ocr_recommended=True,
                vision_review_recommended=True,
            )
        ],
    )
    output = tmp_path / "run"
    targets = PdfIngestPipeline()._render_ocr_targets(
        source,
        inspection,
        output / "ocr_crops",
        artifact_root=output,
    )

    target = targets[1][0]
    assert target["kind"] == "suspect_native_text"
    assert target["native_candidate"] == "�"
    assert target["dpi"] == 450
    assert target["crop_padding_points"] == 6.0
    assert target["image_sha256"]
    transform = target["coordinate_transform"]
    assert transform["image_width_px"] > 0
    assert transform["image_height_px"] > 0
