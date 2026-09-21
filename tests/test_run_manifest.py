from pathlib import Path

import fitz
import pytest

from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import RunManifestMismatchError, prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


def _pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((20, 40), "ABC DEF GHI")
    document.save(path)
    document.close()


def test_run_manifest_distinguishes_page_ranges_and_rejects_mixed_output(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    pipeline = PdfIngestPipeline()

    first = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="15",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    second = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="15",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    assert first.output_dir != second.output_dir
    assert first.manifest.source.sha256 == snapshot.sha256
    assert first.manifest.pymupdf_version
    assert first.manifest_path.exists()

    with pytest.raises(RunManifestMismatchError):
        prepare_run(
            snapshot=snapshot,
            subject="criminal",
            question="15",
            pages=None,
            pipeline_config=pipeline.input_config(),
            output_dir=first.output_dir,
            settings=settings,
        )


def test_nonempty_custom_output_requires_matching_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    output = tmp_path / "existing"
    output.mkdir()
    (output / "old-ocr.json").write_text("stale", encoding="utf-8")

    with pytest.raises(RunManifestMismatchError):
        prepare_run(
            snapshot=snapshot,
            subject="criminal",
            question="15",
            pages=None,
            pipeline_config=PdfIngestPipeline().input_config(),
            output_dir=output,
            settings=settings,
        )


def test_pipeline_uses_snapshot_after_original_is_removed(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    source.unlink()
    pipeline = PdfIngestPipeline()
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="15",
        pages=None,
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    result = pipeline.run(snapshot, prepared)

    assert result.sha256 == snapshot.sha256
    assert result.source_path == snapshot.snapshot_path
