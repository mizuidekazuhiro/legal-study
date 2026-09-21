from pathlib import Path

import pymupdf

from legal_study.finalize import finalize_existing_run
from legal_study.io_utils import file_sha256
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


def _pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((30, 50), "ABC DEF GHI JKL MNO")
    document.save(path)
    document.close()


def test_finalize_existing_run_does_not_change_ocr_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    pipeline = PdfIngestPipeline()
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="finalize",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    pipeline.run(snapshot, prepared, pages=[1])
    ocr_path = prepared.output_dir / "ocr.json"
    before = file_sha256(ocr_path)

    result = finalize_existing_run(prepared.output_dir, settings=settings)

    assert file_sha256(ocr_path) == before
    assert result["problem_validation"]["valid"] is True
    assert result["problem_markdown"] == "criminal_finalize_problem.md"
    assert (prepared.output_dir / result["problem_markdown"]).is_file()
