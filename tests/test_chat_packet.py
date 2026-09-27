import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from legal_study.chat_bridge_worker import BridgeAction, BridgeCommand
from legal_study.chat_contract import command_filename
from legal_study.chat_packet import build_chat_packet
from legal_study.chat_packet_validation import validate_chat_packet_structure
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
        "# Page Reading Pack\n\n## PDF page 109\n\n"
        "第12問\n12-1\n次の事例について甲の罪責を論ぜよ。\n"
        "答案例\n講師答案本文\n以上\n",
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
        assert contract["command"]["allowed_actions"] == ["apply_obsidian"]
        assert contract["command"]["filename_template"].format(
            result_sha256="c" * 64, action="apply_obsidian"
        ) == command_filename(
            subject="criminal",
            question="12",
            run_id="r" * 64,
            result_sha256="c" * 64,
            action=BridgeAction.APPLY_OBSIDIAN,
        )
        card_schema = contract["result"]["json_schema"]["$defs"]["ChatAnkiCard"]
        assert {
            "name",
            "scope",
            "learning_type",
            "front",
            "back",
            "extra",
            "anki_tags",
            "anki_deck",
        } <= set(card_schema["properties"])
        assert "api_key" not in json.dumps(contract).lower()
        assert "openai" not in json.dumps(contract).lower()
        instructions = archive.read("CHAT_INSTRUCTIONS.md").decode("utf-8")
        assert "Notion" in instructions and "explicit" in instructions
        assert "接続済みNotion" in instructions
        assert "Never send register_notion or apply_all to the PC" in instructions
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


def _upgrade_run_to_v2(run: Path) -> tuple[str, str]:
    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    text = "前文（重要事実）後文"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    canonical["schema_version"] = 4
    canonical["pages"] = [
        {
            "page_number": 109,
            "canonical_text": text,
            "canonical_text_sha256": digest,
            "canonical_text_source": "reconciled_text",
        }
    ]
    marker = canonical["logical_markers"][0]
    marker.update(
        {
            "canonical_start_char": 3,
            "canonical_end_char_exclusive": 7,
            "character_range_semantics": "page_unicode_codepoints_end_exclusive",
            "text_reference": {
                "page_number": 109,
                "field": "canonical_text",
                "text_sha256": digest,
                "source": "reconciled_text",
            },
            "position_status": "VERIFIED",
            "text_accuracy_status": "NEEDS_REVIEW",
        }
    )
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")
    return text, digest


