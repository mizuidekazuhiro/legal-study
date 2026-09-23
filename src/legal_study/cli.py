from __future__ import annotations

import importlib.util
import json
import platform
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from legal_study.automation.file_watcher import iter_file_updates
from legal_study.automation.orchestrator import watch_pdf_updates
from legal_study.automation.sync_stability import mark_question_sync_stable
from legal_study.completion.done_marker import detect_done_markers, save_done_stamp
from legal_study.completion.question_resolution import apply_done_markers
from legal_study.finalize import finalize_existing_run
from legal_study.io_utils import atomic_write_text
from legal_study.page_identity import (
    align_page_indexes,
    ensure_source_page_index,
    load_source_page_index,
)
from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.cache import seed_shared_ocr_cache_from_run
from legal_study.pdf.ocr.paddle import (
    PaddleOcrEngine,
    inspect_paddle_installation,
    warmup_paddle_models,
)
from legal_study.pdf.ocr.routing import OcrRoutingConfig
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.problem_packet import handoff_markdown_filename
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source

app = typer.Typer(no_args_is_help=True)
console = Console()


def _parse_pages(value: str | None) -> list[int] | None:
    if not value:
        return None
    pages: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            start, end = [int(x) for x in part.split("-", 1)]
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


@app.command("make-done-stamp")
def make_done_stamp(
    output: Annotated[Path, typer.Argument(help="PNG path to create.")],
) -> None:
    """Create the canonical Goodnotes DONE stamp PNG."""
    digest = save_done_stamp(output)
    console.print(
        json.dumps(
            {
                "output": str(output.expanduser().resolve()),
                "sha256": digest,
                "stamp_version": "LEGAL-STUDY-DONE-V1",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("detect-done")
def detect_done(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    pages: Annotated[
        str | None,
        typer.Option(help="1-based pages, e.g. 110-116,120"),
    ] = None,
) -> None:
    """Detect the canonical DONE stamp on selected PDF pages."""
    results = detect_done_markers(pdf, pages=_parse_pages(pages))
    console.print(
        json.dumps(
            {
                "pdf": str(pdf),
                "detected_pages": [
                    item.page_number for item in results if item.detected
                ],
                "results": [item.model_dump(mode="json") for item in results],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("apply-done")
def apply_done(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    subject: Annotated[str, typer.Option(help="Subject key, e.g. criminal")] = "criminal",
    pages: Annotated[
        str | None,
        typer.Option(help="1-based pages to inspect for DONE, e.g. 165 or 160-166"),
    ] = None,
    max_backtrack: Annotated[
        int,
        typer.Option(help="Maximum pages to scan backward for the nearest 第N問 header."),
    ] = 16,
    ocr: Annotated[str, typer.Option(help="Question-header OCR backend; currently paddle")] = "paddle",
) -> None:
    """Resolve detected DONE stamps to questions and persist DONE_DETECTED state."""
    settings = LocalSettings()
    settings.ensure()
    if ocr != "paddle":
        raise typer.BadParameter("ocr must be 'paddle' for apply-done")
    engine = PaddleOcrEngine(model_root=settings.models_dir / "paddleocr")
    results = apply_done_markers(
        pdf,
        subject=subject,
        ocr_engine=engine,
        settings=settings,
        pages=_parse_pages(pages),
        max_backtrack=max_backtrack,
    )
    console.print(
        json.dumps(
            {
                "source": str(pdf),
                "subject": subject,
                "detected_count": len(results),
                "results": [item.model_dump(mode="json") for item in results],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("watch-study")
def watch_study(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    subject: Annotated[str, typer.Option(help="Subject key, e.g. criminal")] = "criminal",
    poll_interval_seconds: Annotated[
        float,
        typer.Option(help="Idle poll interval in seconds. Default is one minute."),
    ] = 60.0,
    stability_interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds between active stability checks after an update."),
    ] = 5.0,
    stability_equal_observations: Annotated[
        int,
        typer.Option(help="Equal metadata observations required before hash verification."),
    ] = 3,
    stability_timeout_seconds: Annotated[
        float,
        typer.Option(help="Maximum seconds to wait for a stable updated PDF."),
    ] = 90.0,
    max_backtrack: Annotated[
        int,
        typer.Option(help="Maximum pages to scan backward for the nearest 第N問 header."),
    ] = 16,
) -> None:
    """Watch a study PDF and advance newly DONE questions to SYNC_STABLE."""
    settings = LocalSettings()
    settings.ensure()
    engine = PaddleOcrEngine(model_root=settings.models_dir / "paddleocr")
    console.print(
        json.dumps(
            {
                "watching": str(pdf.expanduser().resolve()),
                "subject": subject,
                "poll_interval_seconds": poll_interval_seconds,
                "stability_interval_seconds": stability_interval_seconds,
            },
            ensure_ascii=False,
        )
    )
    try:
        for result in watch_pdf_updates(
            pdf,
            subject=subject,
            ocr_engine=engine,
            settings=settings,
            poll_interval_seconds=poll_interval_seconds,
            stability_interval_seconds=stability_interval_seconds,
            stability_equal_observations=stability_equal_observations,
            stability_timeout_seconds=stability_timeout_seconds,
            max_backtrack=max_backtrack,
        ):
            console.print(result.model_dump_json())
    except KeyboardInterrupt:
        console.print("Stopped.")


@app.command("watch-file")
def watch_file(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    poll_interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds between cheap file metadata polls."),
    ] = 60.0,
) -> None:
    """Watch a Drive-synced PDF and emit one JSON object for each detected update."""
    console.print(
        json.dumps(
            {
                "watching": str(pdf.expanduser().resolve()),
                "poll_interval_seconds": poll_interval_seconds,
            },
            ensure_ascii=False,
        )
    )
    try:
        for event in iter_file_updates(
            pdf,
            poll_interval_seconds=poll_interval_seconds,
        ):
            console.print(event.model_dump_json())
    except KeyboardInterrupt:
        console.print("Stopped.")


@app.command("check-sync-stable")
def check_sync_stable(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    subject: Annotated[str, typer.Option(help="Subject key, e.g. criminal")] = "criminal",
    question: Annotated[str, typer.Option(help="Question number already in DONE_DETECTED state")] = "",
    interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds between stability probes."),
    ] = 5.0,
    required_equal_observations: Annotated[
        int,
        typer.Option(help="Equal size/mtime observations required before hash verification."),
    ] = 3,
    timeout_seconds: Annotated[
        float,
        typer.Option(help="Maximum seconds to wait before leaving the state unchanged."),
    ] = 90.0,
) -> None:
    """Advance DONE_DETECTED to SYNC_STABLE only after source stability verification."""
    if not question.strip():
        raise typer.BadParameter("--question is required")
    settings = LocalSettings()
    settings.ensure()
    result = mark_question_sync_stable(
        pdf,
        subject=subject,
        question=question.strip(),
        settings=settings,
        interval_seconds=interval_seconds,
        required_equal_observations=required_equal_observations,
        timeout_seconds=timeout_seconds,
    )
    console.print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command()
def init() -> None:
    """Create the portable local workspace."""
    settings = LocalSettings()
    settings.ensure()
    console.print(f"LEGAL_STUDY_HOME: {settings.home}")
    console.print(f"runs:   {settings.runs_dir}")
    console.print(f"cache:  {settings.cache_dir}")
    console.print(f"models: {settings.models_dir}")
    console.print("Initialized.")


@app.command()
def doctor(
    ocr: Annotated[
        bool,
        typer.Option("--ocr", help="Verify offline Paddle packages, models, and hashes."),
    ] = False,
) -> None:
    """Check whether the core local-only PDF pipeline can run."""
    settings = LocalSettings()
    settings.ensure()
    checks = {
        "Python": platform.python_version(),
        "Platform": platform.platform(),
        "Workspace": str(settings.home),
        "PyMuPDF": "OK" if importlib.util.find_spec("pymupdf") else "MISSING",
        "Pillow": "OK" if importlib.util.find_spec("PIL") else "MISSING",
        "PaddleOCR": (
            "installed" if importlib.util.find_spec("paddleocr") else "optional/not installed"
        ),
    }
    table = Table("Check", "Value")
    for key, value in checks.items():
        table.add_row(key, value)
    console.print(table)
    console.print(
        "Core inspect/ingest works locally without cloud services. "
        "PaddleOCR is optional and may require a separate Paddle runtime."
    )
    if ocr:
        status = inspect_paddle_installation(settings.models_dir / "paddleocr")
        console.print_json(data=status)
        if status["ready"]:
            console.print("Offline PaddleOCR: READY (CPU)")
        else:
            console.print(
                "Offline PaddleOCR: NOT READY. Install the OCR extra and run "
                "`legal-study warmup-ocr` once while online."
            )
            raise typer.Exit(code=1)


@app.command("warmup-ocr")
def warmup_ocr() -> None:
    """Explicitly download and hash PaddleOCR models for later offline CPU use."""
    settings = LocalSettings()
    settings.ensure()
    console.print("Preparing PaddleOCR CPU models; this command may use the network...")
    status = warmup_paddle_models(settings.models_dir / "paddleocr")
    console.print_json(data=status)
    if not status["ready"]:
        raise typer.Exit(code=1)


@app.command()
def inspect(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    pages: Annotated[str | None, typer.Option(help="1-based pages, e.g. 110-116,120")] = None,
    render_dir: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    settings = LocalSettings()
    snapshot = snapshot_source(pdf, settings=settings)
    result = PdfInspector().inspect(
        snapshot.snapshot_path, pages=_parse_pages(pages), render_dir=render_dir
    )
    if json_output:
        atomic_write_text(json_output, result.model_dump_json(indent=2) + "\n")

    table = Table(
        "Page", "Mode", "Chars", "TextQ", "Largest image", "Marks", "OCR", "Vision"
    )
    for page in result.pages:
        table.add_row(
            str(page.page_number),
            page.mode.value,
            str(page.native_char_count),
            f"{page.native_quality_score:.2f}",
            f"{page.largest_image_coverage:.0%}",
            str(len(page.vector_marks)),
            "YES" if page.ocr_recommended else "no",
            "YES" if page.vision_review_recommended else "no",
        )
    console.print(table)
    console.print(f"SHA256: {result.sha256}")


@app.command()
def finalize(
    run_dir: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True),
    ],
) -> None:
    """Create reconciliation/canonical/problem Markdown from an existing P1-B run."""
    result = finalize_existing_run(run_dir)
    console.print(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("diff-source")
def diff_source(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    against_run: Annotated[
        Path,
        typer.Option(
            "--against-run",
            exists=True,
            file_okay=False,
            readable=True,
            help="Completed historical run whose source/page range is the baseline.",
        ),
    ],
) -> None:
    """Compare a new PDF version with the exact source used by a historical run."""
    settings = LocalSettings()
    settings.ensure()
    manifest_path = against_run.expanduser().resolve() / "run_manifest.json"
    if not manifest_path.is_file():
        raise typer.BadParameter(f"run_manifest.json not found: {against_run}")
    from legal_study.run_manifest import RunManifest

    manifest = RunManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    snapshot = snapshot_source(pdf, settings=settings)
    current = ensure_source_page_index(snapshot, settings.cache_dir)
    previous = load_source_page_index(
        settings.cache_dir,
        manifest.source.sha256,
    )
    alignment = align_page_indexes(previous, current)

    requested = set(manifest.requested_pages or [])
    relevant = [
        item
        for item in alignment.records
        if item.previous_page in requested
    ]
    current_pages = sorted(
        int(item.current_page)
        for item in relevant
        if item.current_page is not None
    )
    reusable = [
        int(item.current_page)
        for item in relevant
        if item.current_page is not None and item.safe_for_base_ocr_reuse
    ]
    console.print(
        json.dumps(
            {
                "baseline_run_id": manifest.run_id,
                "baseline_source_sha256": manifest.source.sha256,
                "current_source_sha256": snapshot.sha256,
                "baseline_page_count": previous.page_count,
                "current_page_count": current.page_count,
                "requested_baseline_pages": manifest.requested_pages,
                "suggested_current_pages": current_pages,
                "safe_for_base_ocr_reuse_pages": sorted(reusable),
                "alignment_counts": alignment.counts,
                "requested_page_alignment": [
                    item.model_dump(mode="json") for item in relevant
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("seed-ocr-cache")
def seed_ocr_cache(
    run_dir: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True),
    ],
) -> None:
    """Seed cross-version OCR cache from a completed historical run."""
    settings = LocalSettings()
    settings.ensure()
    result = seed_shared_ocr_cache_from_run(run_dir, settings.cache_dir)
    console.print(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def ingest(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Optional; defaults to the portable local workspace."
        ),
    ] = None,
    pages: Annotated[str | None, typer.Option(help="1-based pages, e.g. 110-116")] = None,
    ocr: Annotated[str, typer.Option(help="none|paddle")] = "none",
    full_page_dpi: Annotated[
        int, typer.Option(help="Full-page OCR render DPI: 300, 450, or 600.")
    ] = 300,
    image_region_dpi: Annotated[
        int, typer.Option(help="Embedded-image OCR crop DPI: 300, 450, or 600.")
    ] = 300,
    surgical_dpi: Annotated[
        int, typer.Option(help="Surgical OCR crop DPI: 300, 450, or 600.")
    ] = 450,
    subject: Annotated[str, typer.Option(help="Used for default run directory")] = "unknown",
    question: Annotated[str, typer.Option(help="Used for default run directory")] = "adhoc",
) -> None:
    parsed_pages = _parse_pages(pages)
    settings = LocalSettings()
    snapshot = snapshot_source(pdf, settings=settings)
    try:
        routing = OcrRoutingConfig(
            full_page_dpi=full_page_dpi,
            image_region_dpi=image_region_dpi,
            surgical_dpi=surgical_dpi,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    engine = None
    if ocr == "paddle":
        engine = PaddleOcrEngine(model_root=settings.models_dir / "paddleocr")
    elif ocr != "none":
        raise typer.BadParameter("ocr must be 'none' or 'paddle'")

    pipeline = PdfIngestPipeline(
        inspector=PdfInspector(render_dpi=full_page_dpi),
        ocr_engine=engine,
        routing_config=routing,
    )
    prepared = prepare_run(
        snapshot=snapshot,
        subject=subject,
        question=question,
        pages=parsed_pages,
        pipeline_config=pipeline.input_config(),
        output_dir=output_dir,
        settings=settings,
    )
    result = pipeline.run(snapshot, prepared, pages=parsed_pages)
    problem_markdown = next(prepared.output_dir.glob("*_problem.md"), None)
    validation_path = prepared.output_dir / "problem_validation.json"
    validation = (
        json.loads(validation_path.read_text(encoding="utf-8"))
        if validation_path.is_file()
        else None
    )
    console.print(
        json.dumps(
            {
                "run_id": prepared.manifest.run_id,
                "output_dir": str(prepared.output_dir),
                "source_sha256": snapshot.sha256,
                "pages": len(result.pages),
                "ocr_recommended": [p.page_number for p in result.pages if p.ocr_recommended],
                "vision_review": [
                    p.page_number for p in result.pages if p.vision_review_recommended
                ],
                "problem_markdown": (
                    problem_markdown.name if problem_markdown is not None else None
                ),
                "handoff_markdown": (
                    handoff_markdown_filename(
                        prepared.manifest.subject,
                        prepared.manifest.question,
                    )
                    if (
                        prepared.output_dir
                        / handoff_markdown_filename(
                            prepared.manifest.subject,
                            prepared.manifest.question,
                        )
                    ).is_file()
                    else None
                ),
                "canonical_source": (
                    "canonical_source.json"
                    if (prepared.output_dir / "canonical_source.json").is_file()
                    else None
                ),
                "needs_review_count": (
                    validation.get("needs_review_count") if validation else None
                ),
                "problem_packet_valid": validation.get("valid") if validation else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
