from __future__ import annotations

import os
import uuid
import zipfile
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

    temporary = pending / f".{filename}.{uuid.uuid4().hex}.partial"
    try:
        build_chat_packet(
            run_dir=root,
            output_path=temporary,
            supplemental_path=None,
        )
        with temporary.open("rb+") as file_handle:
            os.fsync(file_handle.fileno())

        if destination.exists():
            if not _same_packet_contents(temporary, destination):
                raise RuntimeError(
                    f"Existing Chat packet has different content: {destination}"
                )
            status = "ALREADY_PUBLISHED"
            published = False
        else:
            os.rename(temporary, destination)
            status = "PUBLISHED"
            published = True

        return ChatPacketPublishResult(
            published=published,
            status=status,
            packet_path=str(destination),
            subject=manifest.subject,
            question=manifest.question,
            source_sha256=manifest.source.sha256,
            run_id=manifest.run_id,
        )
    finally:
        temporary.unlink(missing_ok=True)


def _same_packet_contents(expected: Path, existing: Path) -> bool:
    try:
        with zipfile.ZipFile(expected) as left, zipfile.ZipFile(existing) as right:
            names = left.namelist()
            return names == right.namelist() and all(
                left.read(name) == right.read(name) for name in names
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"Existing Chat packet is unreadable: {existing}") from exc
