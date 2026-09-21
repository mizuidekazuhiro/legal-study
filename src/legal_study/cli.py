from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.paddle import PaddleOcrEngine
from legal_study.pdf.pipeline import PdfIngestPipeline

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
def inspect(
    pdf: Annotated[Path, typer.Argument(exists=True, readable=True)],
    pages: Annotated[str | None, typer.Option(help="1-based pages, e.g. 110-116,120")] = None,
    render_dir: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    result = PdfInspector().inspect(pdf, pages=_parse_pages(pages), render_dir=render_dir)
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
    output_dir: Annotated[Path, typer.Option("--output", "-o")],
    pages: Annotated[str | None, typer.Option(help="1-based pages, e.g. 110-116")] = None,
    ocr: Annotated[str, typer.Option(help="none|paddle")] = "none",
) -> None:
    engine = None
    if ocr == "paddle":
        engine = PaddleOcrEngine()
    elif ocr != "none":
        raise typer.BadParameter("ocr must be 'none' or 'paddle'")

    result = PdfIngestPipeline(ocr_engine=engine).run(
        pdf, output_dir, pages=_parse_pages(pages)
    )
    console.print(
        json.dumps(
            {
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
