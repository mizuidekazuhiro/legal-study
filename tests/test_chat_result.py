import json
from pathlib import Path

import pytest

from legal_study.chat_result import (
    ChatStudyResult,
    apply_chat_result,
    expanded_anki_cards,
    validate_chat_result,
)


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    manifest = {
        "schema_version": "1",
        "run_id": "r" * 64,
        "input_hash": "i" * 64,
        "created_at": "2026-09-23T00:00:00Z",
        "updated_at": "2026-09-23T00:00:00Z",
        "subject": "criminal",
        "question": "12",
        "requested_pages": [109, 110],
        "source": {
            "original_path": "C:/input/source.pdf",
            "original_filename": "論文マスター_刑法.pdf",
            "source_size": 10,
            "source_mtime_ns": 1,
            "sha256": "a" * 64,
            "snapshot_path": "C:/store/source.pdf",
            "snapshot_created_at": "2026-09-23T00:00:00Z",
        },
        "output_dir": ".",
        "page_count": 286,
        "pipeline_config": {},
        "app_version": "0.1.0",
        "python_version": "3.13.7",
        "platform": "Windows",
        "pymupdf_version": "1.28.2",
    }
    (run / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return run


def _payload() -> dict:
    return {
        "schema_version": "chat_study_result.v1",
        "source": {
            "subject": "criminal",
            "question": "12",
            "source_sha256": "a" * 64,
            "requested_pages": [109, 110],
        },
        "reviewed_pages": [109, 110],
        "unresolved": [],
        "problem_card_extra": "【講師答案全文】<br><br>全文",
        "anki_cards": [
            {
                "name": "刑法第12問-01",
                "scope": "problem",
                "learning_type": "B",
                "subject": "刑法",
                "source": "論文マスター_刑法.pdf",
                "pdf_page": "PDF pp.109-110",
                "pdf_page_number": 109,
                "topic": "答案構成",
                "subtopic": "処理順序",
                "level": "L4",
                "anki_type": "基本",
                "front": "【L4 答案構成】<br><br>問",
                "back": "【回答】<br><br>答",
                "extra": None,
                "anki_tags": ["刑法"],
                "anki_deck": "刑法 論文試験",
            },
            {
                "name": "刑法共通規範-刑3",
                "scope": "common_rule",
                "learning_type": "E",
                "subject": "刑法",
                "source": "登録済みObsidian論証パターン",
                "pdf_page": "PDF p.109",
                "pdf_page_number": 109,
                "topic": "間接正犯",
                "subtopic": "",
                "level": "L2",
                "anki_type": "基本",
                "front": "【L2 一問一答】<br><br>問",
                "back": "規範<br><br>本文",
                "extra": "【出典】<br><br>Obsidian",
                "anki_tags": ["共通規範"],
                "anki_deck": "刑法 論文試験",
            },
        ],
        "obsidian_note": {
            "relative_path": "30_論文マスター/刑法/刑法_第12問.md",
            "markdown": "---\ntitle: 第12問\n---\n\n本文\n",
        },
    }


def test_valid_chat_result_binds_to_exact_run(tmp_path: Path) -> None:
    run = _run(tmp_path)
    result_file = tmp_path / "study_result.json"
    result_file.write_text(
        json.dumps(_payload(), ensure_ascii=False),
        encoding="utf-8",
    )

    report = validate_chat_result(result_path=result_file, run_dir=run)

    assert report.valid is True
    assert report.issues == []
    assert report.card_count == 2
    assert report.problem_card_count == 1
    assert report.common_rule_card_count == 1


def test_legacy_result_filename_must_bind_to_expected_run(tmp_path: Path) -> None:
    run = _run(tmp_path)
    payload = _payload()
    result_file = tmp_path / "unbound.study_result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_chat_result(
        result_path=result_file,
        run_dir=run,
        expected_run_id="r" * 64,
    )

    assert report.valid is False
    assert "SOURCE_RUN_ID_UNBOUND" in report.issues


def test_result_source_run_id_must_match_expected_run(tmp_path: Path) -> None:
    run = _run(tmp_path)
    payload = _payload()
    payload["source"]["run_id"] = "x" * 64
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_chat_result(
        result_path=result_file,
        run_dir=run,
        expected_run_id="r" * 64,
    )

    assert report.valid is False
    assert "SOURCE_RUN_ID_MISMATCH" in report.issues


