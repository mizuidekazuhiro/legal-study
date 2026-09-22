import json
from pathlib import Path

import pymupdf
import pytest

from legal_study.io_utils import file_sha256
from legal_study.models import BBox, DocumentInspection, PageInspection, PageMode, SuspectRegion
from legal_study.pdf.ocr.base import OcrBackendMetadata, OcrLine, OcrResult
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


class FakeOcrEngine:
    name = "fake"

    def __init__(
        self,
        *,
        fail_on_call: int | None = None,
        model_version: str = "fake-v1",
    ) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0
        self._metadata = OcrBackendMetadata(
            engine=self.name,
            library_version="1.0",
            runtime="fake-runtime",
            runtime_version="1.0",
            model_version=model_version,
            model_names=["fake-model"],
            model_hashes={"fake-model": f"hash-{model_version}"},
            device="cpu",
            offline=True,
        )

    @property
    def metadata(self) -> OcrBackendMetadata:
        return self._metadata

    def recognize(self, _image_path: Path) -> OcrResult:
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("intentional OCR interruption")
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
            backend=self.metadata,
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
        "text_layer_trust": "high",
        "text_layer_origin": "born_digital_likely",
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


def _three_ocr_page_pdf(path: Path) -> None:
    document = pymupdf.open()
    for index in range(3):
        page = document.new_page(width=300, height=200)
        page.insert_text((30, 50), f"x{index}")
    document.save(path)
    document.close()


def _checkpoint_target(out: Path, *, dpi: int = 300) -> dict[str, object]:
    image = out / "target.png"
    if not image.exists():
        image.write_bytes(b"checkpoint-image")
    return {
        "kind": "suspect_native_text",
        "page_number": 1,
        "reason": "test",
        "bbox": [0.0, 0.0, 10.0, 10.0],
        "image": "target.png",
        "image_sha256": file_sha256(image),
        "dpi": dpi,
        "crop_padding_points": 0.0,
        "preprocessing": {"renderer": "test"},
        "coordinate_transform": {
            "pixel_to_pdf": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "image_width_px": 10,
            "image_height_px": 10,
            "pdf_bbox": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "page_rotation": 0,
        },
        "native_candidate": "bad",
        "native_bbox": [0.0, 0.0, 10.0, 10.0],
    }


