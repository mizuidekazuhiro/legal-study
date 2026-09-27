from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.book.models import BookChapter, BookManifest, BookPage, BookSection
from legal_study.book.text_verification import BookTextStatus

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{value}" for value in range(1, 10)),
    *(f"LPT{value}" for value in range(1, 10)),
}


class BookExportSummary(BaseModel):
    written: int = 0
    reused: int = 0
    files: list[str] = Field(default_factory=list)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).rstrip(" .")
    cleaned = cleaned or "_"
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _publish_bytes(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_bytes() == content:
            return "reused"
        raise FileExistsError(f"Refusing to overwrite existing Vault file: {path}")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "written"


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _frontmatter(
    manifest: BookManifest,
    chapter: BookChapter,
    section: BookSection | None,
    pages: list[BookPage],
) -> str:
    printed = [page.printed_page for page in pages if page.printed_page is not None]
    pdf_pages = [page.pdf_page for page in pages]
    printed_range = (
        str(printed[0]) if printed and printed[0] == printed[-1] else f"{min(printed)}-{max(printed)}"
        if printed
        else "unknown"
    )
    pdf_range = (
        str(pdf_pages[0])
        if pdf_pages and pdf_pages[0] == pdf_pages[-1]
        else f"{min(pdf_pages)}-{max(pdf_pages)}"
    )
    values = [
        "---",
        f"subject: {_yaml_string(manifest.subject)}",
        f"book: {_yaml_string(manifest.book)}",
        f"chapter: {_yaml_string(f'第{chapter.chapter}章 {chapter.title}')}",
    ]
    if section is not None:
        values.append(f"section: {_yaml_string(f'{section.id} {section.title}')}")
    values.extend(
        [
            f"printed_pages: {_yaml_string(printed_range)}",
            f"pdf_pages: {_yaml_string(pdf_range)}",
            f"source_sha256: {manifest.source_sha256}",
            "---",
        ]
    )
    return "\n".join(values)


def _overlaps_figure(page: BookPage, record) -> bool:
    for figure in page.figures:
        if not (
            record.bbox.x1 <= figure.bbox.x0
            or figure.bbox.x1 <= record.bbox.x0
            or record.bbox.y1 <= figure.bbox.y0
            or figure.bbox.y1 <= record.bbox.y0
        ):
            return True
    return False


def _render_page(page: BookPage) -> list[str]:
    printed = str(page.printed_page) if page.printed_page is not None else "UNKNOWN"
    lines = [f"<!-- PDF_PAGE:{page.pdf_page} / PRINTED_PAGE:{printed} -->", ""]
    for record in sorted(page.text_records, key=lambda item: (item.bbox.y0, item.bbox.x0)):
        if _overlaps_figure(page, record):
            continue
        if record.canonical_text is None:
            lines.append(f"<!-- {record.status.value}: {record.id} -->")
            continue
        lines.append(record.canonical_text.rstrip())
        if record.status == BookTextStatus.VISUALLY_REPAIRED:
            lines.append(
                f"<!-- REPAIR_PROVENANCE: {record.id} / {record.ocr_evidence_id or 'none'} -->"
            )
    for figure in page.figures:
        lines.extend(["", f"![[assets/{figure.asset_name}]]"])
    for annotation in page.annotations:
        label = "rank" if annotation.text and "Rank" in annotation.text else "annotation"
        lines.extend(["", f"> [!{label}] {annotation.text or annotation.status.value}"])
        if annotation.linked_heading:
            lines.append(f"> linked heading: {annotation.linked_heading}")
    for highlight in page.highlights:
        if highlight.kind == "highlight":
            lines.extend(
                [
                    "",
                    f"> [!highlight] {highlight.color_family}",
                    f"> {highlight.linked_text or highlight.status.value}",
                ]
            )
    lines.append("")
    return lines


class ObsidianExporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.expanduser().resolve()

    def _record(self, summary: BookExportSummary, path: Path, status: str) -> None:
        if status == "written":
            summary.written += 1
        else:
            summary.reused += 1
        summary.files.append(path.as_posix())

    def export(self, manifest: BookManifest, pages: list[BookPage]) -> BookExportSummary:
        summary = BookExportSummary()
        root = self.output_dir / safe_filename(manifest.subject) / safe_filename(manifest.book)
        toc_lines = [f"# {manifest.book}", ""]
        for chapter in manifest.chapters:
            folder = f"第{chapter.chapter:02d}章_{safe_filename(chapter.title)}"
            toc_lines.append(f"- [[{folder}/00_{safe_filename(chapter.title)}|第{chapter.chapter}章 {chapter.title}]]")
            chapter_dir = root / folder
            chapter_pages = [page for page in pages if page.chapter == chapter.chapter]
            overview = [
                _frontmatter(manifest, chapter, None, chapter_pages),
                "",
                f"# 第{chapter.chapter}章 {chapter.title}",
                "",
            ]
            for index, section in enumerate(chapter.sections, start=1):
                overview.append(f"- [[{index:02d}_{safe_filename(section.title)}|{section.id} {section.title}]]")
                section_pages = [page for page in chapter_pages if page.section_id == section.id]
                body = [
                    _frontmatter(manifest, chapter, section, section_pages),
                    "",
                    f"# {section.id} {section.title}",
                    "",
                ]
                for page in section_pages:
                    body.extend(_render_page(page))
                    for figure in page.figures:
                        if not figure.source_image.is_file():
                            raise FileNotFoundError(figure.source_image)
                        destination = chapter_dir / "assets" / figure.asset_name
                        status = _publish_bytes(destination, figure.source_image.read_bytes())
                        self._record(summary, destination, status)
                note = chapter_dir / f"{index:02d}_{safe_filename(section.title)}.md"
                status = _publish_bytes(note, ("\n".join(body).rstrip() + "\n").encode("utf-8"))
                self._record(summary, note, status)
            overview_path = chapter_dir / f"00_{safe_filename(chapter.title)}.md"
            status = _publish_bytes(
                overview_path,
                ("\n".join(overview).rstrip() + "\n").encode("utf-8"),
            )
            self._record(summary, overview_path, status)
        toc_path = root / "00_目次.md"
        status = _publish_bytes(toc_path, ("\n".join(toc_lines) + "\n").encode("utf-8"))
        self._record(summary, toc_path, status)
        manifest_path = root / "book_manifest.json"
        payload = manifest.model_dump(mode="json")
        payload["pages"] = [page.model_dump(mode="json") for page in pages]
        status = _publish_bytes(
            manifest_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        self._record(summary, manifest_path, status)
        return summary
