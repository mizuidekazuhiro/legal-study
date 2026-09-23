import json
from pathlib import Path

import pytest

import legal_study.chat_bridge_worker as bridge_worker
from legal_study.chat_bridge_worker import BridgeCommand, process_bridge_command
from legal_study.io_utils import file_sha256
from legal_study.settings import LocalSettings


def _write_run(settings: LocalSettings) -> tuple[Path, str, str]:
    run = settings.runs_dir / "criminal" / "16" / "run"
    run.mkdir(parents=True)
    run_id = "r" * 64
    source_sha = "a" * 64
    manifest = {
        "schema_version": "1",
        "run_id": run_id,
        "input_hash": "i" * 64,
        "created_at": "2026-09-23T00:00:00Z",
        "updated_at": "2026-09-23T00:00:00Z",
        "subject": "criminal",
        "question": "16",
        "requested_pages": [200],
        "source": {
            "original_path": "C:/input/source.pdf",
            "original_filename": "論文マスター_刑法.pdf",
            "source_size": 10,
            "source_mtime_ns": 1,
            "sha256": source_sha,
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
    return run, run_id, source_sha


def _write_result(path: Path, source_sha: str) -> None:
    payload = {
        "schema_version": "chat_study_result.v1",
        "source": {
            "subject": "criminal",
            "question": "16",
            "source_sha256": source_sha,
            "requested_pages": [200],
        },
        "reviewed_pages": [200],
        "unresolved": [],
        "problem_card_extra": "【講師答案全文】<br><br>全文",
        "anki_cards": [
            {
                "name": "刑法第16問-01",
                "scope": "problem",
                "learning_type": "B",
                "subject": "刑法",
                "source": "論文マスター_刑法.pdf",
                "pdf_page": "PDF p.200",
                "pdf_page_number": 200,
                "topic": "答案構成",
                "subtopic": "処理順序",
                "level": "L4",
                "anki_type": "基本",
                "front": "【L4 答案構成】<br><br>問",
                "back": "【回答】<br><br>答",
                "extra": None,
                "anki_tags": ["刑法"],
                "anki_deck": "刑法 論文試験",
            }
        ],
        "obsidian_note": {
            "relative_path": "30_論文マスター/刑法/刑法_第16問.md",
            "markdown": "---\ntitle: 第16問\n---\n\n本文\n",
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_obsidian_command_is_idempotent_and_receipted(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)

    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)

    result_file = bridge / "10_approved" / "criminal-q16.json"
    _write_result(result_file, source_sha)
    result_sha = file_sha256(result_file)

    command = {
        "schema_version": "chat_bridge_command.v1",
        "command_id": "criminal-q16-apply-001",
        "action": "apply_obsidian",
        "subject": "criminal",
        "question": "16",
        "source_sha256": source_sha,
        "run_id": run_id,
        "result_file": "10_approved/criminal-q16.json",
        "result_sha256": result_sha,
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
        "notion_registration_authorized": False,
    }
    command_path = bridge / "20_commands" / "criminal-q16-apply-001.json"
    command_path.write_text(
        json.dumps(command, ensure_ascii=False),
        encoding="utf-8",
    )

    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()

    first = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
    )

    assert first.processed is True
    assert first.status == "SUCCESS"
    destination = inbox / "30_論文マスター/刑法/刑法_第16問.md"
    assert destination.is_file()
    receipt = json.loads(Path(first.receipt_path or "").read_text(encoding="utf-8"))
    assert receipt["obsidian_status"] == "created"
    assert receipt["notion_created"] == 0

    second = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
    )
    assert second.processed is False
    assert second.status == "ALREADY_COMPLETED"


def test_notion_command_requires_explicit_authorization(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)

    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)

    result_file = bridge / "10_approved" / "criminal-q16.json"
    _write_result(result_file, source_sha)

    command = {
        "schema_version": "chat_bridge_command.v1",
        "command_id": "criminal-q16-notion-001",
        "action": "register_notion",
        "subject": "criminal",
        "question": "16",
        "source_sha256": source_sha,
        "run_id": run_id,
        "result_file": "10_approved/criminal-q16.json",
        "result_sha256": file_sha256(result_file),
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
        "notion_registration_authorized": False,
    }
    command_path = bridge / "20_commands" / "criminal-q16-notion-001.json"
    command_path.write_text(
        json.dumps(command, ensure_ascii=False),
        encoding="utf-8",
    )

    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()

    try:
        process_bridge_command(
            command_path=command_path,
            bridge_root=bridge,
            obsidian_inbox=inbox,
            settings=settings,
        )
    except ValueError as exc:
        assert "Notion actions require" in str(exc)
    else:
        raise AssertionError("Expected unauthorized Notion command to be rejected")


@pytest.mark.parametrize(
    ("result_file", "accepted"),
    [
        ("10_approved/result.json", True),
        ("00_pending/result.json", False),
        ("20_commands/result.json", False),
        ("30_receipts/result.json", False),
        ("99_failed/result.json", False),
        ("result.json", False),
        ("../10_approved/result.json", False),
        ("C:/bridge/10_approved/result.json", False),
    ],
)
def test_result_file_must_be_directly_in_approved_folder(
    result_file: str, accepted: bool
) -> None:
    command = {
        "command_id": "path-check-001",
        "action": "apply_obsidian",
        "subject": "criminal",
        "question": "16",
        "source_sha256": "a" * 64,
        "run_id": "r" * 64,
        "result_file": result_file,
        "result_sha256": "b" * 64,
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
    }
    if accepted:
        assert BridgeCommand.model_validate(command).result_file == result_file
    else:
        with pytest.raises(ValueError, match="10_approved"):
            BridgeCommand.model_validate(command)


@pytest.mark.parametrize("action", ["register_notion", "apply_all"])
def test_invalid_result_fails_before_any_external_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    result_file = bridge / "10_approved" / "invalid.json"
    _write_result(result_file, source_sha)
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    payload["unresolved"] = ["要確認"]
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    command = {
        "schema_version": "chat_bridge_command.v1",
        "command_id": f"invalid-{action}-001",
        "action": action,
        "subject": "criminal",
        "question": "16",
        "source_sha256": source_sha,
        "run_id": run_id,
        "result_file": "10_approved/invalid.json",
        "result_sha256": file_sha256(result_file),
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
        "notion_registration_authorized": True,
    }
    command_path = bridge / "20_commands" / f"{action}.json"
    command_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")

    calls = {"obsidian": 0, "notion": 0}

    def fail_if_obsidian_called(**_kwargs: object) -> None:
        calls["obsidian"] += 1
        raise AssertionError("Obsidian must not be called")

    class FakeRegistrar:
        def register(self, **_kwargs: object) -> None:
            calls["notion"] += 1
            raise AssertionError("Notion must not be called")

    monkeypatch.setattr(bridge_worker, "apply_chat_result", fail_if_obsidian_called)
    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()
    result = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
        notion_registrar=FakeRegistrar(),
    )

    assert result.status == "FAILED"
    receipt = json.loads(Path(result.receipt_path or "").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert "UNRESOLVED_ITEMS_REMAIN" in receipt["error"]
    assert calls == {"obsidian": 0, "notion": 0}
