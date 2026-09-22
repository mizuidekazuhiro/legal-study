import json
from pathlib import Path

import pymupdf
import yaml

from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


def _marked_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.insert_text((40, 80), "ABC DEF GHI JKL MNO", fontsize=12)

    yellow = page.new_shape()
    yellow.draw_line((40, 76), (140, 76))
    yellow.finish(color=(1.0, 1.0, 0.5137255), width=7, stroke_opacity=0.5)
    yellow.commit()

    second_yellow = page.new_shape()
    second_yellow.draw_line((220, 76), (260, 76))
    second_yellow.finish(color=(1.0, 1.0, 0.5137255), width=7, stroke_opacity=0.5)
    second_yellow.commit()

    red = page.new_shape()
    red.draw_line((180, 80), (210, 95))
    red.finish(color=(1.0, 0.1647059, 0.1333181), width=2)
    red.commit()

    document.save(path)
    document.close()


def test_pipeline_writes_valid_problem_packet_with_relative_evidence(tmp_path: Path) -> None:
    source = tmp_path / "marked.pdf"
    _marked_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    pipeline = PdfIngestPipeline()
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="15",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    pipeline.run(snapshot, prepared, pages=[1])

    canonical_path = prepared.output_dir / "canonical_source.json"
    markdown_path = prepared.output_dir / "criminal_15_problem.md"
    validation_path = prepared.output_dir / "problem_validation.json"
    reconciliation_path = prepared.output_dir / "reconciliation.json"
    repair_path = prepared.output_dir / "repair.json"

    assert canonical_path.is_file()
    assert markdown_path.is_file()
    assert validation_path.is_file()
    assert reconciliation_path.is_file()
    assert repair_path.is_file()

    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert canonical["source"]["sha256"] == snapshot.sha256
    assert canonical["source"]["requested_pages"] == [1]
    assert validation["valid"] is True
    assert validation["checks"]["yaml_parseable"] is True
    assert validation["checks"]["native_text_present"] is True
    assert validation["checks"]["upload_files_unique"] is True
    assert validation["checks"]["repair_source_sha_matches"] is True
    assert validation["checks"]["auto_repairs_have_provenance"] is True
    assert validation["checks"]["protected_content_never_auto_repaired"] is True
    assert (
        validation["checks"]["low_trust_auto_repairs_have_full_page_support"] is True
    )
    assert "ABC DEF GHI JKL MNO" in markdown

    assert canonical["needs_review"]
    assert canonical["review_issue_count"] >= len(canonical["needs_review"])
    assert canonical["handoff_review_sheets"] == {
        "1": "handoff_review/page-0001-review.png"
    } or canonical["handoff_review_sheets"] == {
        1: "handoff_review/page-0001-review.png"
    }
    assert (prepared.output_dir / "handoff_review/page-0001-review.png").is_file()
    assert len(validation["upload_files"]) == len(set(validation["upload_files"]))
    assert validation["upload_files"] == [
        "criminal_15_problem.md",
        "handoff_review/page-0001-review.png",
    ]

    end = markdown.find("\n---\n", 4)
    frontmatter = yaml.safe_load(markdown[4:end])
    assert frontmatter["subject"] == "criminal"
    assert frontmatter["question"] == "15"
    assert frontmatter["source_pages"] == [1]

    for item in canonical["needs_review"]:
        reference = item.get("handoff_evidence_image")
        assert reference
        assert not Path(reference).is_absolute()
        assert ".." not in Path(reference).parts
        assert (prepared.output_dir / reference).is_file()
        for source_reference in item.get("source_evidence_images", []):
            assert not Path(source_reference).is_absolute()
            assert ".." not in Path(source_reference).parts
            assert (prepared.output_dir / source_reference).is_file()


def test_problem_packet_resume_keeps_outputs_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "marked.pdf"
    _marked_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    pipeline = PdfIngestPipeline()
    prepared = prepare_run(
        snapshot=snapshot,
        subject="criminal",
        question="resume",
        pages=[1],
        pipeline_config=pipeline.input_config(),
        settings=settings,
    )

    pipeline.run(snapshot, prepared, pages=[1])
    names = [
        "reconciliation.json",
        "repair.json",
        "canonical_source.json",
        "criminal_resume_problem.md",
        "problem_validation.json",
    ]
    before = {name: (prepared.output_dir / name).read_bytes() for name in names}

    pipeline.run(snapshot, prepared, pages=[1])
    after = {name: (prepared.output_dir / name).read_bytes() for name in names}

    assert before == after
