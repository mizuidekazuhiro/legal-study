from __future__ import annotations

import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import BaseModel, Field

from legal_study import __version__
from legal_study.io_utils import atomic_write_json
from legal_study.provenance import resolve_code_revision
from legal_study.settings import LocalSettings
from legal_study.source_store import SourceSnapshot
from legal_study.workspace import run_dir

RUN_MANIFEST_SCHEMA_VERSION = "2"


class RunManifestMismatchError(RuntimeError):
    """Raised when an output directory belongs to different run inputs."""


class RunManifest(BaseModel):
    schema_version: str = RUN_MANIFEST_SCHEMA_VERSION
    run_id: str
    input_hash: str
    created_at: datetime
    updated_at: datetime
    subject: str
    question: str
    requested_pages: list[int] | None = None
    source: SourceSnapshot
    output_dir: Path
    page_count: int | None = None
    pipeline_config: dict[str, Any] = Field(default_factory=dict)
    pipeline_config_sha256: str | None = None
    code_revision: str | None = None
    code_revision_source: str = "unavailable"
    code_dirty: bool | None = None
    app_version: str
    python_version: str
    platform: str
    pymupdf_version: str


class PreparedRun(BaseModel):
    output_dir: Path
    manifest_path: Path
    state_db: Path
    manifest: RunManifest


def _normalized_pages(pages: list[int] | None) -> list[int] | None:
    return sorted(set(pages)) if pages is not None else None


def calculate_pipeline_config_hash(pipeline_config: dict[str, Any]) -> str:
    encoded = json.dumps(
        pipeline_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calculate_input_hash(
    *,
    source_sha256: str,
    subject: str,
    question: str,
    pages: list[int] | None,
    pipeline_config: dict[str, Any],
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "subject": subject,
        "question": question,
        "requested_pages": _normalized_pages(pages),
        "pipeline_config": pipeline_config,
        "app_version": __version__,
        "pymupdf_version": pymupdf.__version__,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_run(
    *,
    snapshot: SourceSnapshot,
    subject: str,
    question: str,
    pages: list[int] | None,
    pipeline_config: dict[str, Any],
    output_dir: Path | None = None,
    settings: LocalSettings | None = None,
) -> PreparedRun:
    cfg = settings or LocalSettings()
    cfg.ensure()
    normalized_pages = _normalized_pages(pages)
    input_hash = calculate_input_hash(
        source_sha256=snapshot.sha256,
        subject=subject,
        question=question,
        pages=normalized_pages,
        pipeline_config=pipeline_config,
    )
    resolved_output = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else run_dir(
            subject=subject,
            question=question,
            source_sha256=snapshot.sha256,
            input_hash=input_hash,
            settings=cfg,
        )
    )
    manifest_path = resolved_output / "run_manifest.json"
    if manifest_path.exists():
        existing = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if existing.input_hash != input_hash:
            raise RunManifestMismatchError(
                f"Output directory belongs to a different input: {resolved_output}"
            )
        return PreparedRun(
            output_dir=resolved_output,
            manifest_path=manifest_path,
            state_db=cfg.state_db,
            manifest=existing,
        )

    if resolved_output.exists() and any(resolved_output.iterdir()):
        raise RunManifestMismatchError(
            f"Refusing to use a non-empty directory without a matching manifest: {resolved_output}"
        )
    resolved_output.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    run_id = hashlib.sha256(f"{input_hash}\0{resolved_output}".encode()).hexdigest()
    revision = resolve_code_revision()
    manifest = RunManifest(
        run_id=run_id,
        input_hash=input_hash,
        created_at=now,
        updated_at=now,
        subject=subject,
        question=question,
        requested_pages=normalized_pages,
        source=snapshot,
        output_dir=Path("."),
        pipeline_config=pipeline_config,
        pipeline_config_sha256=calculate_pipeline_config_hash(pipeline_config),
        code_revision=revision.sha,
        code_revision_source=revision.source,
        code_dirty=revision.dirty,
        app_version=__version__,
        python_version=platform.python_version(),
        platform=platform.platform(),
        pymupdf_version=pymupdf.__version__,
    )
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
    return PreparedRun(
        output_dir=resolved_output,
        manifest_path=manifest_path,
        state_db=cfg.state_db,
        manifest=manifest,
    )


def update_manifest_page_count(prepared: PreparedRun, page_count: int) -> PreparedRun:
    manifest = prepared.manifest.model_copy(
        update={"page_count": page_count, "updated_at": datetime.now(UTC)}
    )
    atomic_write_json(prepared.manifest_path, manifest.model_dump(mode="json"))
    return prepared.model_copy(update={"manifest": manifest})
