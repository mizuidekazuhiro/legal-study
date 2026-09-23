import json
import zipfile
from pathlib import Path

import pytest

from legal_study.chat_bridge_worker import BridgeAction, BridgeCommand
from legal_study.chat_contract import command_filename
from legal_study.chat_packet import build_chat_packet
from legal_study.chat_result import ChatStudyResult


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "handoff_review").mkdir(parents=True)
    manifest = {
        "schema_version": "1",
        "run_id": "r" * 64,
        "input_hash": "i" * 64,
        "created_at": "2026-09-23T00:00:00Z",
        "updated_at": "2026-09-23T00:00:00Z",
        "subject": "criminal",
        "question": "12",
        "requested_pages": [109],
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
    canonical = {
        "schema_version": 3,
        "subject": "criminal",
        "question": "12",
        "source": {
            "filename": "論文マスター_刑法.pdf",
            "sha256": "a" * 64,
            "requested_pages": [109],
        },
        "logical_markers": [
            {
                "id": "p0109-logical-mark-001",
                "page_number": 109,
                "color": "yellow",
                "exact_text": "重要事実",
                "start_char": 10,
                "end_char": 14,
                "bbox": [1, 2, 3, 4],
                "boundary_confidence": 0.9,
                "review_status": "NEEDS_REVIEW",
                "reason": "verify",
                "evidence_image": "renders/page-0109.png",
                "raw_vector_refs": ["must-not-be-copied"],
            }
        ],
    }
    (run / "canonical_source.json").write_text(
        json.dumps(canonical, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "problem_validation.json").write_text(
        json.dumps({"valid": True}),
        encoding="utf-8",
    )
    (run / "criminal_12_handoff.md").write_text(
        "# Page Reading Pack\n\n講師答案本文\n",
        encoding="utf-8",
    )
    (run / "handoff_review/page-0109-review.png").write_bytes(b"png")

    supplemental = {
        "schema_version": "supplemental_retrieval.v1",
        "subject": "criminal",
        "question": "12",
        "search_attempts": [],
        "argument_patterns": [
            {
                "source_kind": "obsidian_argument_pattern",
                "pattern_id": "刑3",
                "aliases": ["間接正犯"],
                "title": "刑3 間接正犯",
                "body": "登録済みObsidian論証本文",
                "related_statutes": [],
                "source": "論文ナビゲートテキスト 刑法",
                "source_page": 10,
                "pdf_pages": "PDF p.23",
                "source_path": "10_論証パターン/03_刑法/刑003.md",
                "source_sha256": "b" * 64,
            }
        ],
        "statutes": [
            {
                "source_kind": "notion_statute",
                "record_id": "record-199",
                "title": "刑法 第百九十九条",
                "law_name": "刑法",
                "article": "199",
                "text": "人を殺した者は...",
                "notion_url": "https://app.notion.com/p/test",
                "official_url": "https://laws.e-gov.go.jp/test",
                "last_edited_time": None,
            }
        ],
        "existing_problem_notes": [],
    }
    (run / "supplemental_retrieval.json").write_text(
        json.dumps(supplemental, ensure_ascii=False),
        encoding="utf-8",
    )
    return run


def test_build_chat_packet_is_compact_and_project_instruction_free(tmp_path: Path) -> None:
    run = _run(tmp_path)

    result = build_chat_packet(run_dir=run)

    packet = Path(result.packet_path)
    assert packet.is_file()
    assert result.logical_marker_count == 1
    assert result.review_sheet_count == 1
    assert result.supplemental_included is True

    with zipfile.ZipFile(packet) as archive:
        names = set(archive.namelist())
        assert names == {
            "packet_manifest.json",
            "handoff.md",
            "marker_index.json",
            "supplemental.json",
            "CHAT_INSTRUCTIONS.md",
            "review/page-0109-review.png",
        }
        marker_payload = json.loads(archive.read("marker_index.json"))
        marker = marker_payload["logical_markers"][0]
        assert marker["exact_text"] == "重要事実"
        assert "raw_vector_refs" not in marker

        supplemental = json.loads(archive.read("supplemental.json"))
        pattern = supplemental["argument_patterns"][0]
        assert pattern["authority"] == "obsidian_registered_pattern"
        assert "source" not in pattern
        assert "source_page" not in pattern
        assert "pdf_pages" not in pattern
        assert "論文ナビゲートテキスト" not in json.dumps(
            supplemental,
            ensure_ascii=False,
        )

        manifest = json.loads(archive.read("packet_manifest.json"))
        assert manifest["project_instructions_included"] is False
        assert manifest["audit_artifacts_included"] is False
        assert manifest["subject"] == "criminal"
        assert manifest["question"] == "12"
        assert manifest["source_sha256"] == "a" * 64
        assert manifest["run_id"] == "r" * 64
        assert manifest["requested_pages"] == [109]
        contract = manifest["chat_contract"]
        assert contract["schema_version"] == "chat_bridge_contract.v1"
        assert contract["result"]["result_file"].startswith("10_approved/")
        assert contract["result"]["json_schema"] == ChatStudyResult.model_json_schema()
        assert contract["command"]["json_schema"] == BridgeCommand.model_json_schema()
        assert contract["command"]["folder"] == "20_commands"
        assert contract["command"]["allowed_actions"] == [
            "apply_obsidian", "register_notion", "apply_all"
        ]
        assert contract["command"]["filename_template"].format(
            result_sha256="c" * 64, action="apply_obsidian"
        ) == command_filename(
            subject="criminal", question="12", run_id="r" * 64,
            result_sha256="c" * 64, action=BridgeAction.APPLY_OBSIDIAN,
        )
        card_schema = contract["result"]["json_schema"]["$defs"]["ChatAnkiCard"]
        assert {"name", "scope", "learning_type", "front", "back", "extra",
                "anki_tags", "anki_deck"} <= set(card_schema["properties"])
        assert "api_key" not in json.dumps(contract).lower()
        assert "openai" not in json.dumps(contract).lower()
        instructions = archive.read("CHAT_INSTRUCTIONS.md").decode("utf-8")
        assert "Notion" in instructions and "explicit" in instructions
        assert instructions.index("10_approved") < instructions.index("20_commands")
        assert "result_sha256" in instructions
        assert "exactly one" in instructions
        assert "unresolved" in instructions
        assert "reviewed_pages" in instructions
        assert "command_id" in instructions
        assert "never overwrite" in instructions


def test_invalid_problem_packet_is_not_exported(tmp_path: Path) -> None:
    run = _run(tmp_path)
    (run / "problem_validation.json").write_text(
        json.dumps({"valid": False}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="not valid"):
        build_chat_packet(run_dir=run)


def test_packet_without_supplemental_keeps_primary_evidence(tmp_path: Path) -> None:
    run = _run(tmp_path)
    (run / "supplemental_retrieval.json").unlink()

    result = build_chat_packet(run_dir=run)

    assert result.supplemental_included is False
    with zipfile.ZipFile(result.packet_path) as archive:
        assert set(archive.namelist()) == {
            "packet_manifest.json",
            "handoff.md",
            "marker_index.json",
            "CHAT_INSTRUCTIONS.md",
            "review/page-0109-review.png",
        }
