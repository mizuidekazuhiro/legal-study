import json
from pathlib import Path

from legal_study.study_draft import StudyDraft
from legal_study.study_draft_validation import validate_study_draft_evidence


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
        "reconciliation": {
            "records": [
                {
                    "id": "recon-110",
                    "page_number": 110,
                    "source_kind": "full_page",
                    "status": "AUTO_VERIFIED",
                }
            ]
        },
        "markers": [],
        "logical_markers": [],
        "ocr_supplements": [],
        "needs_review": [
            {
                "id": "p0110-page_review",
                "page_number": 110,
                "source_kind": "page_review",
                "status": "NEEDS_REVIEW",
                "issues": [
                    {
                        "id": "issue-110-red",
                        "status": "NEEDS_REVIEW",
                    }
                ],
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
    (run_dir / "handoff_review/page-0110-review.png").write_bytes(b"png")


def _payload() -> dict[str, object]:
    primary = {
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
            {"name": "00_論文作成・登録_本番_プロジェクト指示.md"}
        ],
        "visual_reviews": [
            {
                "page_number": 110,
                "review_sheet_path": "handoff_review/page-0110-review.png",
                "reviewed": True,
                "unresolved_issue_ids": ["issue-110-red"],
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
                    "evidence_refs": [primary],
                    "transformation": "EXTRACTED_REORDERED",
                },
                "back": {
                    "text": "【回答】<br>資料に基づく回答。",
                    "evidence_refs": [primary],
                    "transformation": "MINIMAL_CONNECTIVE",
                },
                "extra": {
                    "text": "【正本｜Obsidian「講師答案例」原文】<br>講師答案本文",
                    "evidence_refs": [primary],
                    "transformation": "VERBATIM",
                },
                "anki_tags": ["刑法"],
                "anki_deck": "刑法 論文試験",
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
                            "evidence_refs": [primary],
                            "transformation": "VERBATIM",
                        }
                    ],
                }
            ],
        },
        "unresolved": [
            {
                "code": "RED_MARK_REVIEW",
                "message": "赤い書込みの意味は視覚確認が必要。",
                "evidence_refs": [
                    {
                        "evidence_id": "issue-110-red",
                        "page_number": 110,
                        "source_kind": "review_issue",
                        "artifact_path": "canonical_source.json",
                        "source_anchor": None,
                        "review_required": True,
                    }
                ],
                "blocking": True,
            }
        ],
    }


def test_valid_draft_evidence_resolves_to_run_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    draft = StudyDraft.model_validate(_payload())

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is True
    assert result.issues == []


def test_unknown_evidence_id_is_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["anki_cards"][0]["back"]["evidence_refs"][0]["evidence_id"] = "invented"
    draft = StudyDraft.model_validate(payload)

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is False
    assert "UNKNOWN_EVIDENCE_ID" in {issue.code for issue in result.issues}


def test_evidence_provenance_cannot_point_to_another_artifact(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    (run_dir / "other.md").write_text("exists", encoding="utf-8")
    payload = _payload()
    payload["anki_cards"][0]["back"]["evidence_refs"][0]["artifact_path"] = "other.md"
    draft = StudyDraft.model_validate(payload)

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is False
    assert "EVIDENCE_PROVENANCE_MISMATCH" in {issue.code for issue in result.issues}


def test_source_identity_must_match_canonical_source(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    canonical_path = run_dir / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    canonical["source"]["sha256"] = "b" * 64
    canonical_path.write_text(json.dumps(canonical), encoding="utf-8")
    draft = StudyDraft.model_validate(_payload())

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is False
    assert "SOURCE_IDENTITY_MISMATCH" in {issue.code for issue in result.issues}


def test_review_required_flag_cannot_be_dropped(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    payload = _payload()
    payload["unresolved"][0]["evidence_refs"][0]["review_required"] = False
    draft = StudyDraft.model_validate(payload)

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is False
    assert "REVIEW_REQUIRED_NOT_PROPAGATED" in {issue.code for issue in result.issues}


def test_review_sheet_must_match_canonical_handoff_sheet(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    (run_dir / "handoff_review/other.png").write_bytes(b"png")
    payload = _payload()
    payload["visual_reviews"][0]["review_sheet_path"] = "handoff_review/other.png"
    draft = StudyDraft.model_validate(payload)

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is False
    assert "REVIEW_SHEET_MISMATCH" in {issue.code for issue in result.issues}


def test_problem_packet_must_have_passed_validation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)
    (run_dir / "problem_validation.json").write_text(
        json.dumps({"valid": False}),
        encoding="utf-8",
    )
    draft = StudyDraft.model_validate(_payload())

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is False
    assert "PROBLEM_PACKET_NOT_VALID" in {issue.code for issue in result.issues}


def test_mirrored_review_issue_reuses_original_evidence_provenance(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run(run_dir)

    canonical_path = run_dir / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    canonical["logical_markers"] = [
        {
            "id": "p0110-logical-mark-001",
            "page_number": 110,
            "review_status": "NEEDS_REVIEW",
        }
    ]
    canonical["needs_review"][0]["issues"] = [
        {
            "id": "p0110-logical-mark-001",
            "page_number": 110,
            "status": "NEEDS_REVIEW",
            "review_category": "visual_markup_page",
        }
    ]
    canonical_path.write_text(
        json.dumps(canonical, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = _payload()
    payload["unresolved"] = [
        {
            "code": "MARKER_REVIEW",
            "message": "マーカー境界の視覚確認が必要。",
            "evidence_refs": [
                {
                    "evidence_id": "p0110-logical-mark-001",
                    "page_number": 110,
                    "source_kind": "logical_marker",
                    "artifact_path": "canonical_source.json",
                    "source_anchor": None,
                    "review_required": True,
                }
            ],
            "blocking": True,
        }
    ]
    draft = StudyDraft.model_validate(payload)

    result = validate_study_draft_evidence(draft=draft, run_dir=run_dir)

    assert result.valid is True
    assert result.issues == []