def test_obsidian_only_validation_accepts_no_cards_but_full_validation_does_not(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    payload = _payload()
    payload["anki_cards"] = []
    payload.pop("problem_card_extra")
    result_file = tmp_path / "study_result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    full = validate_chat_result(result_path=result_file, run_dir=run)
    obsidian = validate_chat_result(
        result_path=result_file,
        run_dir=run,
        validation_scope="obsidian",
    )

    assert full.valid is False
    assert "CRIMINAL_L4_B_CARD_MISSING" in full.issues
    assert "CRIMINAL_PROBLEM_CARDS_MISSING" in full.issues
    assert obsidian.valid is True
    assert obsidian.card_count == 0


def test_obsidian_only_validation_ignores_anki_policy_fields(tmp_path: Path) -> None:
    run = _run(tmp_path)
    payload = _payload()
    payload["problem_card_extra"] = None
    payload["anki_cards"][0].update(
        {
            "subject": "wrong subject",
            "anki_deck": "wrong deck",
            "front": "no html break",
            "back": "no html break",
        }
    )
    payload["anki_cards"][1]["extra"] = "no html break"
    result_file = tmp_path / "study_result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    full = validate_chat_result(result_path=result_file, run_dir=run)
    obsidian = validate_chat_result(
        result_path=result_file,
        run_dir=run,
        validation_scope="obsidian",
    )

    assert full.valid is False
    assert "ANKI_SUBJECT_MISMATCH:0" in full.issues
    assert "ANKI_DECK_MISMATCH:0" in full.issues
    assert "ANKI_HTML_BREAK_MISSING:0:front" in full.issues
    assert "ANKI_COMMON_EXTRA_INVALID:1" in full.issues
    assert "PROBLEM_CARD_EXTRA_MISSING" in full.issues
    assert obsidian.valid is True
    assert obsidian.issues == []


@pytest.mark.parametrize(
    ("subject", "display_subject"),
    [("constitutional", "憲法"), ("administrative", "行政法")],
)
def test_full_validation_requires_problem_cards_for_every_supported_subject(
    tmp_path: Path,
    subject: str,
    display_subject: str,
) -> None:
    run = _run(tmp_path)
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["subject"] = subject
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    payload = _payload()
    payload["source"]["subject"] = subject
    payload["anki_cards"] = []
    payload["problem_card_extra"] = None
    payload["obsidian_note"]["relative_path"] = (
        f"30_論文マスター/{display_subject}/{display_subject}_第12問.md"
    )
    result_file = tmp_path / "study_result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_chat_result(result_path=result_file, run_dir=run)

    assert report.valid is False
    assert "PROBLEM_CARDS_MISSING" in report.issues
    assert "CRIMINAL_L4_B_CARD_MISSING" not in report.issues


def test_problem_card_page_must_be_in_requested_pages(tmp_path: Path) -> None:
    run = _run(tmp_path)
    payload = _payload()
    payload["anki_cards"][0]["pdf_page_number"] = 999
    result_file = tmp_path / "study_result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_chat_result(result_path=result_file, run_dir=run)

    assert report.valid is False
    assert "ANKI_PROBLEM_PAGE_OUTSIDE_SOURCE:0" in report.issues


def test_criminal_card_roles_and_l4_b_requirement_are_counted_independently(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    payload = _payload()
    payload["anki_cards"][0]["learning_type"] = "A"
    result_file = tmp_path / "study_result.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_chat_result(result_path=result_file, run_dir=run)

    assert report.problem_card_count == 1
    assert report.common_rule_card_count == 1
    assert "CRIMINAL_L4_B_CARD_MISSING" in report.issues
    assert "CRIMINAL_PROBLEM_CARDS_MISSING" not in report.issues


def test_malformed_chat_result_json_is_rejected(tmp_path: Path) -> None:
    run = _run(tmp_path)
    result_file = tmp_path / "study_result.json"
    result_file.write_text('{"schema_version":', encoding="utf-8")

    with pytest.raises(ValueError):
        validate_chat_result(result_path=result_file, run_dir=run)


def test_shared_problem_extra_expands_locally(tmp_path: Path) -> None:
    payload = _payload()
    result = ChatStudyResult.model_validate(payload)

    cards = expanded_anki_cards(result)

    assert cards[0]["extra"] == payload["problem_card_extra"]
    assert cards[1]["extra"] == "【出典】<br><br>Obsidian"


def test_source_or_review_mismatch_is_rejected(tmp_path: Path) -> None:
    run = _run(tmp_path)
    payload = _payload()
    payload["source"]["source_sha256"] = "b" * 64
    payload["reviewed_pages"] = [109]
    payload["unresolved"] = ["p110 marker boundary unreadable"]
    result_file = tmp_path / "study_result.json"
    result_file.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    report = validate_chat_result(result_path=result_file, run_dir=run)

    assert report.valid is False
    assert "SOURCE_SHA256_MISMATCH" in report.issues
    assert "VISUAL_REVIEW_INCOMPLETE" in report.issues
    assert "UNRESOLVED_ITEMS_REMAIN" in report.issues


def test_apply_chat_result_materializes_and_writes_inbox_safely(tmp_path: Path) -> None:
    run = _run(tmp_path)
    payload = _payload()
    result_file = tmp_path / "study_result.json"
    result_file.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()

    report = apply_chat_result(
        result_path=result_file,
        run_dir=run,
        obsidian_inbox=inbox,
    )

    assert report.valid is True
    assert report.notion_mutated is False
    assert report.inbox_status == "created"
    destination = inbox / "30_論文マスター/刑法/刑法_第12問.md"
    assert destination.read_text(encoding="utf-8") == payload["obsidian_note"]["markdown"]

    expanded = json.loads(
        (run / "chat_result_expanded.json").read_text(encoding="utf-8")
    )
    assert expanded["anki_cards"][0]["extra"] == payload["problem_card_extra"]
    assert "problem_card_extra" not in expanded


def test_apply_chat_result_refuses_differing_existing_note_by_default(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    payload = _payload()
    result_file = tmp_path / "study_result.json"
    result_file.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    inbox = tmp_path / "Obsidian_Inbox"
    destination = inbox / "30_論文マスター/刑法/刑法_第12問.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("older different content", encoding="utf-8")

    try:
        apply_chat_result(
            result_path=result_file,
            run_dir=run,
            obsidian_inbox=inbox,
        )
    except FileExistsError as exc:
        assert "differing Obsidian file already exists" in str(exc)
    else:
        raise AssertionError("Expected differing existing note to be refused")



def test_all_pages_run_uses_page_count_as_effective_requested_pages(tmp_path: Path) -> None:
    run = _run(tmp_path)
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["requested_pages"] = None
    manifest["page_count"] = 2
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    payload = _payload()
    payload["source"]["requested_pages"] = [1, 2]
    payload["reviewed_pages"] = [1, 2]
    payload["anki_cards"][0]["pdf_page_number"] = 1
    result_file = tmp_path / "study_result.json"
    result_file.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    report = validate_chat_result(result_path=result_file, run_dir=run)

    assert report.valid is True
    assert report.issues == []
