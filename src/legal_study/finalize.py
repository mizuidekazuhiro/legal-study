from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from legal_study.io_utils import file_sha256
from legal_study.models import DocumentInspection
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import PreparedRun, RunManifest
from legal_study.settings import LocalSettings
from legal_study.state import RunStateStore


def finalize_existing_run(
    run_dir: Path,
    *,
    settings: LocalSettings | None = None,
) -> dict[str, Any]:
    """Create P1-C artifacts from an already completed P1-B run without rerunning OCR."""
    cfg = settings or LocalSettings()
    cfg.ensure()
    out = run_dir.expanduser().resolve()
    required = {
        "manifest": out / "run_manifest.json",
        "inspection": out / "inspection.json",
        "ocr": out / "ocr.json",
        "review": out / "review_manifest.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Run is missing required P1-B artifacts: {', '.join(sorted(missing))}"
        )

    manifest = RunManifest.model_validate_json(
        required["manifest"].read_text(encoding="utf-8")
    )
    inspection = DocumentInspection.model_validate_json(
        required["inspection"].read_text(encoding="utf-8")
    )
    if inspection.sha256 != manifest.source.sha256:
        raise ValueError("Run manifest and inspection source SHA do not match")

    state = RunStateStore(cfg.state_db)
    if not state.has_run(manifest.run_id):
        raise RuntimeError(
            "Persistent run state is missing; refusing to finalize artifacts "
            "without the original SQLite run identity"
        )
    prepared = PreparedRun(
        output_dir=out,
        manifest_path=required["manifest"],
        state_db=cfg.state_db,
        manifest=manifest,
    )
    pipeline = PdfIngestPipeline()
    inspection_hash = pipeline._inspection_bundle_hash(
        out, required["inspection"], inspection
    )
    if inspection_hash is None:
        raise RuntimeError("Inspection render bundle is incomplete or has invalid paths")
    ocr_hash = file_sha256(required["ocr"])
    review_hash = file_sha256(required["review"])

    reconciliation, reconciliation_hash = pipeline._reconciliation_step(
        state,
        prepared,
        inspection,
        out,
        upstream_hashes=[inspection_hash, ocr_hash, review_hash],
    )
    packet_hash = pipeline._problem_packet_step(
        state,
        prepared,
        inspection,
        reconciliation,
        out,
        upstream_hashes=[reconciliation_hash, review_hash],
    )
    state.set_run_status(manifest.run_id, "COMPLETED")

    validation = json.loads(
        (out / "problem_validation.json").read_text(encoding="utf-8")
    )
    markdown_files = sorted(out.glob("*_problem.md"))
    if len(markdown_files) != 1:
        raise RuntimeError(
            f"Expected exactly one problem Markdown, found {len(markdown_files)}"
        )
    return {
        "run_id": manifest.run_id,
        "output_dir": str(out),
        "reconciliation": reconciliation.counts,
        "problem_markdown": markdown_files[0].name,
        "canonical_source": "canonical_source.json",
        "problem_validation": validation,
        "packet_hash": packet_hash,
    }
