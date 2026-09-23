from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from legal_study.chat_bridge_worker import BridgeAction, BridgeCommand
from legal_study.chat_contract import command_filename_template, result_filename
from legal_study.chat_result import ChatStudyResult
from legal_study.io_utils import file_sha256
from legal_study.problem_packet import handoff_markdown_filename
from legal_study.run_manifest import RunManifest
from legal_study.supplemental_retrieval import SupplementalRetrievalBundle


class StrictChatPacketModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatPacketBuildResult(StrictChatPacketModel):
    packet_path: str
    packet_sha256: str = Field(min_length=64, max_length=64)
    subject: str
    question: str
    source_sha256: str = Field(min_length=64, max_length=64)
    requested_pages: list[int]
    logical_marker_count: int
    review_sheet_count: int
    supplemental_included: bool


def build_chat_packet(
    *,
    run_dir: Path,
    output_path: Path | None = None,
    supplemental_path: Path | None = None,
) -> ChatPacketBuildResult:
    """Build a compact packet for the ChatGPT Project bridge.

    This intentionally keeps the large local audit trail local. Project instruction
    files are also omitted because the ChatGPT Project already owns the governing
    instructions and sources.
    """

    root = run_dir.expanduser().resolve()
    manifest_path = root / "run_manifest.json"
    canonical_path = root / "canonical_source.json"
    validation_path = root / "problem_validation.json"

    for required in (manifest_path, canonical_path, validation_path):
        if not required.is_file():
            raise FileNotFoundError(f"Required run artifact is missing: {required}")

    manifest = RunManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("valid") is not True:
        raise RuntimeError(
            "problem_validation.json is not valid; refusing to build Chat packet"
        )

    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    source = canonical.get("source") or {}
    if source.get("sha256") != manifest.source.sha256:
        raise RuntimeError("canonical source SHA does not match run manifest")
    if source.get("requested_pages") != manifest.requested_pages:
        raise RuntimeError("canonical requested_pages do not match run manifest")

    handoff_name = handoff_markdown_filename(manifest.subject, manifest.question)
    handoff_path = root / handoff_name
    if not handoff_path.is_file():
        raise FileNotFoundError(f"Handoff Markdown is missing: {handoff_path}")

    marker_index = [
        _compact_marker(item)
        for item in (canonical.get("logical_markers") or [])
    ]

    review_files: list[tuple[int, Path]] = []
    for page_number in manifest.requested_pages or []:
        path = root / "handoff_review" / f"page-{page_number:04d}-review.png"
        if not path.is_file():
            raise FileNotFoundError(f"Review sheet is missing: {path}")
        review_files.append((page_number, path))

    supplemental_file = (
        supplemental_path.expanduser().resolve()
        if supplemental_path is not None
        else root / "supplemental_retrieval.json"
    )
    supplemental_payload: dict[str, Any] | None = None
    if supplemental_file.is_file():
        supplemental = SupplementalRetrievalBundle.model_validate_json(
            supplemental_file.read_text(encoding="utf-8")
        )
        if (
            supplemental.subject != manifest.subject
            or supplemental.question != manifest.question
        ):
            raise RuntimeError("Supplemental retrieval identity does not match run")
        supplemental_payload = _compact_supplemental(supplemental)

    result_name = result_filename(
        subject=manifest.subject,
        question=manifest.question,
        source_sha256=manifest.source.sha256,
        run_id=manifest.run_id,
    )
    packet_manifest = {
        "schema_version": "chat_packet.v1",
        "subject": manifest.subject,
        "question": manifest.question,
        "source_sha256": manifest.source.sha256,
        "run_id": manifest.run_id,
        "requested_pages": manifest.requested_pages,
        "handoff_file": "handoff.md",
        "logical_marker_count": len(marker_index),
        "review_sheet_count": len(review_files),
        "supplemental_included": supplemental_payload is not None,
        "project_instructions_included": False,
        "audit_artifacts_included": False,
        "chat_contract": {
            "schema_version": "chat_bridge_contract.v1",
            "selection": {
                "match_fields": ["subject", "question"],
                "require_unique": True,
                "ambiguity_fields": ["source_sha256", "run_id"],
            },
            "result": {
                "schema_version": "chat_study_result.v1",
                "folder": "10_approved",
                "filename": result_name,
                "result_file": f"10_approved/{result_name}",
                "json_schema": ChatStudyResult.model_json_schema(),
            },
            "command": {
                "schema_version": "chat_bridge_command.v1",
                "folder": "20_commands",
                "filename_template": command_filename_template(
                    subject=manifest.subject,
                    question=manifest.question,
                    run_id=manifest.run_id,
                ),
                "allowed_actions": [BridgeAction.APPLY_OBSIDIAN.value],
                "json_schema": BridgeCommand.model_json_schema(),
            },
        },
    }

    target = (
        output_path.expanduser().resolve()
        if output_path is not None
        else root / "chat_packet.zip"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(
            f"Chat packet already exists; refusing overwrite: {target}"
        )

    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_json(archive, "packet_manifest.json", packet_manifest)
        archive.writestr("handoff.md", handoff_path.read_text(encoding="utf-8"))
        _write_json(
            archive,
            "marker_index.json",
            {"logical_markers": marker_index},
        )
        if supplemental_payload is not None:
            _write_json(archive, "supplemental.json", supplemental_payload)
        archive.writestr("CHAT_INSTRUCTIONS.md", _chat_instructions())
        for page_number, path in review_files:
            archive.write(
                path,
                arcname=f"review/page-{page_number:04d}-review.png",
            )

    return ChatPacketBuildResult(
        packet_path=str(target),
        packet_sha256=file_sha256(target),
        subject=manifest.subject,
        question=manifest.question,
        source_sha256=manifest.source.sha256,
        requested_pages=list(manifest.requested_pages or []),
        logical_marker_count=len(marker_index),
        review_sheet_count=len(review_files),
        supplemental_included=supplemental_payload is not None,
    )


def _compact_marker(item: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "page_number",
        "color",
        "exact_text",
        "start_char",
        "end_char",
        "bbox",
        "boundary_confidence",
        "review_status",
        "reason",
        "evidence_image",
    )
    return {key: item.get(key) for key in allowed if key in item}


def _compact_supplemental(
    bundle: SupplementalRetrievalBundle,
) -> dict[str, Any]:
    """Keep only content ChatGPT needs for legal drafting.

    In particular, do not carry the historical 論文ナビゲート source metadata
    from an Obsidian pattern into the bridge packet. The registered Obsidian
    pattern itself is the supplied common-rule source for this workflow.
    """

    return {
        "schema_version": "chat_supplemental.v1",
        "subject": bundle.subject,
        "question": bundle.question,
        "argument_patterns": [
            {
                "authority": "obsidian_registered_pattern",
                "pattern_id": item.pattern_id,
                "aliases": item.aliases,
                "title": item.title,
                "body": item.body,
                "related_statutes": item.related_statutes,
                "source_path": item.source_path,
                "source_sha256": item.source_sha256,
            }
            for item in bundle.argument_patterns
        ],
        "statutes": [
            {
                "authority": "notion_statute",
                "record_id": item.record_id,
                "title": item.title,
                "law_name": item.law_name,
                "article": item.article,
                "text": item.text,
                "notion_url": item.notion_url,
                "official_url": item.official_url,
                "last_edited_time": item.last_edited_time,
            }
            for item in bundle.statutes
        ],
        "existing_problem_notes": [
            {
                "authority": "obsidian_problem_note",
                "relative_path": item.relative_path,
                "text": item.text,
                "source_sha256": item.source_sha256,
            }
            for item in bundle.existing_problem_notes
        ],
    }


def _chat_instructions() -> str:
    return """# Chat Bridge Instructions

Use this in a normal ChatGPT Project chat, not Work mode or an API workflow.
Project Sources govern; their full instructions are not copied here.

1. Match pending packets by subject and question. Use exactly one matching
   packet; if zero, stop. If multiple, inspect source_sha256, run_id and each
   manifest; stop unless the intended packet is unambiguous. Never pick newest.
2. handoff.md is the compact reading surface. review/*.png is visual authority
   for handwriting, corrections and marker boundaries. Visually verify
   marker_index.json candidates; do not infer legal meaning from color alone.
3. If present, supplemental.json contains registered Obsidian arguments and
   Notion statutes. Do not fetch a 論文ナビゲートテキスト PDF as another source.
4. Follow packet_manifest.json chat_contract and the embedded result JSON schema.
   Copy subject, question, source_sha256 and requested_pages exactly into
   study_result.source. Set reviewed_pages only for pages actually checked in
   review/*.png; all requested pages must be reviewed. Record unresolved issues.
   Use the listed Anki fields, the criminal deck 刑法 論文試験, and shared
   problem_card_extra rather than duplicating the full answer per problem card.
5. Show every Anki card and the Obsidian draft in full before asking approval.
   If unresolved is nonempty, source differs, markers remain undecided or visual
   review is incomplete, stop; approval alone cannot override local validation.
6. For explicit approval ("承認", "OK", "これでいい"), create only an
   apply_obsidian command with notion_registration_authorized=false. For a
   separate explicit "登録して" or "Notionに入れて" request, register directly from
   this normal ChatGPT chat through 接続済みNotion, only after content approval.
   Never send register_notion or apply_all to the PC in normal operation.
   Ambiguity never opts into Notion. Keep the user's actual words in
   approval_text and use an ISO8601 approved_at; these fields are not proof
   of human approval.
7. After approval, finish the study_result JSON and save it completely to the
   exact 10_approved result_file in chat_contract. Re-read if possible and hash
   the bytes actually saved (SHA-256). If save, review or validation fails,
   write no command. Then build the command from the existing schema with no
   extra fields, set result_sha256 to that saved-file hash, and save it last to
   20_commands using the deterministic filename_template. Set command_id to
   that filename without .command.json. Never reverse order. For an existing
   result or command name, reuse only identical bytes; never overwrite a
   different file.
"""


def _write_json(
    archive: zipfile.ZipFile,
    name: str,
    payload: Any,
) -> None:
    archive.writestr(
        name,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
