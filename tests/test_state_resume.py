from pathlib import Path

import fitz
import pytest

from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.base import OcrLine, OcrResult
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source
from legal_study.state import RunStateMissingError, RunStateStore, StepStatus


class CountingInspector(PdfInspector):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def inspect(self, *args, **kwargs):
        self.calls += 1
        return super().inspect(*args, **kwargs)


class FlakyOcr:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image_path: Path) -> OcrResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated OCR interruption")
        return OcrResult(
            engine=self.name,
            text="recovered",
            confidence=0.99,
            lines=[OcrLine(text="recovered", confidence=0.99)],
        )


def _blank_pdf(path: Path) -> None:
    document = fitz.open()
    document.new_page(width=200, height=200)
    document.save(path)
    document.close()


def test_pipeline_resumes_completed_steps_after_ocr_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    inspector = CountingInspector()
    engine = FlakyOcr()
    pipeline = PdfIngestPipeline(inspector=inspector, ocr_engine=engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="15",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    with pytest.raises(RuntimeError, match="simulated OCR interruption"):
        pipeline.run(snapshot, prepared)

    state = RunStateStore(settings.state_db)
    assert inspector.calls == 1
    assert state.get_step(prepared.manifest.run_id, "PDF_INSPECTED").status == StepStatus.COMPLETED
    assert state.get_step(prepared.manifest.run_id, "OCR_COMPLETE").status == StepStatus.FAILED

    pipeline.run(snapshot, prepared)

    assert inspector.calls == 1
    ocr_step = state.get_step(prepared.manifest.run_id, "OCR_COMPLETE")
    assert ocr_step is not None
    assert ocr_step.status == StepStatus.COMPLETED
    assert ocr_step.retry_count == 1


def test_completed_ocr_is_not_overwritten_on_resume(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    engine = FlakyOcr()
    engine.calls = 1
    pipeline = PdfIngestPipeline(ocr_engine=engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="16",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    pipeline.run(snapshot, prepared)
    original = (prepared.output_dir / "ocr.json").read_bytes()
    call_count = engine.calls
    pipeline.run(snapshot, prepared)

    assert engine.calls == call_count
    assert (prepared.output_dir / "ocr.json").read_bytes() == original


def test_corrupt_render_invalidates_completed_inspection_step(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    inspector = CountingInspector()
    pipeline = PdfIngestPipeline(inspector=inspector)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="17",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    pipeline.run(snapshot, prepared)
    render = prepared.output_dir / "renders" / "page-0001.png"
    render.write_bytes(b"corrupt")

    pipeline.run(snapshot, prepared)

    assert inspector.calls == 2
    assert render.read_bytes().startswith(b"\x89PNG")
    orphans = list((prepared.output_dir / "orphans" / "PDF_INSPECTED").glob("*.orphan"))
    assert any(path.read_bytes() == b"corrupt" for path in orphans)
    state = RunStateStore(settings.state_db)
    step = state.get_step(prepared.manifest.run_id, "PDF_INSPECTED")
    assert step is not None
    assert step.retry_count == 1


def test_missing_state_db_never_overwrites_existing_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    engine = FlakyOcr()
    engine.calls = 1
    pipeline = PdfIngestPipeline(ocr_engine=engine)
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="18",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    pipeline.run(snapshot, prepared)
    original = (prepared.output_dir / "ocr.json").read_bytes()
    settings.state_db.unlink()
    settings.state_db.with_name(f"{settings.state_db.name}-wal").unlink(missing_ok=True)
    settings.state_db.with_name(f"{settings.state_db.name}-shm").unlink(missing_ok=True)

    with pytest.raises(RunStateMissingError):
        pipeline.run(snapshot, prepared)

    assert (prepared.output_dir / "ocr.json").read_bytes() == original


def test_output_parent_named_orphans_does_not_disable_overwrite_guard(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    pipeline = PdfIngestPipeline()
    output = tmp_path / "orphans" / "custom-run"
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="19",
        pages=None,
        pipeline_config=pipeline.input_config(),
        output_dir=output,
        settings=settings,
    )
    pipeline.run(snapshot, prepared)
    settings.state_db.unlink()
    settings.state_db.with_name(f"{settings.state_db.name}-wal").unlink(missing_ok=True)
    settings.state_db.with_name(f"{settings.state_db.name}-shm").unlink(missing_ok=True)

    with pytest.raises(RunStateMissingError):
        pipeline.run(snapshot, prepared)
