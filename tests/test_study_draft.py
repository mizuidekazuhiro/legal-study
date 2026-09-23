import pytest
from pydantic import ValidationError

from legal_study.study_draft import StudyDraft, study_draft_json_schema


def _payload() -> dict[str, object]:
    evidence = {
        "evidence_id": "handoff-p110-answer",
        "page_number": 110,
        "source_kind": "handoff_text",
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
            "stable_page_ids": ["stable-110", "stable-111"],
            "requested_pages": [110, 111],
            "run_id": "run-22",
            "handoff_path": "criminal_22_handoff.md",
            "canonical_source_path": "canonical_source.json",
            "problem_validation_path": "problem_validation.json",
        },
        "instruction_sources": [
            {"name": "00_論文作成・登録_本番_プロジェクト指示.md"},
            {"name": "01_論文作成_共通原則_原文・赤字・資料確認.md"},
            {"name": "10_Ankiカード作成仕様_共通.md"},
            {"name": "22_刑法_Ankiカード作成仕様_科目別特則.md"},
            {"name": "30_Obsidianノート作成仕様_共通.md"},
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
                "pdf_page": "110-111",
                "topic": "サンプル",
                "subtopic": "サンプル",
                "level": "L4",
                "anki_type": "基本",
                "front": {
                    "text": "事案を踏まえて処理順序を示せ。",
                    "evidence_refs": [evidence],
                    "transformation": "EXTRACTED_REORDERED",
                },
                "back": {
                    "text": "【回答】<br>資料に基づく回答。",
                    "evidence_refs": [evidence],
                    "transformation": "MINIMAL_CONNECTIVE",
                },
                "extra": {
                    "text": "【正本｜Obsidian「講師答案例」原文】<br>講師答案全文。",
                    "evidence_refs": [evidence],
                    "transformation": "VERBATIM",
                },
                "anki_tags": ["刑法", "論文マスター"],
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
                            "text": "講師答案全文。",
                            "evidence_refs": [evidence],
                            "transformation": "VERBATIM",
                        }
                    ],
                }
            ],
        },
        "unresolved": [],
    }


def test_study_draft_contract_round_trips() -> None:
    draft = StudyDraft.model_validate(_payload())

    assert draft.schema_version == "study_draft.v1"
    assert draft.source.source_sha256 == "a" * 64
    assert draft.source.stable_page_ids == ["stable-110", "stable-111"]
    assert draft.anki_cards[0].status == "DRAFT"
    assert draft.anki_cards[0].export_to_anki is False
    assert draft.obsidian_note is not None
    assert draft.obsidian_note.relative_path == "30_論文マスター/刑法/第22問.md"


def test_contract_requires_provenance_for_generated_text() -> None:
    payload = _payload()
    payload["anki_cards"][0]["back"]["evidence_refs"] = []

    with pytest.raises(ValidationError):
        StudyDraft.model_validate(payload)


def test_contract_rejects_registration_ready_state() -> None:
    payload = _payload()
    payload["anki_cards"][0]["status"] = "Ready"

    with pytest.raises(ValidationError):
        StudyDraft.model_validate(payload)

    payload = _payload()
    payload["anki_cards"][0]["export_to_anki"] = True

    with pytest.raises(ValidationError):
        StudyDraft.model_validate(payload)


@pytest.mark.parametrize(
    ("location", "bad_path"),
    [
        ("handoff_path", "../outside.md"),
        ("canonical_source_path", "/tmp/canonical_source.json"),
    ],
)
def test_contract_rejects_run_artifact_escape(location: str, bad_path: str) -> None:
    payload = _payload()
    payload["source"][location] = bad_path

    with pytest.raises(ValidationError):
        StudyDraft.model_validate(payload)


def test_contract_rejects_vault_escape() -> None:
    payload = _payload()
    payload["obsidian_note"]["relative_path"] = "../第22問.md"

    with pytest.raises(ValidationError):
        StudyDraft.model_validate(payload)


def test_json_schema_exposes_immutable_source_and_draft_only_fields() -> None:
    schema = study_draft_json_schema()
    source_schema = schema["$defs"]["DraftSource"]
    card_schema = schema["$defs"]["AnkiCardDraft"]

    assert "source_sha256" in source_schema["required"]
    assert "stable_page_ids" in source_schema["required"]
    assert "run_id" in source_schema["required"]
    assert "note_id" not in card_schema["properties"]
    assert card_schema["properties"]["status"]["const"] == "DRAFT"
    assert card_schema["properties"]["export_to_anki"]["const"] is False
