import hashlib
import json
from pathlib import Path

import pytest

from legal_study.study_draft import DraftSource
from legal_study.study_draft_request import (
    build_study_draft_request_bundle,
    required_instruction_names,
)
from legal_study.supplemental_retrieval import SupplementalRetrievalBundle


def _source() -> DraftSource:
    return DraftSource(
        source_sha256="a" * 64,
        source_snapshot_path="C:/Users/test/.legal-study/sources/sha256/source.pdf",
        stable_page_ids=["stable-110"],
        requested_pages=[110],
        run_id="run-22",
        handoff_path="criminal_22_handoff.md",
        canonical_source_path="canonical_source.json",
        problem_validation_path="problem_validation.json",
    )


def _write_inputs(run_dir: Path, instruction_dir: Path) -> None:
    (run_dir / "handoff_review").mkdir(parents=True)
    canonical = {
        "schema_version": 3,
        "subject": "criminal",
        "question": "22",
        "source": {
            "filename": "論文マスター_刑法.pdf",
            "sha256": "a" * 64,
            "requested_pages": [110],
        },
        "pages": [
            {
                "page_number": 110,
                "repair_review_count": 1,
            }
        ],
        "handoff_review_sheets": {
            "110": "handoff_review/page-0110-review.png",
        },
    }
    (run_dir / "canonical_source.json").write_text(
        json.dumps(canonical, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "problem_validation.json").write_text(
        json.dumps({"valid": True}),
        encoding="utf-8",
    )
    (run_dir / "criminal_22_handoff.md").write_text(
        """---
handoff_schema_version: 2
subject: criminal
question: "22"
source_file: 論文マスター_刑法.pdf
source_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
source_pages: [110]
page_review_count: 1
review_issue_count: 1
---

# Page Reading Pack

## PDF page 110

### Primary Reading Text

講師答案本文
""",
        encoding="utf-8",
    )
    (run_dir / "handoff_review/page-0110-review.png").write_bytes(
        b"review-sheet-110"
    )

    instruction_dir.mkdir(parents=True)
    for index, name in enumerate(required_instruction_names("criminal"), start=1):
        (instruction_dir / name).write_text(
            f"# instruction {index}\n{name}\n",
            encoding="utf-8",
        )


def test_build_bundle_contains_exact_inputs_and_response_schema(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)

    bundle = build_study_draft_request_bundle(
        subject="criminal",
        question="22",
        source=_source(),
        run_dir=run_dir,
        instruction_dir=instruction_dir,
    )

    assert bundle.schema_version == "study_draft_request.v1"
    assert bundle.output_filename == "study_draft.json"
    assert bundle.handoff.name == "criminal_22_handoff.md"
    assert bundle.handoff.text.endswith("講師答案本文\n")
    assert [item.name for item in bundle.instructions] == required_instruction_names(
        "criminal"
    )
    assert all(len(item.sha256) == 64 for item in bundle.instructions)
    assert len(bundle.review_sheets) == 1
    assert bundle.primary_text_review_required_pages == [110]
    review = bundle.review_sheets[0]
    assert review.page_number == 110
    assert review.path == "handoff_review/page-0110-review.png"
    assert review.mime_type == "image/png"
    assert review.sha256 == hashlib.sha256(b"review-sheet-110").hexdigest()
    assert bundle.response_schema["title"] == "StudyDraft"
    assert len(bundle.response_schema_sha256) == 64


def test_bundle_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)

    first = build_study_draft_request_bundle(
        subject="criminal",
        question="22",
        source=_source(),
        run_dir=run_dir,
        instruction_dir=instruction_dir,
    )
    second = build_study_draft_request_bundle(
        subject="criminal",
        question="22",
        source=_source(),
        run_dir=run_dir,
        instruction_dir=instruction_dir,
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_missing_required_instruction_stops_bundle_creation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)
    missing = instruction_dir / "22_刑法_Ankiカード作成仕様_科目別特則.md"
    missing.unlink()

    with pytest.raises(FileNotFoundError, match="Required instruction file is missing"):
        build_study_draft_request_bundle(
            subject="criminal",
            question="22",
            source=_source(),
            run_dir=run_dir,
            instruction_dir=instruction_dir,
        )


def test_invalid_problem_packet_stops_bundle_creation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)
    (run_dir / "problem_validation.json").write_text(
        json.dumps({"valid": False}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="problem_validation.json is not valid=true"):
        build_study_draft_request_bundle(
            subject="criminal",
            question="22",
            source=_source(),
            run_dir=run_dir,
            instruction_dir=instruction_dir,
        )


def test_handoff_identity_mismatch_stops_bundle_creation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)
    handoff = run_dir / "criminal_22_handoff.md"
    handoff.write_text(
        handoff.read_text(encoding="utf-8").replace(
            'question: "22"',
            'question: "23"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Bundle source identity mismatch"):
        build_study_draft_request_bundle(
            subject="criminal",
            question="22",
            source=_source(),
            run_dir=run_dir,
            instruction_dir=instruction_dir,
        )


def test_review_sheet_must_exist_inside_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)
    (run_dir / "handoff_review/page-0110-review.png").unlink()

    with pytest.raises(FileNotFoundError, match="Review sheet is missing"):
        build_study_draft_request_bundle(
            subject="criminal",
            question="22",
            source=_source(),
            run_dir=run_dir,
            instruction_dir=instruction_dir,
        )


def test_instruction_selection_is_subject_specific() -> None:
    assert required_instruction_names("criminal") == [
        "00_論文作成・登録_本番_プロジェクト指示.md",
        "01_論文作成_共通原則_原文・赤字・資料確認.md",
        "10_Ankiカード作成仕様_共通.md",
        "22_刑法_Ankiカード作成仕様_科目別特則.md",
        "30_Obsidianノート作成仕様_共通.md",
        "31_Obsidian_Vault直接更新仕様.md",
    ]

    with pytest.raises(ValueError, match="No Anki subject instruction mapping"):
        required_instruction_names("unknown-subject")


def test_supplemental_identity_must_match_request(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)

    supplemental = SupplementalRetrievalBundle(
        subject="criminal",
        question="different-question",
    )

    with pytest.raises(ValueError, match="Supplemental retrieval identity mismatch"):
        build_study_draft_request_bundle(
            subject="criminal",
            question="22",
            source=_source(),
            run_dir=run_dir,
            instruction_dir=instruction_dir,
            supplemental=supplemental,
        )
