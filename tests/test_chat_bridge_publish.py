import json
import zipfile
from pathlib import Path

import pytest

import legal_study.chat_bridge_publish as bridge_publish
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


@pytest.mark.parametrize("missing", ["bridge", "pending"])
def test_publish_requires_existing_bridge_and_pending(
    tmp_path: Path, missing: str
) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    if missing == "pending":
        bridge.mkdir()

    with pytest.raises(FileNotFoundError, match="pending folder"):
        publish_run_to_bridge(run_dir=run, bridge_root=bridge)

    assert not (bridge / "00_pending").exists()


@pytest.mark.parametrize("incomplete", ["invalid_validation", "missing_review"])
def test_incomplete_run_never_publishes(tmp_path: Path, incomplete: str) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    pending = bridge / "00_pending"
    pending.mkdir(parents=True)
    if incomplete == "invalid_validation":
        (run / "problem_validation.json").write_text('{"valid": false}', encoding="utf-8")
    else:
        (run / "handoff_review/page-0200-review.png").unlink()

    with pytest.raises((RuntimeError, FileNotFoundError)):
        publish_run_to_bridge(run_dir=run, bridge_root=bridge)

    assert list(pending.iterdir()) == []


def test_existing_packet_with_different_content_is_not_accepted(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    pending = bridge / "00_pending"
    pending.mkdir(parents=True)
    first = publish_run_to_bridge(run_dir=run, bridge_root=bridge)
    (run / "criminal_16_handoff.md").write_text("changed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="different"):
        publish_run_to_bridge(run_dir=run, bridge_root=bridge)

    assert len(list(pending.glob("*.chat_packet.zip"))) == 1
    with zipfile.ZipFile(first.packet_path) as archive:
        assert archive.read("handoff.md").decode("utf-8") == "# Page Reading Pack\n"


def test_partial_packet_is_not_treated_as_completed(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    pending = bridge / "00_pending"
    pending.mkdir(parents=True)
    partial = pending / ".interrupted.chat_packet.zip.partial"
    partial.write_bytes(b"incomplete")

    result = publish_run_to_bridge(run_dir=run, bridge_root=bridge)

    assert result.status == "PUBLISHED"
    assert partial.read_bytes() == b"incomplete"
    assert len(list(pending.glob("*.chat_packet.zip"))) == 1


def test_existing_packet_with_wrong_manifest_is_not_accepted(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    (bridge / "00_pending").mkdir(parents=True)
    first = publish_run_to_bridge(run_dir=run, bridge_root=bridge)
    packet = Path(first.packet_path)
    with zipfile.ZipFile(packet, "w") as archive:
        archive.writestr("packet_manifest.json", '{"run_id": "wrong"}')

    with pytest.raises(RuntimeError, match="different"):
        publish_run_to_bridge(run_dir=run, bridge_root=bridge)


def test_packet_is_built_under_partial_name_before_final_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _write_run(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    pending = bridge / "00_pending"
    pending.mkdir(parents=True)
    real_build = bridge_publish.build_chat_packet
    observed: list[Path] = []

    def inspect_build(*, run_dir: Path, output_path: Path, supplemental_path: Path | None):
        observed.append(output_path)
        assert output_path.suffix == ".partial"
        assert list(pending.glob("*.chat_packet.zip")) == []
        return real_build(
            run_dir=run_dir,
            output_path=output_path,
            supplemental_path=supplemental_path,
        )

    monkeypatch.setattr(bridge_publish, "build_chat_packet", inspect_build)
    result = publish_run_to_bridge(run_dir=run, bridge_root=bridge)

    assert len(observed) == 1
    assert Path(result.packet_path).is_file()
    assert not observed[0].exists()