def test_ocr_target_checkpoint_resumes_after_interruption(tmp_path: Path) -> None:
    source = tmp_path / "three-pages.pdf"
    _three_ocr_page_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)

    first_engine = FakeOcrEngine(fail_on_call=2)
    first_pipeline = PdfIngestPipeline(ocr_engine=first_engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="checkpoint-resume",
        pages=[1, 2, 3],
        pipeline_config=first_pipeline.input_config(),
        settings=settings,
    )

    with pytest.raises(RuntimeError, match="intentional OCR interruption"):
        first_pipeline.run(snapshot, prepared, pages=[1, 2, 3])

    checkpoint_dir = prepared.output_dir / "ocr_checkpoints"
    assert len(list(checkpoint_dir.glob("*.json"))) == 1
    assert not (prepared.output_dir / "ocr.json").exists()
    assert first_engine.calls == 2

    second_engine = FakeOcrEngine()
    second_pipeline = PdfIngestPipeline(ocr_engine=second_engine)
    second_pipeline.run(snapshot, prepared, pages=[1, 2, 3])

    assert second_engine.calls == 2
    assert len(list(checkpoint_dir.glob("*.json"))) == 3
    payload = json.loads(
        (prepared.output_dir / "ocr.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoint"] == {
        "schema_version": 1,
        "total_targets": 3,
        "reused_targets": 1,
        "executed_targets": 2,
    }
    assert all(
        payload["pages"][str(page)]["full_page"]["status"] == "completed"
        for page in (1, 2, 3)
    )


def test_ocr_checkpoint_invalidates_on_dpi_image_and_model_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _three_ocr_page_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    engine = FakeOcrEngine(model_version="fake-v1")
    pipeline = PdfIngestPipeline(ocr_engine=engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="checkpoint-identity",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    out = prepared.output_dir
    target = _checkpoint_target(out)

    _, reused = pipeline._execute_ocr_target_checkpointed(
        prepared, target, out, current=1, total=1
    )
    assert reused is False
    _, reused = pipeline._execute_ocr_target_checkpointed(
        prepared, target, out, current=1, total=1
    )
    assert reused is True
    assert engine.calls == 1

    dpi_target = {**target, "dpi": 450}
    _, reused = pipeline._execute_ocr_target_checkpointed(
        prepared, dpi_target, out, current=1, total=1
    )
    assert reused is False
    assert engine.calls == 2

    image = out / "target.png"
    image.write_bytes(b"changed-checkpoint-image")
    image_target = {**target, "image_sha256": file_sha256(image)}
    _, reused = pipeline._execute_ocr_target_checkpointed(
        prepared, image_target, out, current=1, total=1
    )
    assert reused is False
    assert engine.calls == 3

    changed_model = FakeOcrEngine(model_version="fake-v2")
    changed_pipeline = PdfIngestPipeline(ocr_engine=changed_model)
    _, reused = changed_pipeline._execute_ocr_target_checkpointed(
        prepared, image_target, out, current=1, total=1
    )
    assert reused is False
    assert changed_model.calls == 1


def test_corrupt_ocr_checkpoint_is_recomputed(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _three_ocr_page_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    engine = FakeOcrEngine()
    pipeline = PdfIngestPipeline(ocr_engine=engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="checkpoint-corrupt",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    target = _checkpoint_target(prepared.output_dir)

    pipeline._execute_ocr_target_checkpointed(
        prepared, target, prepared.output_dir, current=1, total=1
    )
    checkpoint_hash, _identity = pipeline._ocr_checkpoint_identity(
        prepared, target, prepared.output_dir
    )
    checkpoint = pipeline._ocr_checkpoint_path(prepared.output_dir, checkpoint_hash)
    checkpoint.write_text("{corrupt", encoding="utf-8")

    _, reused = pipeline._execute_ocr_target_checkpointed(
        prepared, target, prepared.output_dir, current=1, total=1
    )
    assert reused is False
    assert engine.calls == 2


def test_ocr_checkpoint_rejects_artifact_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _three_ocr_page_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    engine = FakeOcrEngine()
    pipeline = PdfIngestPipeline(ocr_engine=engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="checkpoint-path",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    outside = prepared.output_dir.parent / "outside.png"
    outside.write_bytes(b"outside")
    target = _checkpoint_target(prepared.output_dir)
    target["image"] = "../outside.png"
    target["image_sha256"] = file_sha256(outside)

    with pytest.raises(ValueError, match="escapes the run directory"):
        pipeline._execute_ocr_target_checkpointed(
            prepared, target, prepared.output_dir, current=1, total=1
        )


def _write_shared_cache_pdf(path: Path, *, insert_front: bool, add_markup: bool) -> None:
    document = pymupdf.open()
    if insert_front:
        inserted = document.new_page(width=300, height=200)
        inserted.insert_text((30, 50), "inserted")
    page = document.new_page(width=300, height=200)
    page.insert_text((30, 50), "x")
    if add_markup:
        shape = page.new_shape()
        shape.draw_line((30, 58), (100, 58))
        shape.finish(color=(1.0, 1.0, 0.514), width=8)
        shape.commit()
    document.save(path)
    document.close()


def test_shared_page_cache_reuses_ocr_after_page_move_and_markup_change(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "v1"
    second_dir = tmp_path / "v2"
    first_dir.mkdir()
    second_dir.mkdir()
    first_source = first_dir / "source.pdf"
    second_source = second_dir / "source.pdf"
    _write_shared_cache_pdf(first_source, insert_front=False, add_markup=False)
    _write_shared_cache_pdf(second_source, insert_front=True, add_markup=True)

    settings = LocalSettings(home=tmp_path / "home")

    first_snapshot = snapshot_source(first_source, settings=settings)
    first_engine = FakeOcrEngine()
    first_pipeline = PdfIngestPipeline(ocr_engine=first_engine)
    first_prepared = prepare_run(
        snapshot=first_snapshot,
        subject="criminal",
        question="shared-cache-v1",
        pages=[1],
        pipeline_config=first_pipeline.input_config(),
        settings=settings,
    )
    first_pipeline.run(first_snapshot, first_prepared, pages=[1])
    assert first_engine.calls == 1

    second_snapshot = snapshot_source(second_source, settings=settings)
    second_engine = FakeOcrEngine()
    second_pipeline = PdfIngestPipeline(ocr_engine=second_engine)
    second_prepared = prepare_run(
        snapshot=second_snapshot,
        subject="criminal",
        question="shared-cache-v2",
        pages=[2],
        pipeline_config=second_pipeline.input_config(),
        settings=settings,
    )
    second_pipeline.run(second_snapshot, second_prepared, pages=[2])

    assert second_engine.calls == 0
    ocr_payload = json.loads(
        (second_prepared.output_dir / "ocr.json").read_text(encoding="utf-8")
    )
    assert ocr_payload["checkpoint"]["reused_targets"] == 1
    assert ocr_payload["checkpoint"]["executed_targets"] == 0
    evidence = ocr_payload["pages"]["2"]["full_page"]
    assert evidence["cache"]["scope"] == "shared_page"
    assert evidence["cache"]["reused"] is True

    alignment = json.loads(
        (second_prepared.output_dir / "page_alignment.json").read_text(encoding="utf-8")
    )
    moved = next(
        item for item in alignment["records"] if item.get("current_page") == 2
    )
    assert moved["classification"] == "MOVED_MARKUP_CHANGED"
    assert moved["safe_for_base_ocr_reuse"] is True
