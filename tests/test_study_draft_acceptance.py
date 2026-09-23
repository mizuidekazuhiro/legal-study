import json
from pathlib import Path

from legal_study.study_draft_acceptance import accept_study_draft_response


def _write_run(run_dir: Path) -> None:
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
                "reconciled_text": "講師答案本文",
                "repair_review_count": 0,
            }
        ],
        "reconciliation": {"records": []},
        "markers": [],
        "logical_markers": [],
        "ocr_supplements": [],
        "needs_review": [],
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
page_review_count: 0
review_issue_count: 0
---

# Page Reading Pack

## PDF page 110

### Primary Reading Text

講師答案本文
""",
        encoding="utf-8",
    )
    (run_dir / "handoff_review/page-0110-review.png").write_bytes(b"png")


def _payload() -> dict[str, object]:
    evidence = {
        "evidence_id": "page:110:primary_text",
        "page_number": 110,
        "source_kind": "handoff_primary_text",
        "artifact_path": "criminal_22_handoff.md",
        "source_anchor": "PDF page 110 / Primary Reading Text",
        "review_required": False,
    }
    return {
        "schema_version": "study_draft.v1",
        "subject": "criminal",
        "question": "22",
        "source": {
            "source_sha256": "a" * 64,
            "source_snapshot_path": "C:/Users/test/.legal-study/sources/sha256/source.pdf",
            "stable_page_ids": ["stable-110"],
            "requested_pages": [110],
            "run_id": "run-22",
            "handoff_path": "criminal_22_handoff.md",
            "canonical_source_path": "canonical_source.json",
            "problem_validation_path": "problem_validation.json",
        },
        "instruction_sources": [
            {
                "name": "00_論文作成・登録_本番_プロジェクト指示.md",
                "sha256": None,
            }
        ],
        "visual_reviews": [
            {
                "page_number": 110,
                "review_sheet_path": "handoff_review/page-0110-review.png",
                "reviewed": True,
                "unresolved_issue_ids": [],
            }
        ],
        "anki_cards": [
            {
                "name": "刑法第22問-01",
                "subject": "刑法",
                "source": "論文マスター_刑法",
                "pdf_page": "110",
                "topic": "サンプル",
                "subtopic": "サンプル",
                "level": "L4",
                "anki_type": "基本",
                "front": {
                    "text": "事案を踏まえて処理順序を示せ。",
                    "evidence_refs": [evidence],
                    "transformation": "EXTRACTED_REORDERED",
                    "review_required": False,
                },
                "back": {
                    "text": "【回答】<br>資料に基づく回答。",
                    "evidence_refs": [evidence],
                    "transformation": "MINIMAL_CONNECTIVE",
                    "review_required": False,
                },
                "extra": {
                    "text": "【正本｜Obsidian「講師答案例」原文】<br>講師答案本文",
                    "evidence_refs": [evidence],
                    "transformation": "VERBATIM",
                    "review_required": False,
                },
                "anki_tags": ["刑法"],
                "anki_deck": "刑法 論文試験",
                "status": "DRAFT",
                "export_to_anki": False,
            }
        ],
        "obsidian_note": {
            "relative_path": "30_論文マスター/刑法/第22問.md",
            "sections": [
                {
                    "heading": "講師答案例",
                    "blocks": [
                        {
                            "text": "講師答案本文",
                            "evidence_refs": [evidence],
                            "transformation": "VERBATIM",
                            "review_required": False,
                        }
                    ],
                }
            ],
        },
        "unresolved": [],
    }


def _raw(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_valid_response_is_accepted_and_saved_atomically(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)

    result = accept_study_draft_response(
        raw_response_text=_raw(_payload()),
        run_dir=run_dir,
    )

    assert result.accepted is True
    assert result.output_path == "study_draft.json"
    assert result.issues == []
    assert result.draft_sha256 is not None
    saved = json.loads((run_dir / "study_draft.json").read_text(encoding="utf-8"))
    assert saved["subject"] == "criminal"
    assert saved["anki_cards"][0]["status"] == "DRAFT"
    assert saved["anki_cards"][0]["export_to_anki"] is False
    assert not list(run_dir.glob(".study_draft.json.*.tmp"))


def test_invalid_json_is_rejected_without_output(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)

    result = accept_study_draft_response(
        raw_response_text="{not-json",
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert {issue.code for issue in result.issues} == {"INVALID_JSON"}
    assert not (run_dir / "study_draft.json").exists()


def test_schema_violation_is_rejected_without_output(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["anki_cards"][0]["status"] = "Ready"

    result = accept_study_draft_response(
        raw_response_text=_raw(payload),
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert "SCHEMA_VALIDATION_FAILED" in {issue.code for issue in result.issues}
    assert not (run_dir / "study_draft.json").exists()


def test_unknown_evidence_is_rejected_without_output(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["anki_cards"][0]["back"]["evidence_refs"][0]["evidence_id"] = "invented"

    result = accept_study_draft_response(
        raw_response_text=_raw(payload),
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert "UNKNOWN_EVIDENCE_ID" in {issue.code for issue in result.issues}
    assert not (run_dir / "study_draft.json").exists()


def test_blocking_uncertainty_is_not_accepted_as_candidate(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["unresolved"] = [
        {
            "code": "UNREADABLE",
            "message": "判読不能",
            "evidence_refs": [
                {
                    "evidence_id": "page:110:primary_text",
                    "page_number": 110,
                    "source_kind": "handoff_primary_text",
                    "artifact_path": "criminal_22_handoff.md",
                    "source_anchor": "PDF page 110 / Primary Reading Text",
                    "review_required": False,
                }
            ],
            "blocking": True,
        }
    ]

    result = accept_study_draft_response(
        raw_response_text=_raw(payload),
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert "BLOCKING_UNRESOLVED" in {issue.code for issue in result.issues}
    assert not (run_dir / "study_draft.json").exists()


def test_incomplete_visual_review_is_not_accepted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["visual_reviews"][0]["reviewed"] = False

    result = accept_study_draft_response(
        raw_response_text=_raw(payload),
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert "VISUAL_REVIEW_INCOMPLETE" in {issue.code for issue in result.issues}
    assert not (run_dir / "study_draft.json").exists()


def test_review_required_draft_text_is_not_accepted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["anki_cards"][0]["back"]["review_required"] = True

    result = accept_study_draft_response(
        raw_response_text=_raw(payload),
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert "DRAFT_TEXT_REVIEW_REQUIRED" in {issue.code for issue in result.issues}
    assert not (run_dir / "study_draft.json").exists()


def test_existing_different_candidate_is_never_overwritten(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    output = run_dir / "study_draft.json"
    output.write_text('{"existing": true}\n', encoding="utf-8")

    result = accept_study_draft_response(
        raw_response_text=_raw(_payload()),
        run_dir=run_dir,
    )

    assert result.accepted is False
    assert "OUTPUT_ALREADY_EXISTS" in {issue.code for issue in result.issues}
    assert output.read_text(encoding="utf-8") == '{"existing": true}\n'


def test_identical_existing_candidate_is_idempotently_accepted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)

    first = accept_study_draft_response(
        raw_response_text=_raw(_payload()),
        run_dir=run_dir,
    )
    before = (run_dir / "study_draft.json").read_bytes()

    second = accept_study_draft_response(
        raw_response_text=_raw(_payload()),
        run_dir=run_dir,
    )

    assert first.accepted is True
    assert second.accepted is True
    assert second.issues == []
    assert second.draft_sha256 == first.draft_sha256
    assert (run_dir / "study_draft.json").read_bytes() == before
