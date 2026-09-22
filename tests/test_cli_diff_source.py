import json
from pathlib import Path

import pymupdf
from typer.testing import CliRunner

from legal_study.cli import app
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


def _pdf(path: Path, *, insert_front: bool) -> None:
    document = pymupdf.open()
    if insert_front:
        inserted = document.new_page(width=300, height=200)
        inserted.insert_text((30, 50), "inserted page")
    page = document.new_page(width=300, height=200)
    page.insert_text((30, 50), "stable baseline page")
    document.save(path)
    document.close()


def test_diff_source_maps_requested_page_after_insertion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("LEGAL_STUDY_HOME", str(home))
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_pdf = old_dir / "source.pdf"
    new_pdf = new_dir / "source.pdf"
    _pdf(old_pdf, insert_front=False)
    _pdf(new_pdf, insert_front=True)

    settings = LocalSettings()
    old_snapshot = snapshot_source(old_pdf, settings=settings)
    pipeline = PdfIngestPipeline()
    prepared = prepare_run(
        snapshot=old_snapshot,
        subject="criminal",
        question="shadow",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )
    pipeline.run(old_snapshot, prepared, pages=[1])

    result = CliRunner().invoke(
        app,
        [
            "diff-source",
            str(new_pdf),
            "--against-run",
            str(prepared.output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["requested_baseline_pages"] == [1]
    assert payload["suggested_current_pages"] == [2]
    assert payload["safe_for_base_ocr_reuse_pages"] == [2]
    assert payload["requested_page_alignment"][0]["classification"] == "MOVED"
