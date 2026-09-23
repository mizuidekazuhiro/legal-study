from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from legal_study.chat_packet import build_chat_packet
from legal_study.run_manifest import RunManifest


class StrictPublishModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatPacketPublishResult(StrictPublishModel):
    published: bool
    status: str
    packet_path: str
    subject: str
    question: str
    source_sha256: str = Field(min_length=64, max_length=64)
    run_id: str = Field(min_length=64, max_length=64)


def publish_run_to_bridge(
    *,
    run_dir: Path,
    bridge_root: Path,
) -> ChatPacketPublishResult:
    """Publish one compact Chat packet to a Drive-synced bridge folder.

    The local Drive client performs the actual cloud synchronization. This function
    only writes into the existing 00_pending folder and never creates the bridge
    root or its child folders.
    """

    root = run_dir.expanduser().resolve()
    bridge = bridge_root.expanduser().resolve()
    pending = bridge / "00_pending"
    if not pending.is_dir():
        raise FileNotFoundError(f"Bridge pending folder does not exist: {pending}")

    manifest = RunManifest.model_validate_json(
        (root / "run_manifest.json").read_text(encoding="utf-8")
    )
    filename = (
        f"{manifest.subject}-q{manifest.question}-"
        f"{manifest.source.sha256[:12]}-{manifest.run_id[:12]}.chat_packet.zip"
    )
    destination = pending / filename

    if destination.exists():
        return ChatPacketPublishResult(
            published=False,
            status="ALREADY_PUBLISHED",
            packet_path=str(destination),
            subject=manifest.subject,
            question=manifest.question,
            source_sha256=manifest.source.sha256,
            run_id=manifest.run_id,
        )

    result = build_chat_packet(
        run_dir=root,
        output_path=destination,
        supplemental_path=None,
    )
    return ChatPacketPublishResult(
        published=True,
        status="PUBLISHED",
        packet_path=result.packet_path,
        subject=manifest.subject,
        question=manifest.question,
        source_sha256=manifest.source.sha256,
        run_id=manifest.run_id,
    )
