"""Reference naming and command construction for the normal-chat bridge."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from legal_study.chat_bridge_worker import BridgeAction, BridgeCommand


def result_filename(
    *, subject: str, question: str, source_sha256: str, run_id: str
) -> str:
    """Bind a result name to the complete source and run identity."""
    return f"{subject}-q{question}-{source_sha256}-{run_id}.study_result.json"


def command_filename_template(*, subject: str, question: str, run_id: str) -> str:
    return f"{subject}-q{question}-{run_id[:32]}-{{result_sha256}}.{{action}}.command.json"


def command_filename(
    *, subject: str, question: str, run_id: str,
    result_sha256: str, action: BridgeAction,
) -> str:
    return command_filename_template(
        subject=subject, question=question, run_id=run_id
    ).format(result_sha256=result_sha256, action=action.value)


def build_bridge_command(
    *,
    packet_manifest: dict[str, Any],
    result_sha256: str,
    action: BridgeAction,
    approved_at: str,
    approval_text: str,
    notion_registration_authorized: bool = False,
) -> BridgeCommand:
    """Build a command from packet identity and the SHA of saved result bytes.

    This is a local reference implementation, not a ChatGPT API call or a file
    writer. The caller must hash the bytes actually stored in 10_approved.
    """
    if packet_manifest.get("schema_version") != "chat_packet.v1":
        raise ValueError("Unsupported packet manifest schema_version")
    contract = packet_manifest.get("chat_contract") or {}
    if contract.get("schema_version") != "chat_bridge_contract.v1":
        raise ValueError("Unsupported chat contract schema_version")

    subject = packet_manifest["subject"]
    question = packet_manifest["question"]
    source_sha256 = packet_manifest["source_sha256"]
    run_id = packet_manifest["run_id"]
    expected_result_file = "10_approved/" + result_filename(
        subject=subject,
        question=question,
        source_sha256=source_sha256,
        run_id=run_id,
    )
    if (contract.get("result") or {}).get("result_file") != expected_result_file:
        raise ValueError("Packet contract result_file does not match packet identity")
    if action == BridgeAction.APPLY_OBSIDIAN and notion_registration_authorized:
        raise ValueError("apply_obsidian must not authorize Notion registration")
    try:
        approved_time = datetime.fromisoformat(approved_at)
    except ValueError as exc:
        raise ValueError("approved_at must be ISO8601 with a timezone") from exc
    if approved_time.tzinfo is None:
        raise ValueError("approved_at must be ISO8601 with a timezone")

    filename = command_filename(
        subject=subject,
        question=question,
        run_id=run_id,
        result_sha256=result_sha256,
        action=action,
    )
    return BridgeCommand(
        command_id=filename.removesuffix(".command.json"),
        action=action,
        subject=subject,
        question=question,
        source_sha256=source_sha256,
        run_id=run_id,
        result_file=expected_result_file,
        result_sha256=result_sha256,
        approved_at=approved_at,
        approval_text=approval_text,
        notion_registration_authorized=notion_registration_authorized,
    )
