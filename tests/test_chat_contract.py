from pathlib import Path

import pytest

from legal_study.chat_bridge_worker import BridgeAction
from legal_study.chat_contract import (
    build_bridge_command,
    command_filename,
    result_filename,
)
from legal_study.io_utils import file_sha256


def _packet_manifest() -> dict:
    source_sha = "a" * 64
    run_id = "r" * 64
    filename = result_filename(
        subject="criminal",
        question="16",
        source_sha256=source_sha,
        run_id=run_id,
    )
    return {
        "schema_version": "chat_packet.v1",
        "subject": "criminal",
        "question": "16",
        "source_sha256": source_sha,
        "run_id": run_id,
        "requested_pages": [150, 151, 152],
        "chat_contract": {
            "schema_version": "chat_bridge_contract.v1",
            "result": {"result_file": f"10_approved/{filename}"},
        },
    }


def test_result_filename_binds_full_source_and_run_identity() -> None:
    manifest = _packet_manifest()
    values = {
        "subject": manifest["subject"],
        "question": manifest["question"],
        "source_sha256": manifest["source_sha256"],
        "run_id": manifest["run_id"],
    }
    first = result_filename(**values)

    assert first == result_filename(**values)
    assert first.endswith(".study_result.json")
    assert manifest["source_sha256"] in first
    assert manifest["run_id"] in first
    assert first != result_filename(**{**values, "source_sha256": "b" * 64})
    assert first != result_filename(**{**values, "run_id": "s" * 64})


def test_approved_only_command_is_deterministic_and_bound_to_packet(
    tmp_path: Path,
) -> None:
    manifest = _packet_manifest()
    saved_result = tmp_path / manifest["chat_contract"]["result"]["result_file"]
    saved_result.parent.mkdir()
    saved_result.write_bytes(b'{"schema_version":"chat_study_result.v1"}')
    result_sha = file_sha256(saved_result)
    options = {
        "packet_manifest": manifest,
        "result_sha256": result_sha,
        "action": BridgeAction.APPLY_OBSIDIAN,
        "approved_at": "2026-09-23T00:00:00Z",
        "approval_text": "承認",
        "notion_registration_authorized": False,
    }

    command = build_bridge_command(**options)

    assert command == build_bridge_command(**options)
    assert command.action == BridgeAction.APPLY_OBSIDIAN
    assert command.notion_registration_authorized is False
    assert command.result_file == manifest["chat_contract"]["result"]["result_file"]
    assert command.result_sha256 == result_sha
    assert command.subject == manifest["subject"]
    assert command.question == manifest["question"]
    assert command.source_sha256 == manifest["source_sha256"]
    assert command.run_id == manifest["run_id"]
    assert command_filename(
        subject="criminal", question="16", run_id=manifest["run_id"],
        result_sha256=result_sha, action=BridgeAction.APPLY_OBSIDIAN,
    ) == f"{command.command_id}.command.json"


@pytest.mark.parametrize("action", [BridgeAction.REGISTER_NOTION, BridgeAction.APPLY_ALL])
def test_explicit_notion_action_builds_authorized_command(action: BridgeAction) -> None:
    command = build_bridge_command(
        packet_manifest=_packet_manifest(),
        result_sha256="c" * 64,
        action=action,
        approved_at="2026-09-23T00:00:00Z",
        approval_text="Notionに登録して",
        notion_registration_authorized=True,
    )

    assert command.action == action
    assert command.notion_registration_authorized is True
    assert command.approval_text == "Notionに登録して"


@pytest.mark.parametrize("action", [BridgeAction.REGISTER_NOTION, BridgeAction.APPLY_ALL])
def test_notion_action_without_explicit_authorization_is_rejected(action: BridgeAction) -> None:
    with pytest.raises(ValueError, match="Notion actions require"):
        build_bridge_command(
            packet_manifest=_packet_manifest(),
            result_sha256="c" * 64,
            action=action,
            approved_at="2026-09-23T00:00:00Z",
            approval_text="承認",
            notion_registration_authorized=False,
        )


def test_builder_rejects_tampered_result_path_and_unapproved_notion_flag() -> None:
    manifest = _packet_manifest()
    manifest["chat_contract"]["result"]["result_file"] = "00_pending/result.json"
    with pytest.raises(ValueError, match="result_file"):
        build_bridge_command(
            packet_manifest=manifest,
            result_sha256="c" * 64,
            action=BridgeAction.APPLY_OBSIDIAN,
            approved_at="2026-09-23T00:00:00Z",
            approval_text="承認",
            notion_registration_authorized=False,
        )

    with pytest.raises(ValueError, match="apply_obsidian"):
        build_bridge_command(
            packet_manifest=_packet_manifest(),
            result_sha256="c" * 64,
            action=BridgeAction.APPLY_OBSIDIAN,
            approved_at="2026-09-23T00:00:00Z",
            approval_text="承認",
            notion_registration_authorized=True,
        )


def test_builder_requires_timezone_aware_iso8601_approval_time() -> None:
    with pytest.raises(ValueError, match="ISO8601"):
        build_bridge_command(
            packet_manifest=_packet_manifest(),
            result_sha256="c" * 64,
            action=BridgeAction.APPLY_OBSIDIAN,
            approved_at="2026-09-23T00:00:00",
            approval_text="承認",
        )
