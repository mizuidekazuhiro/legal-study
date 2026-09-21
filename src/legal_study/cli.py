from __future__ import annotations

import importlib.util
import json
import platform
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.paddle import PaddleOcrEngine
from legal_study.pdf.pipeline import PdfIngestPipeline
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
def doctor() -> None:
    """Check whether the core local-only PDF pipeline can run."""
    settings = LocalSettings()
    settings.ensure()
    checks = {
        "Python": platform.python_version(),
        "Platform": platform.platform(),
        "Workspace": str(settings.home),
        "PyMuPDF": "OK" if importlib.util.find_spec("fitz") else "MISSING",
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
        json_output.write_text(result.model_dump_json(indent=2), encoding="utf-8")

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
    subject: Annotated[str, typer.Option(help="Used for default run directory")] = "unknown",
    question: Annotated[str, typer.Option(help="Used for default run directory")] = "adhoc",
) -> None:
    parsed_pages = _parse_pages(pages)
    settings = LocalSettings()
    snapshot = snapshot_source(pdf, settings=settings)
    engine = None
    if ocr == "paddle":
        engine = PaddleOcrEngine()
    elif ocr != "none":
        raise typer.BadParameter("ocr must be 'none' or 'paddle'")

    pipeline = PdfIngestPipeline(ocr_engine=engine)
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
            },
            ensure_ascii=False,
            indent=2,
        )
    )
