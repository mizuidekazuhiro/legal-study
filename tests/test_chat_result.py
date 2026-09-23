import json
from pathlib import Path

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
