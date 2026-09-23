import json
import zipfile
from pathlib import Path

from legal_study.chat_bridge_publish import publish_run_to_bridge


def _write_run(root: Path) -> Path:
    run = root / "run"
    (run / "handoff_review").mkdir(parents=True)
    manifest = {
        "schema_version": "1",
        "run_id": "r" * 64,
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
        "question": "16",
        "source": {
            "filename": "論文マスター_刑法.pdf",
            "sha256": "a" * 64,
            "requested_pages": [200],
        },
        "logical_markers": [],
    }
    (run / "canonical_source.json").write_text(
        json.dumps(canonical, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "problem_validation.json").write_text(
        json.dumps({"valid": True}),
        encoding="utf-8",
    )
    (run / "criminal_16_handoff.md").write_text(
        "# Page Reading Pack\n",
        encoding="utf-8",
    )
    (run / "handoff_review/page-0200-review.png").write_bytes(b"png")
    return run


def test_publish_run_to_existing_drive_pending_folder(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    (bridge / "00_pending").mkdir(parents=True)

    result = publish_run_to_bridge(run_dir=run, bridge_root=bridge)

    assert result.published is True
    assert result.status == "PUBLISHED"
    packet = Path(result.packet_path)
    assert packet.parent == bridge / "00_pending"
    assert packet.name.startswith("criminal-q16-")
    with zipfile.ZipFile(packet) as archive:
        manifest = json.loads(archive.read("packet_manifest.json"))
        assert manifest["question"] == "16"

    second = publish_run_to_bridge(run_dir=run, bridge_root=bridge)
    assert second.published is False
    assert second.status == "ALREADY_PUBLISHED"
    assert second.packet_path == result.packet_path
