from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from legal_study.settings import LocalSettings
from legal_study.workspace import source_fingerprint


class SourceChangedError(RuntimeError):
    """Raised when the source changes while its immutable snapshot is being created."""


class SourceStoreCorruptionError(RuntimeError):
    """Raised when a content-addressed source does not match its path hash."""


class SourceSnapshot(BaseModel):
    original_path: Path
    original_filename: str
    source_size: int
    source_mtime_ns: int
    sha256: str
    snapshot_path: Path
    snapshot_created_at: datetime


def verify_snapshot(snapshot: SourceSnapshot) -> None:
    if not snapshot.snapshot_path.is_file():
        raise SourceStoreCorruptionError(f"Stored source is missing: {snapshot.snapshot_path}")
    if snapshot.snapshot_path.stat().st_size != snapshot.source_size:
        raise SourceStoreCorruptionError(
            f"Stored source size does not match its manifest: {snapshot.snapshot_path}"
        )
    if source_fingerprint(snapshot.snapshot_path) != snapshot.sha256:
        raise SourceStoreCorruptionError(
            f"Stored source content does not match its manifest: {snapshot.snapshot_path}"
        )


def snapshot_source(
    source: str | Path, *, settings: LocalSettings | None = None
) -> SourceSnapshot:
    """Copy a local source once into the content-addressed immutable source store.

    Callers must use ``snapshot_path`` for all parsing after this function returns.
    The original path is retained only as provenance metadata.
    """

    cfg = settings or LocalSettings()
    cfg.ensure()
    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise ValueError(f"Source is not a file: {source_path}")

    before = source_path.stat()
    staging_path = cfg.temp_dir / f"source-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as source_file, staging_path.open("xb") as staging_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(chunk)
                staging_file.write(chunk)
            staging_file.flush()
            os.fsync(staging_file.fileno())

        after = source_path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SourceChangedError(f"Source changed while snapshotting: {source_path}")

        sha256 = digest.hexdigest()
        snapshot_path = cfg.sources_dir / f"{sha256}.pdf"
        if snapshot_path.exists():
            if snapshot_path.stat().st_size != before.st_size:
                raise SourceStoreCorruptionError(
                    f"Stored source size does not match its hash path: {snapshot_path}"
                )
            if source_fingerprint(snapshot_path) != sha256:
                raise SourceStoreCorruptionError(
                    f"Stored source content does not match its hash path: {snapshot_path}"
                )
            staging_path.unlink()
        else:
            os.replace(staging_path, snapshot_path)

        created_at = datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=UTC)
        return SourceSnapshot(
            original_path=source_path,
            original_filename=source_path.name,
            source_size=before.st_size,
            source_mtime_ns=before.st_mtime_ns,
            sha256=sha256,
            snapshot_path=snapshot_path,
            snapshot_created_at=created_at,
        )
    finally:
        staging_path.unlink(missing_ok=True)