def test_v2_packet_keeps_verified_page_text_marker_range_and_evidence(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _, digest = _upgrade_run_to_v2(run)

    result = build_chat_packet(run_dir=run)

    assert validate_chat_packet_structure(Path(result.packet_path))["valid"] is True
    with zipfile.ZipFile(result.packet_path) as archive:
        assert "page_text.json" in archive.namelist()
        manifest = json.loads(archive.read("packet_manifest.json"))
        assert manifest["schema_version"] == "chat_packet.v2"
        marker_payload = json.loads(archive.read("marker_index.json"))
        assert marker_payload["schema_version"] == "marker_index.v2"
        exported = marker_payload["logical_markers"][0]
        assert exported["evidence_image"] == "review/page-0109-review.png"
        page_text = json.loads(archive.read("page_text.json"))["pages"][0]
        assert page_text["text_sha256"] == digest
        assert (
            page_text["text"][
                exported["canonical_start_char"] : exported["canonical_end_char_exclusive"]
            ]
            == exported["exact_text"]
        )



@pytest.mark.parametrize("downgraded_schema", [None, "marker_index.v1"])
def test_v2_packet_rejects_marker_schema_downgrade(
    tmp_path: Path, downgraded_schema: str | None
) -> None:
    run = _run(tmp_path)
    _upgrade_run_to_v2(run)
    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / f"tampered-{downgraded_schema or 'missing'}.zip"

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "marker_index.json":
                marker_payload = json.loads(payload)
                if downgraded_schema is None:
                    marker_payload.pop("schema_version", None)
                else:
                    marker_payload["schema_version"] = downgraded_schema
                payload = json.dumps(marker_payload, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)



def test_v2_packet_preserves_intentionally_empty_canonical_text(tmp_path: Path) -> None:
    run = _run(tmp_path)
    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    empty_digest = hashlib.sha256(b"").hexdigest()
    canonical["schema_version"] = 4
    canonical["pages"] = [
        {
            "page_number": 109,
            "canonical_text": "",
            "canonical_text_sha256": empty_digest,
            "canonical_text_source": "unavailable_requires_visual_review",
            "reconciled_text": "UNTRUSTED EMBEDDED TEXT",
        }
    ]
    canonical["logical_markers"] = []
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")

    result = build_chat_packet(run_dir=run)

    assert validate_chat_packet_structure(Path(result.packet_path))["valid"] is True
    with zipfile.ZipFile(result.packet_path) as archive:
        page = json.loads(archive.read("page_text.json"))["pages"][0]
        assert page["text"] == ""
        assert page["text_sha256"] == empty_digest
        assert page["source"] == "unavailable_requires_visual_review"


@pytest.mark.parametrize("tamper_kind", ["missing_page", "changed_text", "changed_hash"])
def test_v2_packet_validates_every_requested_page_text_without_markers(
    tmp_path: Path, tamper_kind: str
) -> None:
    run = _run(tmp_path)
    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    text = "marker-free canonical text"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    canonical["schema_version"] = 4
    canonical["pages"] = [
        {
            "page_number": 109,
            "canonical_text": text,
            "canonical_text_sha256": digest,
            "canonical_text_source": "reconciled_text",
        }
    ]
    canonical["logical_markers"] = []
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")
    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / f"page-text-{tamper_kind}.zip"

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "page_text.json":
                page_text = json.loads(payload)
                if tamper_kind == "missing_page":
                    page_text["pages"] = []
                elif tamper_kind == "changed_text":
                    page_text["pages"][0]["text"] = "tampered"
                else:
                    page_text["pages"][0]["text_sha256"] = "0" * 64
                payload = json.dumps(page_text, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)



@pytest.mark.parametrize("replacement", [[], {}])
def test_v2_packet_rejects_marker_count_or_type_mismatch(
    tmp_path: Path, replacement: object
) -> None:
    run = _run(tmp_path)
    _upgrade_run_to_v2(run)
    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / ("marker-empty.zip" if replacement == [] else "marker-not-list.zip")

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "marker_index.json":
                marker_payload = json.loads(payload)
                marker_payload["logical_markers"] = replacement
                payload = json.dumps(marker_payload, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)



def test_all_pages_run_materializes_effective_requested_pages(tmp_path: Path) -> None:
    run = _run(tmp_path)
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["requested_pages"] = None
    manifest["page_count"] = 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    text = "全ページ本文"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    canonical["schema_version"] = 4
    canonical["source"]["requested_pages"] = None
    canonical["pages"] = [
        {
            "page_number": 1,
            "canonical_text": text,
            "canonical_text_sha256": digest,
            "canonical_text_source": "reconciled_text",
        }
    ]
    canonical["logical_markers"] = []
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")

    (run / "handoff_review/page-0109-review.png").unlink()
    (run / "handoff_review/page-0001-review.png").write_bytes(b"png")
    (run / "criminal_12_handoff.md").write_text(
        "# Page Reading Pack\n\n## PDF page 1\n\n"
        "第12問\n12-1\n次の事例について甲の罪責を論ぜよ。\n"
        "答案例\n講師答案本文\n以上\n",
        encoding="utf-8",
    )

    result = build_chat_packet(run_dir=run)

    assert result.requested_pages == [1]
    assert validate_chat_packet_structure(Path(result.packet_path))["valid"] is True
    with zipfile.ZipFile(result.packet_path) as archive:
        packet_manifest = json.loads(archive.read("packet_manifest.json"))
        assert packet_manifest["requested_pages"] == [1]
        assert "review/page-0001-review.png" in archive.namelist()



def test_all_pages_run_rejects_incomplete_canonical_page_set(tmp_path: Path) -> None:
    run = _run(tmp_path)
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["requested_pages"] = None
    manifest["page_count"] = 2
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    text = "1ページ目だけ"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    canonical["schema_version"] = 4
    canonical["source"]["requested_pages"] = None
    canonical["pages"] = [
        {
            "page_number": 1,
            "canonical_text": text,
            "canonical_text_sha256": digest,
            "canonical_text_source": "reconciled_text",
        }
    ]
    canonical["logical_markers"] = []
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="canonical pages do not match the effective requested page set"
    ):
        build_chat_packet(run_dir=run)


