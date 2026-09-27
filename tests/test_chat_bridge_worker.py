import hashlib
import json
from pathlib import Path

import pytest

import legal_study.chat_bridge_worker as bridge_worker
from legal_study.chat_bridge_worker import (
    BridgeCommand,
    BridgeWorkerResult,
    process_bridge_command,
    watch_bridge_commands,
)
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


def _write_result(
    path: Path, source_sha: str, run_id: str, *, include_cards: bool = True
) -> None:
    payload = {
        "schema_version": "chat_study_result.v1",
        "source": {
            "subject": "criminal",
            "question": "16",
            "source_sha256": source_sha,
            "requested_pages": [200],
            "run_id": run_id,
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
    if not include_cards:
        payload["anki_cards"] = []
        payload.pop("problem_card_extra")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_obsidian_command_is_idempotent_and_receipted(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)

    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)

    result_file = bridge / "10_approved" / "criminal-q16.json"
    _write_result(result_file, source_sha, run_id)
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


def test_process_uses_command_bytes_captured_by_stability_gate(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    result_file = bridge / "10_approved" / "result.json"
    _write_result(result_file, source_sha, run_id)
    command = BridgeCommand(
        command_id="snapshot-001",
        action="apply_obsidian",
        subject="criminal",
        question="16",
        source_sha256=source_sha,
        run_id=run_id,
        result_file="10_approved/result.json",
        result_sha256=file_sha256(result_file),
        approved_at="2026-09-23T00:00:00Z",
        approval_text="承認",
    )
    command_path = bridge / "20_commands" / "snapshot.json"
    command_path.write_text(command.model_dump_json(), encoding="utf-8")
    stable_bytes = command_path.read_bytes()
    command_path.write_text("{}", encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    outcome = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
        command_bytes=stable_bytes,
    )

    assert outcome.status == "SUCCESS"
    receipt = json.loads(Path(outcome.receipt_path or "").read_text(encoding="utf-8"))
    assert receipt["command_sha256"] == hashlib.sha256(stable_bytes).hexdigest()


def test_success_receipt_restores_missing_local_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    result_file = bridge / "10_approved" / "result.json"
    _write_result(result_file, source_sha, run_id)
    command = BridgeCommand(
        command_id="restore-001", action="apply_obsidian", subject="criminal",
        question="16", source_sha256=source_sha, run_id=run_id,
        result_file="10_approved/result.json", result_sha256=file_sha256(result_file),
        approved_at="2026-09-23T00:00:00Z", approval_text="承認",
    )
    command_path = bridge / "20_commands" / "restore.json"
    command_path.write_text(command.model_dump_json(), encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = process_bridge_command(
        command_path=command_path, bridge_root=bridge,
        obsidian_inbox=inbox, settings=settings,
    )
    assert first.status == "SUCCESS"
    settings.state_db.unlink()
    monkeypatch.setattr(
        bridge_worker,
        "apply_chat_result",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable receipt must prevent replay")
        ),
    )

    second = process_bridge_command(
        command_path=command_path, bridge_root=bridge,
        obsidian_inbox=inbox, settings=settings,
    )

    assert second.processed is False
    assert second.status == "ALREADY_COMPLETED"


def test_command_id_collision_is_failed_without_raising(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    command = BridgeCommand(
        command_id="collision-001", action="apply_obsidian", subject="criminal",
        question="16", source_sha256="a" * 64, run_id="r" * 64,
        result_file="10_approved/missing.json", result_sha256="b" * 64,
        approved_at="2026-09-23T00:00:00Z", approval_text="承認",
    )
    command_path = bridge / "20_commands" / "collision.json"
    command_path.write_text(command.model_dump_json(), encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = process_bridge_command(
        command_path=command_path, bridge_root=bridge,
        obsidian_inbox=inbox, settings=settings,
    )
    assert first.status == "FAILED"
    changed = command.model_copy(update={"approval_text": "再承認"})
    command_path.write_text(changed.model_dump_json(), encoding="utf-8")

    second = process_bridge_command(
        command_path=command_path, bridge_root=bridge,
        obsidian_inbox=inbox, settings=settings,
    )

    assert second.status == "FAILED"
    assert "reused with different command bytes" in (second.error or "")
    assert Path(second.receipt_path or "").name.startswith("collision-")


def test_obsidian_only_command_does_not_require_anki_payload(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    result_file = bridge / "10_approved" / "criminal-q16.json"
    _write_result(result_file, source_sha, run_id, include_cards=False)
    command = {
        "schema_version": "chat_bridge_command.v1",
        "command_id": "criminal-q16-obsidian-only-001",
        "action": "apply_obsidian",
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
    command_path = bridge / "20_commands" / "obsidian-only.json"
    command_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()

    result = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
    )

    assert result.status == "SUCCESS"
    assert (inbox / "30_論文マスター/刑法/刑法_第16問.md").is_file()
    assert not list((bridge / "99_failed").iterdir())


def test_result_hash_mismatch_is_receipted_without_writing_obsidian(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    result_file = bridge / "10_approved" / "criminal-q16.json"
    _write_result(result_file, source_sha, run_id)
    command = {
        "schema_version": "chat_bridge_command.v1",
        "command_id": "criminal-q16-bad-hash-001",
        "action": "apply_obsidian",
        "subject": "criminal",
        "question": "16",
        "source_sha256": source_sha,
        "run_id": run_id,
        "result_file": "10_approved/criminal-q16.json",
        "result_sha256": "f" * 64,
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
        "notion_registration_authorized": False,
    }
    command_path = bridge / "20_commands" / "bad-hash.json"
    command_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()

    result = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
    )

    assert result.status == "FAILED"
    assert "SHA" in (result.error or "")
    assert not list(inbox.rglob("*.md"))


def test_command_source_mismatch_is_receipted_without_writing_obsidian(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)

    result_file = bridge / "10_approved" / "approved.json"
    _write_result(result_file, source_sha, run_id)
    command = {
        "schema_version": "chat_bridge_command.v1",
        "command_id": "source-mismatch-001",
        "action": "apply_obsidian",
        "subject": "criminal",
        "question": "16",
        "source_sha256": "b" * 64,
        "run_id": run_id,
        "result_file": "10_approved/approved.json",
        "result_sha256": file_sha256(result_file),
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
    }
    command_path = bridge / "20_commands" / "source-mismatch.json"
    command_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
    inbox = tmp_path / "Obsidian_Inbox"
    inbox.mkdir()

    outcome = process_bridge_command(
        command_path=command_path,
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
    )

    assert outcome.status == "FAILED"
    receipt = json.loads(Path(outcome.receipt_path or "").read_text(encoding="utf-8"))
    assert "Could not resolve exactly one local run" in receipt["error"]
    assert not list(inbox.rglob("*.md"))


def test_notion_command_requires_explicit_authorization(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)

    bridge = tmp_path / "LegalStudy_ChatBridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)

    result_file = bridge / "10_approved" / "criminal-q16.json"
    _write_result(result_file, source_sha, run_id)

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
    _write_result(result_file, source_sha, run_id)
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


def test_watcher_isolates_malformed_command(tmp_path: Path, monkeypatch) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (bridge / "20_commands" / "broken.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(bridge_worker.time, "sleep", lambda _seconds: None)
    watcher = watch_bridge_commands(
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
        poll_interval_seconds=0.01,
        stable_seconds=0,
    )

    result = next(watcher)

    assert result.status == "INVALID_COMMAND"
    assert Path(result.receipt_path or "").is_file()


def test_watcher_continues_to_later_command_after_malformed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (bridge / "20_commands" / "a-broken.json").write_text("{", encoding="utf-8")
    valid = BridgeCommand(
        command_id="valid-001", action="apply_obsidian", subject="criminal",
        question="16", source_sha256="a" * 64, run_id="r" * 64,
        result_file="10_approved/result.json", result_sha256="b" * 64,
        approved_at="2026-09-23T00:00:00Z", approval_text="承認",
    )
    (bridge / "20_commands" / "b-valid.json").write_text(
        valid.model_dump_json(), encoding="utf-8"
    )
    monkeypatch.setattr(bridge_worker.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        bridge_worker,
        "process_bridge_command",
        lambda **_kwargs: BridgeWorkerResult(processed=True, status="SUCCESS"),
    )
    watcher = watch_bridge_commands(
        bridge_root=bridge, obsidian_inbox=inbox, settings=settings,
        poll_interval_seconds=0.01, stable_seconds=0,
    )

    assert next(watcher).status == "INVALID_COMMAND"
    assert next(watcher).status == "SUCCESS"


def test_watcher_retries_transient_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    command = BridgeCommand(
        command_id="retry-001", action="apply_obsidian", subject="criminal",
        question="16", source_sha256="a" * 64, run_id="r" * 64,
        result_file="10_approved/result.json", result_sha256="b" * 64,
        approved_at="2026-09-23T00:00:00Z", approval_text="承認",
    )
    (bridge / "20_commands" / "retry.json").write_text(
        command.model_dump_json(), encoding="utf-8"
    )
    calls = 0

    def fail_then_succeed(**_kwargs: object) -> BridgeWorkerResult:
        nonlocal calls
        calls += 1
        return BridgeWorkerResult(
            processed=True, status="FAILED" if calls == 1 else "SUCCESS"
        )

    clock = iter(range(0, 1000, 31))
    monkeypatch.setattr(bridge_worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bridge_worker.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bridge_worker, "process_bridge_command", fail_then_succeed)
    watcher = watch_bridge_commands(
        bridge_root=bridge, obsidian_inbox=inbox, settings=settings,
        poll_interval_seconds=0.01, stable_seconds=0, retry_seconds=30,
    )

    assert next(watcher).status == "FAILED"
    assert next(watcher).status == "SUCCESS"
    assert calls == 2


def test_notion_factory_failure_is_receipted(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    settings.ensure()
    _run, run_id, source_sha = _write_run(settings)
    bridge = tmp_path / "bridge"
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    result_file = bridge / "10_approved" / "result.json"
    _write_result(result_file, source_sha, run_id)
    command = BridgeCommand(
        command_id="notion-init-001", action="register_notion", subject="criminal",
        question="16", source_sha256=source_sha, run_id=run_id,
        result_file="10_approved/result.json", result_sha256=file_sha256(result_file),
        approved_at="2026-09-23T00:00:00Z", approval_text="承認",
        notion_registration_authorized=True,
    )
    command_path = bridge / "20_commands" / "notion.json"
    command_path.write_text(command.model_dump_json(), encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def fail_factory():
        raise RuntimeError("missing Notion credentials")

    result = process_bridge_command(
        command_path=command_path, bridge_root=bridge, obsidian_inbox=inbox,
        settings=settings, notion_registrar_factory=fail_factory,
    )

    assert result.status == "FAILED"
    assert Path(result.receipt_path or "").is_file()
    assert "missing Notion credentials" in (result.error or "")