def test_all_pages_run_requires_manifest_page_count(tmp_path: Path) -> None:
    run = _run(tmp_path)
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["requested_pages"] = None
    manifest["page_count"] = None
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    canonical["schema_version"] = 4
    canonical["source"]["requested_pages"] = None
    canonical["pages"] = []
    canonical["logical_markers"] = []
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="all-pages Chat packet requires a positive manifest page_count"
    ):
        build_chat_packet(run_dir=run)



def test_v2_packet_rejects_auto_verified_marker_without_verified_range(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    canonical_path = run / "canonical_source.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    text = "境界未確定の本文"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    canonical["schema_version"] = 4
    canonical["pages"] = [
        {
            "page_number": 109,
            "canonical_text": text,
            "canonical_text_sha256": digest,
            "canonical_text_source": "reconciled_text",
        }
    ]
    marker = canonical["logical_markers"][0]
    marker.update(
        {
            "exact_text": None,
            "canonical_start_char": None,
            "canonical_end_char_exclusive": None,
            "character_range_semantics": "page_unicode_codepoints_end_exclusive",
            "text_reference": {
                "page_number": 109,
                "field": "canonical_text",
                "text_sha256": digest,
                "source": "reconciled_text",
            },
            "position_status": "NEEDS_REVIEW",
            "text_accuracy_status": "NEEDS_REVIEW",
            "review_status": "NEEDS_REVIEW",
        }
    )
    canonical_path.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")

    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / "auto-verified-without-range.zip"

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "marker_index.json":
                marker_payload = json.loads(payload)
                marker_payload["logical_markers"][0]["review_status"] = "AUTO_VERIFIED"
                payload = json.dumps(marker_payload, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)


def test_v2_packet_rejects_auto_verified_independent_ocr_marker(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    _upgrade_run_to_v2(run)
    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / "auto-verified-independent-ocr.zip"

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "page_text.json":
                page_text = json.loads(payload)
                page_text["pages"][0]["source"] = "independent_full_page_ocr"
                payload = json.dumps(page_text, ensure_ascii=False).encode("utf-8")
            elif name == "marker_index.json":
                marker_payload = json.loads(payload)
                marker = marker_payload["logical_markers"][0]
                marker["text_reference"]["source"] = "independent_full_page_ocr"
                marker["review_status"] = "AUTO_VERIFIED"
                marker["position_status"] = "VERIFIED"
                marker["text_accuracy_status"] = "VERIFIED"
                payload = json.dumps(marker_payload, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_status", "VERIFIED"),
        ("position_status", "UNKNOWN"),
        ("text_accuracy_status", None),
    ],
)
def test_v2_packet_rejects_unknown_marker_status(
    tmp_path: Path, field: str, value: str | None
) -> None:
    run = _run(tmp_path)
    _upgrade_run_to_v2(run)
    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / f"unknown-{field}.zip"

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "marker_index.json":
                marker_payload = json.loads(payload)
                marker_payload["logical_markers"][0][field] = value
                payload = json.dumps(marker_payload, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)



@pytest.mark.parametrize("tamper_level", ["payload", "marker"])
def test_v2_packet_rejects_changed_range_semantics(
    tmp_path: Path, tamper_level: str
) -> None:
    run = _run(tmp_path)
    _upgrade_run_to_v2(run)
    result = build_chat_packet(run_dir=run)
    packet = Path(result.packet_path)
    tampered = tmp_path / f"range-semantics-{tamper_level}.zip"

    with zipfile.ZipFile(packet) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "marker_index.json":
                marker_payload = json.loads(payload)
                if tamper_level == "payload":
                    marker_payload["range_semantics"] = "utf16_end_inclusive"
                else:
                    marker_payload["logical_markers"][0][
                        "character_range_semantics"
                    ] = "utf16_end_inclusive"
                payload = json.dumps(marker_payload, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)

    with pytest.raises(RuntimeError, match="structural validation failed"):
        validate_chat_packet_structure(tampered)
