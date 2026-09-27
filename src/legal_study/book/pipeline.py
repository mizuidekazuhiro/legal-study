from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pymupdf
from PIL import Image
from pydantic import BaseModel, Field

from legal_study.book.figures import detect_figure_regions
from legal_study.book.markup import (
    AnnotationEvidence,
    MarkupEvidence,
    WordBox,
    detect_raster_markup,
    extract_rank_annotation,
)
from legal_study.book.models import (
    BookChapter,
    BookFigure,
    BookManifest,
    BookPage,
    BookSection,
    PrintedPageMapping,
)
from legal_study.book.obsidian_exporter import BookExportSummary, ObsidianExporter
from legal_study.book.page_zones import build_page_zones, zones_for_bbox
from legal_study.book.text_verification import (
    BookTextStatus,
    reconcile_text_record,
    should_route_surgical_ocr,
)
from legal_study.book.toc_parser import (
    PrintedPageAnchor,
    build_printed_page_map,
    parse_toc_text,
)
from legal_study.io_utils import atomic_output_path, atomic_write_json, file_sha256
from legal_study.models import BBox, DocumentInspection, PageInspection
from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.base import (
    CoordinateTransform,
    OcrBackendMetadata,
    OcrEngine,
    OcrInputMetadata,
    OcrResult,
)
from legal_study.pdf.ocr.cache import SharedOcrCache, shared_cache_identity
from legal_study.run_manifest import PreparedRun, prepare_run, update_manifest_page_count
from legal_study.settings import LocalSettings
from legal_study.source_store import SourceSnapshot, verify_snapshot
from legal_study.state import RunStateStore

_PIPELINE_VERSION = "1"
_STRUCTURE_VERSION = "1"
_INSPECTION_VERSION = "1"
_PAGES_VERSION = "1"
_CHAPTER_RE = re.compile(r"第\s*(\d+)\s*章\s*([^\r\n〈<く]+)")
_SECTION_HEADER_RE = re.compile(r"[〈<く]\s*([^〉>）\r\n]+)\s*[〉>）]")
_PRINTED_PAGE_RE = re.compile(r"(?:刑\s*[-－]\s*)?(\d{1,4})\s*$")
_RANK_RE = re.compile(r"\b[ABC]\s*\+?\s*Rank\b", re.IGNORECASE)


class BookPipelineConfig(BaseModel):
    chapters: list[int] | None = None
    render_dpi: int = 300
    surgical_dpi: int = 450
    toc_ocr_dpi: int = 300
    preserve_markup: bool = True
    verbatim: bool = True
    detect_markup: bool = True
    detect_figures: bool = True
    surgical_padding_points: float = 2.0


class StructurePage(BaseModel):
    pdf_page: int
    printed_page: int | None = None
    chapter: int | None = None
    chapter_title: str | None = None
    section_title: str | None = None


class BookStructureScan(BaseModel):
    structure_source: str
    chapters: list[BookChapter]
    pages: list[StructurePage]
    selected_pdf_pages: list[int]
    toc_pdf_pages: list[int] = Field(default_factory=list)


class BookPipelineResult(BaseModel):
    run_id: str
    run_dir: Path
    output_dir: Path
    source_sha256: str
    selected_pdf_pages: list[int]
    pages: list[BookPage]
    processed: int = 0
    reused: int = 0
    cache_hit: int = 0
    ocr_executed: int = 0
    needs_review: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0
    export: BookExportSummary


class _Counters:
    def __init__(self) -> None:
        self.processed = 0
        self.reused = 0
        self.cache_hit = 0
        self.ocr_executed = 0
        self.failed = 0


def _hash_values(*values: object) -> str:
    encoded = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _line_bbox(line: dict[str, Any]) -> BBox:
    values = line["bbox"]
    return BBox(x0=values[0], y0=values[1], x1=values[2], y1=values[3])


def _native_lines(page: pymupdf.Page) -> list[tuple[str, BBox]]:
    output: list[tuple[str, BBox]] = []
    raw = page.get_text("dict", sort=True)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            if text.strip():
                output.append((text.strip(), _line_bbox(line)))
    return output


def _printed_page_from_lines(
    lines: list[tuple[str, BBox]], *, width: float, height: float
) -> int | None:
    candidates: list[tuple[float, int]] = []
    for text, bbox in lines:
        outer_edge = bbox.x1 <= width * 0.10 or bbox.x0 >= width * 0.90
        if bbox.y0 < height * 0.93 or not outer_edge:
            continue
        match = _PRINTED_PAGE_RE.search(str(text).replace(" ", ""))
        if match:
            candidates.append((bbox.y1, int(match.group(1))))
    if candidates:
        return max(candidates)[1]
    return None


def _header_fields(
    lines: list[tuple[str, BBox]], *, height: float
) -> tuple[int | None, str | None, str | None]:
    chapter: int | None = None
    chapter_title: str | None = None
    section_title: str | None = None
    for text, bbox in lines:
        if bbox.y0 > height * 0.12:
            break
        chapter_match = _CHAPTER_RE.search(text)
        if chapter_match and bbox.y0 <= height * 0.11:
            chapter = int(chapter_match.group(1))
            chapter_title = re.sub(r"\s{2,}.*$", "", chapter_match.group(2)).strip()
        section_match = _SECTION_HEADER_RE.search(text)
        if section_match:
            section_title = section_match.group(1).strip()
    return chapter, chapter_title, section_title


def _header_fields_text(text: str) -> tuple[int | None, str | None, str | None]:
    header = "\n".join(text.splitlines()[:10])
    chapter_match = _CHAPTER_RE.search(header)
    section_match = _SECTION_HEADER_RE.search(header)
    title = None
    chapter = None
    if chapter_match:
        chapter = int(chapter_match.group(1))
        title = re.sub(r"\s{2,}.*$", "", chapter_match.group(2)).strip()
    return (
        chapter,
        title,
        section_match.group(1).strip() if section_match else None,
    )


def _toc_geometry(
    lines: list[tuple[str, BBox]], *, width: float
) -> tuple[list[BookChapter], dict[str, int]]:
    """Recover TOC hierarchy from positioned native fragments, not reading order."""
    chapters: list[BookChapter] = []
    section_pages: dict[str, int] = {}
    right_numbers = [
        (int(re.sub(r"\s+", "", text)), bbox)
        for text, bbox in lines
        if bbox.x0 >= width * 0.60 and re.fullmatch(r"[0-9０-９\s]{1,6}", text)
    ]
    ranks = [
        (re.sub(r"\s+", "", text).upper().replace("RANK", " Rank"), bbox)
        for text, bbox in lines
        if bbox.x0 >= width * 0.70
        and re.fullmatch(r"[ABCabc](?:\+)?(?:\s*Rank)?", text.strip())
    ]
    left_numbers = [
        (int(unicodedata.normalize("NFKC", text.strip())), bbox)
        for text, bbox in lines
        if bbox.x0 <= width * 0.12
        and re.fullmatch(r"[0-9０-９]{1,2}", text.strip())
    ]
    for text, bbox in lines:
        compact = re.sub(r"[.．・…·]+", "", text).strip()
        if not compact or not (width * 0.10 <= bbox.x0 <= width * 0.60):
            continue
        center = (bbox.y0 + bbox.y1) / 2
        page_number = next(
            (
                candidate
                for candidate, candidate_box in right_numbers
                if abs((candidate_box.y0 + candidate_box.y1) / 2 - center) <= 9
            ),
            None,
        )
        if page_number is None:
            continue
        section_pages.setdefault(_normalized(compact), page_number)
        rank = next(
            (
                re.sub(r"\s+", "", rank_text).upper().replace("RANK", " Rank")
                for rank_text, rank_box in ranks
                if abs((rank_box.y0 + rank_box.y1) / 2 - center) <= 12
            ),
            None,
        )
        if rank is None:
            continue
        if not rank.endswith("Rank"):
            rank = f"{rank} Rank"
        number = next(
            (
                number_text
                for number_text, number_box in left_numbers
                if abs((number_box.y0 + number_box.y1) / 2 - center) <= 15
            ),
            None,
        )
        if number is None or len(compact) < 2:
            continue
        chapters.append(
            BookChapter(
                chapter=number,
                title=compact,
                printed_start_page=page_number,
                rank_annotation=rank,
            )
        )
    return chapters, section_pages


def _clip(box: pymupdf.Rect, bbox: BBox, padding: float = 0.0) -> pymupdf.Rect:
    return pymupdf.Rect(
        max(box.x0, bbox.x0 - padding),
        max(box.y0, bbox.y0 - padding),
        min(box.x1, bbox.x1 + padding),
        min(box.y1, bbox.y1 + padding),
    )


class BookPipeline:
    """Thin Book/Corpus layer over immutable source, inspection, and OCR primitives."""

    def __init__(
        self,
        *,
        config: BookPipelineConfig | None = None,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        self.config = config or BookPipelineConfig()
        self.ocr_engine = ocr_engine

    def input_config(self) -> dict[str, object]:
        metadata = getattr(self.ocr_engine, "metadata", None)
        backend = (
            metadata.model_dump(mode="json")
            if isinstance(metadata, OcrBackendMetadata)
            else None
        )
        return {
            "mode": "book_corpus",
            "pipeline_version": _PIPELINE_VERSION,
            **self.config.model_dump(mode="json"),
            "ocr_engine": self.ocr_engine.name if self.ocr_engine else "none",
            "ocr_backend": backend,
        }

    def _prepare(
        self,
        snapshot: SourceSnapshot,
        *,
        settings: LocalSettings,
        subject: str,
        book: str,
    ) -> PreparedRun:
        chapters = self.config.chapters
        chapter_label = "all" if chapters is None else "-".join(map(str, chapters))
        return prepare_run(
            snapshot=snapshot,
            subject=f"book-{subject}",
            question=f"{book}-chapters-{chapter_label}",
            pages=None,
            pipeline_config=self.input_config(),
            settings=settings,
        )

    @staticmethod
    def _resumable(
        state: RunStateStore,
        prepared: PreparedRun,
        step: str,
        input_hash: str,
        version: str,
        artifact: Path,
    ) -> bool:
        return bool(
            artifact.is_file()
            and state.can_resume(
                prepared.manifest.run_id,
                step,
                input_hash=input_hash,
                version=version,
                output_hash=file_sha256(artifact),
            )
        )

    def _structure_step(
        self,
        snapshot: SourceSnapshot,
        prepared: PreparedRun,
        state: RunStateStore,
    ) -> BookStructureScan:
        artifact = prepared.output_dir / "book_structure.json"
        input_hash = _hash_values(prepared.manifest.input_hash, "BOOK_STRUCTURE")
        if self._resumable(
            state, prepared, "BOOK_STRUCTURE", input_hash, _STRUCTURE_VERSION, artifact
        ):
            return BookStructureScan.model_validate_json(artifact.read_text(encoding="utf-8"))
        state.begin_step(
            prepared.manifest.run_id,
            "BOOK_STRUCTURE",
            input_hash=input_hash,
            version=_STRUCTURE_VERSION,
        )
        try:
            scan = self._scan_structure(snapshot.snapshot_path)
            atomic_write_json(artifact, scan.model_dump(mode="json"))
            state.complete_step(
                prepared.manifest.run_id,
                "BOOK_STRUCTURE",
                output_hash=file_sha256(artifact),
            )
            return scan
        except Exception as exc:
            state.fail_step(prepared.manifest.run_id, "BOOK_STRUCTURE", error=str(exc))
            raise

    def _scan_structure(self, source: Path) -> BookStructureScan:
        document = pymupdf.open(source)
        try:
            scanned: list[StructurePage] = []
            heading_runs: list[dict[str, object]] = []
            toc_texts: list[str] = []
            toc_pages: list[int] = []
            geometry_chapters: list[BookChapter] = []
            toc_section_pages: dict[str, int] = {}
            for index, page in enumerate(document):
                pdf_page = index + 1
                text = page.get_text("text", sort=True)
                is_toc = pdf_page <= 16 and "目次" in text
                if is_toc:
                    toc_texts.append(text)
                    toc_pages.append(pdf_page)
                    lines = _native_lines(page)
                    found_chapters, found_sections = _toc_geometry(
                        lines, width=page.rect.width
                    )
                    geometry_chapters.extend(found_chapters)
                    toc_section_pages.update(found_sections)
                chapter, title, section_title = _header_fields_text(text)
                if is_toc:
                    chapter = None
                    title = None
                    section_title = None
                if chapter is not None and title is not None:
                    if (
                        heading_runs
                        and heading_runs[-1]["chapter"] == chapter
                        and _normalized(str(heading_runs[-1]["title"]))
                        == _normalized(title)
                        and int(heading_runs[-1]["end"]) == pdf_page - 1
                    ):
                        heading_runs[-1]["end"] = pdf_page
                    else:
                        heading_runs.append(
                            {
                                "start": pdf_page,
                                "end": pdf_page,
                                "chapter": chapter,
                                "title": title,
                            }
                        )
                scanned.append(
                    StructurePage(
                        pdf_page=pdf_page,
                        printed_page=None,
                        chapter=chapter,
                        chapter_title=title,
                        section_title=section_title,
                    )
                )

            for run in heading_runs:
                page_number = int(run["start"])
                page = document[page_number - 1]
                scanned[page_number - 1].printed_page = _printed_page_from_lines(
                    _native_lines(page),
                    width=page.rect.width,
                    height=page.rect.height,
                )

            toc_chapters = parse_toc_text("\n".join(toc_texts))
            if not toc_chapters:
                deduplicated_geometry: dict[tuple[int, int], BookChapter] = {}
                for chapter in geometry_chapters:
                    deduplicated_geometry.setdefault(
                        (chapter.chapter, chapter.printed_start_page), chapter
                    )
                toc_chapters = list(deduplicated_geometry.values())
            chapter_by_number = {item.chapter: item for item in toc_chapters}
            selected_numbers = (
                sorted(chapter_by_number)
                if self.config.chapters is None
                else sorted(set(self.config.chapters))
            )
            for number in selected_numbers:
                chapter = chapter_by_number.get(number)
                candidates = [
                    run
                    for run in heading_runs
                    if int(run["chapter"]) == number
                    and (
                        chapter is None
                        or _normalized(chapter.title) in _normalized(str(run["title"]))
                        or _normalized(str(run["title"])) in _normalized(chapter.title)
                    )
                ]
                if chapter is not None:
                    exact = [
                        run
                        for run in candidates
                        if next(
                            (
                                item.printed_page
                                for item in scanned
                                if item.pdf_page == int(run["start"])
                            ),
                            None,
                        )
                        == chapter.printed_start_page
                    ]
                    if exact:
                        candidates = exact
                if not candidates:
                    continue
                run = min(candidates, key=lambda item: int(item["start"]))
                if chapter is None:
                    printed_page = next(
                        (
                            item.printed_page
                            for item in scanned
                            if item.pdf_page == int(run["start"])
                            and item.printed_page is not None
                        ),
                        int(run["start"]),
                    )
                    chapter = BookChapter(
                        chapter=number,
                        title=str(run["title"]),
                        printed_start_page=printed_page,
                    )
                    chapter_by_number[number] = chapter
                chapter.pdf_start_page = int(run["start"])
                chapter.pdf_end_page = int(run["end"])

            selected_ranges = [
                (chapter.pdf_start_page, chapter.pdf_end_page)
                for chapter in chapter_by_number.values()
                if chapter.chapter in selected_numbers
                and chapter.pdf_start_page is not None
                and chapter.pdf_end_page is not None
            ]
            for start, end in selected_ranges:
                assert start is not None and end is not None
                for page_number in range(start, end + 1):
                    page = document[page_number - 1]
                    lines = _native_lines(page)
                    scanned[page_number - 1].printed_page = _printed_page_from_lines(
                        lines,
                        width=page.rect.width,
                        height=page.rect.height,
                    )

            anchors = [
                PrintedPageAnchor(pdf_page=item.pdf_page, printed_page=item.printed_page)
                for item in scanned
                if item.printed_page is not None
            ]
            page_map = build_printed_page_map(anchors)
            for chapter in chapter_by_number.values():
                if chapter.pdf_start_page is None:
                    chapter.pdf_start_page = page_map.pdf_page_for_printed(
                        chapter.printed_start_page
                    )
                if chapter.pdf_start_page is None:
                    continue
                relevant = [
                    item
                    for item in scanned
                    if chapter.pdf_start_page
                    <= item.pdf_page
                    <= (chapter.pdf_end_page or document.page_count)
                    and item.section_title
                ]
                if not chapter.sections:
                    seen: set[str] = set()
                    for section_page in relevant:
                        assert section_page.section_title is not None
                        key = _normalized(section_page.section_title)
                        if key in seen:
                            continue
                        seen.add(key)
                        chapter.sections.append(
                            BookSection(
                                id=f"{chapter.chapter}-{len(chapter.sections) + 1}",
                                title=section_page.section_title,
                                printed_page=toc_section_pages.get(
                                    _normalized(section_page.section_title),
                                    section_page.printed_page
                                    or chapter.printed_start_page,
                                ),
                                pdf_page=section_page.pdf_page,
                            )
                        )
                else:
                    for section in chapter.sections:
                        matched = next(
                            (
                                item
                                for item in relevant
                                if _normalized(section.title)
                                in _normalized(item.section_title or "")
                                or _normalized(item.section_title or "")
                                in _normalized(section.title)
                            ),
                            None,
                        )
                        section.pdf_page = (
                            matched.pdf_page
                            if matched is not None
                            else page_map.pdf_page_for_printed(section.printed_page)
                        )

            missing = [number for number in selected_numbers if number not in chapter_by_number]
            if missing:
                raise ValueError(f"Requested chapters were not found: {missing}")
            selected_chapters = [chapter_by_number[number] for number in selected_numbers]
            selected_pages: set[int] = set()
            for chapter in selected_chapters:
                if chapter.pdf_start_page is None:
                    raise ValueError(f"PDF start page is unresolved for chapter {chapter.chapter}")
                end = chapter.pdf_end_page or document.page_count
                selected_pages.update(range(chapter.pdf_start_page, end + 1))
            return BookStructureScan(
                structure_source=(
                    "toc_native_geometry" if toc_chapters else "heading_fallback"
                ),
                chapters=selected_chapters,
                pages=scanned,
                selected_pdf_pages=sorted(selected_pages),
                toc_pdf_pages=toc_pages,
            )
        finally:
            document.close()

    def _inspection_step(
        self,
        snapshot: SourceSnapshot,
        prepared: PreparedRun,
        state: RunStateStore,
        structure: BookStructureScan,
    ) -> DocumentInspection:
        artifact = prepared.output_dir / "book_inspection.json"
        input_hash = _hash_values(
            prepared.manifest.input_hash,
            "BOOK_INSPECTION",
            structure.selected_pdf_pages,
        )
        if self._resumable(
            state,
            prepared,
            "BOOK_INSPECTION",
            input_hash,
            _INSPECTION_VERSION,
            artifact,
        ):
            return DocumentInspection.model_validate_json(artifact.read_text(encoding="utf-8"))
        state.begin_step(
            prepared.manifest.run_id,
            "BOOK_INSPECTION",
            input_hash=input_hash,
            version=_INSPECTION_VERSION,
        )
        try:
            inspection = PdfInspector(render_dpi=self.config.render_dpi).inspect(
                snapshot.snapshot_path,
                pages=structure.selected_pdf_pages,
                render_dir=prepared.output_dir / "book_renders",
                artifact_root=prepared.output_dir,
            )
            atomic_write_json(artifact, inspection.model_dump(mode="json"))
            state.complete_step(
                prepared.manifest.run_id,
                "BOOK_INSPECTION",
                output_hash=file_sha256(artifact),
            )
            return inspection
        except Exception as exc:
            state.fail_step(prepared.manifest.run_id, "BOOK_INSPECTION", error=str(exc))
            raise

    def _backend(self) -> OcrBackendMetadata | None:
        metadata = getattr(self.ocr_engine, "metadata", None)
        return metadata if isinstance(metadata, OcrBackendMetadata) else None

    @staticmethod
    def _enrich_ocr(result: OcrResult, target: dict[str, object]) -> OcrResult:
        transform = CoordinateTransform.model_validate(target["coordinate_transform"])
        values = target["bbox"]
        assert isinstance(values, list)
        input_metadata = OcrInputMetadata(
            source_kind=str(target["kind"]),
            page_number=int(target["page_number"]),
            image=str(target["image"]),
            image_sha256=str(target["image_sha256"]),
            dpi=int(target["dpi"]),
            crop_bbox=BBox(x0=values[0], y0=values[1], x1=values[2], y1=values[3]),
            crop_padding_points=float(target["crop_padding_points"]),
            preprocessing=dict(target["preprocessing"]),  # type: ignore[arg-type]
            coordinate_transform=transform,
        )
        lines = [
            line.model_copy(
                update={"pdf_bbox": transform.map_bbox(line.bbox) if line.bbox else None}
            )
            for line in result.lines
        ]
        return result.model_copy(
            update={
                "input": input_metadata,
                "lines": lines,
                "executed_at": result.executed_at or datetime.now(UTC),
            }
        )

    def _render_target(
        self,
        page: pymupdf.Page,
        bbox: BBox,
        *,
        kind: str,
        stable_page_id: str,
        path: Path,
        dpi: int,
        artifact_root: Path,
    ) -> dict[str, object]:
        padding = self.config.surgical_padding_points
        clip = _clip(page.cropbox, bbox, padding)
        pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        with atomic_output_path(path) as temporary:
            pixmap.save(temporary)
        image_width, image_height = pixmap.width, pixmap.height
        transform = CoordinateTransform(
            pixel_to_pdf=(
                clip.width / image_width,
                0.0,
                0.0,
                clip.height / image_height,
                clip.x0,
                clip.y0,
            ),
            image_width_px=image_width,
            image_height_px=image_height,
            pdf_bbox=BBox(x0=clip.x0, y0=clip.y0, x1=clip.x1, y1=clip.y1),
            page_rotation=page.rotation,
        )
        return {
            "kind": kind,
            "page_number": page.number + 1,
            "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
            "image": path.relative_to(artifact_root).as_posix(),
            "image_sha256": file_sha256(path),
            "dpi": dpi,
            "crop_padding_points": padding,
            "preprocessing": {
                "renderer": "PyMuPDF",
                "colorspace": "rgb",
                "alpha": False,
                "deskew": False,
                "binarization": False,
            },
            "coordinate_transform": transform.model_dump(mode="json"),
            "stable_page_id": stable_page_id,
        }

    def _ocr_target(
        self,
        *,
        stable_page_id: str,
        target: dict[str, object],
        image_path: Path,
        cache: SharedOcrCache,
        snapshot: SourceSnapshot,
        prepared: PreparedRun,
        counters: _Counters,
    ) -> tuple[OcrResult | None, str | None]:
        if self.ocr_engine is None:
            return None, None
        cache_key, _identity = shared_cache_identity(
            stable_page_id=stable_page_id,
            target=target,
            backend=self._backend(),
        )
        cached = cache.load(
            stable_page_id=stable_page_id,
            target=target,
            backend=self._backend(),
        )
        if cached is not None:
            counters.cache_hit += 1
            return self._enrich_ocr(OcrResult.model_validate(cached.result), target), cache_key
        result = self._enrich_ocr(self.ocr_engine.recognize(image_path), target)
        counters.ocr_executed += 1
        cache.store(
            stable_page_id=stable_page_id,
            target=target,
            backend=self._backend(),
            result=result,
            provenance={
                "source_sha256": snapshot.sha256,
                "source_page": int(target["page_number"]),
                "run_id": prepared.manifest.run_id,
                "mode": "book_surgical",
            },
        )
        return result, cache_key

    @staticmethod
    def _section_for_page(chapter: BookChapter, pdf_page: int) -> BookSection:
        available = sorted(
            (section for section in chapter.sections if section.pdf_page is not None),
            key=lambda item: item.pdf_page or 0,
        )
        selected = next(
            (
                section
                for section in reversed(available)
                if (section.pdf_page or 0) <= pdf_page
            ),
            None,
        )
        if selected is not None:
            return selected
        if chapter.sections:
            return chapter.sections[0]
        return BookSection(
            id=f"{chapter.chapter}-1",
            title=chapter.title,
            printed_page=chapter.printed_start_page,
            pdf_page=chapter.pdf_start_page,
        )

    @staticmethod
    def _crop_image(
        image: Image.Image,
        bbox: BBox,
        *,
        page_width: float,
        page_height: float,
        path: Path,
        padding_points: float = 3.0,
    ) -> None:
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        crop = image.crop(
            (
                max(0, round((bbox.x0 - padding_points) * scale_x)),
                max(0, round((bbox.y0 - padding_points) * scale_y)),
                min(image.width, round((bbox.x1 + padding_points) * scale_x)),
                min(image.height, round((bbox.y1 + padding_points) * scale_y)),
            )
        )
        with atomic_output_path(path) as temporary:
            crop.save(temporary, format="PNG")

    def _process_page(
        self,
        *,
        document: pymupdf.Document,
        inspected: PageInspection,
        structure: BookStructureScan,
        snapshot: SourceSnapshot,
        prepared: PreparedRun,
        cache: SharedOcrCache,
        counters: _Counters,
    ) -> BookPage:
        pdf_page = inspected.page_number
        chapter = next(
            item
            for item in structure.chapters
            if (item.pdf_start_page or 0)
            <= pdf_page
            <= (item.pdf_end_page or document.page_count)
        )
        section = self._section_for_page(chapter, pdf_page)
        structure_page = next(item for item in structure.pages if item.pdf_page == pdf_page)
        stable_page_id = inspected.stable_page_id
        if stable_page_id is None:
            raise ValueError(f"stable_page_id missing for PDF page {pdf_page}")
        page = document[pdf_page - 1]
        zones = build_page_zones(
            width=inspected.width,
            height=inspected.height,
            page_number=pdf_page,
            spans=inspected.spans,
            drawings=inspected.raw_vector_drawings,
            images=inspected.raw_image_regions,
        )
        render_path = prepared.output_dir / str(inspected.rendered_image)
        with Image.open(render_path) as opened:
            rendered = opened.convert("RGB")
        words = [
            WordBox(
                text=str(word[4]),
                bbox=BBox(x0=word[0], y0=word[1], x1=word[2], y1=word[3]),
                order=index,
            )
            for index, word in enumerate(page.get_text("words", sort=True))
        ]

        figures: list[BookFigure] = []
        if self.config.detect_figures:
            regions = detect_figure_regions(
                rendered,
                page_width=inspected.width,
                page_height=inspected.height,
                body_bbox=zones.boxes["main_body"],
            )
            for index, bbox in enumerate(regions, start=1):
                asset_name = (
                    f"ch{chapter.chapter:02d}_p{pdf_page:04d}_fig_{index:03d}.png"
                )
                source_image = prepared.output_dir / "book_assets" / asset_name
                self._crop_image(
                    rendered,
                    bbox,
                    page_width=inspected.width,
                    page_height=inspected.height,
                    path=source_image,
                )
                figures.append(
                    BookFigure(
                        id=f"p{pdf_page:04d}-fig-{index:03d}",
                        pdf_page=pdf_page,
                        bbox=bbox,
                        asset_name=asset_name,
                        source_image=source_image,
                    )
                )

        markup: list[MarkupEvidence] = []
        if self.config.preserve_markup and self.config.detect_markup:
            markup = detect_raster_markup(
                render_path,
                page_width=inspected.width,
                page_height=inspected.height,
                words=words,
            )

        annotations: list[AnnotationEvidence] = []
        text_records = []
        crop_dir = prepared.output_dir / "book_ocr_crops"
        for line_index, (raw_text, bbox) in enumerate(_native_lines(page), start=1):
            if _RANK_RE.search(raw_text):
                annotations.append(
                    extract_rank_annotation(
                        raw_text=raw_text,
                        ocr_text=None,
                        ocr_confidence=None,
                        pdf_page=pdf_page,
                        bbox=bbox,
                        linked_heading=section.title,
                    )
                )
                continue
            if "cf." in raw_text.lower():
                annotations.append(
                    AnnotationEvidence(
                        id=f"p{pdf_page:04d}-cf-{line_index:03d}",
                        text=raw_text,
                        raw_text=raw_text,
                        page=pdf_page,
                        bbox=bbox,
                        source_type="pdf_text_object",
                        status=BookTextStatus.NATIVE_VERIFIED,
                        linked_heading=section.title,
                    )
                )
                continue
            page_zones = zones_for_bbox(zones, bbox)
            if "main_body" not in page_zones or "binding" in page_zones:
                continue
            ocr_result: OcrResult | None = None
            evidence_id: str | None = None
            if should_route_surgical_ocr(raw_text, bbox, zones):
                crop_path = crop_dir / (
                    f"page-{pdf_page:04d}-line-{line_index:03d}.png"
                )
                target = self._render_target(
                    page,
                    bbox,
                    kind="book_suspicious_line",
                    stable_page_id=stable_page_id,
                    path=crop_path,
                    dpi=self.config.surgical_dpi,
                    artifact_root=prepared.output_dir,
                )
                target["native_candidate"] = raw_text
                ocr_result, evidence_id = self._ocr_target(
                    stable_page_id=stable_page_id,
                    target=target,
                    image_path=crop_path,
                    cache=cache,
                    snapshot=snapshot,
                    prepared=prepared,
                    counters=counters,
                )
            text_records.append(
                reconcile_text_record(
                    raw_text=raw_text,
                    ocr_text=ocr_result.text if ocr_result else None,
                    ocr_confidence=ocr_result.confidence if ocr_result else None,
                    pdf_page=pdf_page,
                    printed_page=structure_page.printed_page,
                    bbox=bbox,
                    ocr_evidence_id=evidence_id,
                    record_id=f"p{pdf_page:04d}-line-{line_index:03d}",
                )
            )

        annotation_index = 0
        for item in markup:
            if item.kind != "annotation" or item.linked_text:
                continue
            width = item.bbox.x1 - item.bbox.x0
            height = item.bbox.y1 - item.bbox.y0
            if width * height < 80 or width < 8 or height < 4:
                continue
            annotation_index += 1
            asset_name = (
                f"ch{chapter.chapter:02d}_p{pdf_page:04d}_ann_{annotation_index:03d}.png"
            )
            source_image = prepared.output_dir / "book_assets" / asset_name
            self._crop_image(
                rendered,
                item.bbox,
                page_width=inspected.width,
                page_height=inspected.height,
                path=source_image,
            )
            annotations.append(
                AnnotationEvidence(
                    id=f"p{pdf_page:04d}-ann-{annotation_index:03d}",
                    page=pdf_page,
                    bbox=item.bbox,
                    color=item.color_family,
                    raw_rgb=item.raw_rgb,
                    source_type="raster_palette_visual_evidence",
                    status=BookTextStatus.NEEDS_REVIEW,
                    asset_name=asset_name,
                    source_image=source_image,
                    evidence=["unlinked_colored_region_preserved_without_guessing"],
                )
            )

        return BookPage(
            pdf_page=pdf_page,
            printed_page=structure_page.printed_page,
            stable_page_id=stable_page_id,
            chapter=chapter.chapter,
            section_id=section.id,
            text_records=text_records,
            figures=figures,
            annotations=annotations,
            highlights=[item for item in markup if item.kind == "highlight"],
        )

    def _pages_step(
        self,
        snapshot: SourceSnapshot,
        prepared: PreparedRun,
        state: RunStateStore,
        structure: BookStructureScan,
        inspection: DocumentInspection,
        counters: _Counters,
        settings: LocalSettings,
    ) -> list[BookPage]:
        artifact = prepared.output_dir / "book_pages.json"
        input_hash = _hash_values(
            prepared.manifest.input_hash,
            "BOOK_PAGES",
            structure.model_dump(mode="json"),
            [item.stable_page_id for item in inspection.pages],
        )
        if self._resumable(
            state, prepared, "BOOK_PAGES", input_hash, _PAGES_VERSION, artifact
        ):
            pages = [
                BookPage.model_validate(item)
                for item in json.loads(artifact.read_text(encoding="utf-8"))
            ]
            counters.reused += len(pages)
            return pages
        state.begin_step(
            prepared.manifest.run_id,
            "BOOK_PAGES",
            input_hash=input_hash,
            version=_PAGES_VERSION,
        )
        cache = SharedOcrCache(settings.cache_dir)
        pages: list[BookPage] = []
        document = pymupdf.open(snapshot.snapshot_path)
        checkpoint_dir = prepared.output_dir / "book_page_checkpoints"
        try:
            for inspected in inspection.pages:
                checkpoint = checkpoint_dir / f"page-{inspected.page_number:04d}.json"
                page_hash = _hash_values(
                    prepared.manifest.input_hash,
                    inspected.stable_page_id,
                    self.input_config(),
                )
                if checkpoint.is_file():
                    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if payload.get("input_hash") == page_hash:
                        pages.append(BookPage.model_validate(payload["page"]))
                        counters.reused += 1
                        continue
                try:
                    page = self._process_page(
                        document=document,
                        inspected=inspected,
                        structure=structure,
                        snapshot=snapshot,
                        prepared=prepared,
                        cache=cache,
                        counters=counters,
                    )
                    atomic_write_json(
                        checkpoint,
                        {"input_hash": page_hash, "page": page.model_dump(mode="json")},
                    )
                    pages.append(page)
                    counters.processed += 1
                except Exception:
                    counters.failed += 1
                    raise
            atomic_write_json(
                artifact, [page.model_dump(mode="json") for page in pages]
            )
            state.complete_step(
                prepared.manifest.run_id,
                "BOOK_PAGES",
                output_hash=file_sha256(artifact),
            )
            return pages
        except Exception as exc:
            state.fail_step(prepared.manifest.run_id, "BOOK_PAGES", error=str(exc))
            raise
        finally:
            document.close()

    @staticmethod
    def _manifest(
        snapshot: SourceSnapshot,
        structure: BookStructureScan,
        *,
        subject: str,
        book: str,
    ) -> BookManifest:
        return BookManifest(
            source_sha256=snapshot.sha256,
            original_filename=snapshot.original_filename,
            subject=subject,
            book=book,
            structure_source=structure.structure_source,
            chapters=structure.chapters,
            page_mappings=[
                PrintedPageMapping(
                    pdf_page=item.pdf_page,
                    printed_page=item.printed_page,
                )
                for item in structure.pages
                if item.printed_page is not None
                and item.pdf_page in structure.selected_pdf_pages
            ],
        )

    def run(
        self,
        snapshot: SourceSnapshot,
        *,
        output_dir: Path,
        settings: LocalSettings | None = None,
        subject: str = "刑法",
        book: str = "基礎マスター 第1分冊",
    ) -> BookPipelineResult:
        started = perf_counter()
        cfg = settings or LocalSettings()
        cfg.ensure()
        verify_snapshot(snapshot)
        prepared = self._prepare(snapshot, settings=cfg, subject=subject, book=book)
        state = RunStateStore(prepared.state_db)
        state.register_run(prepared.manifest, prepared.manifest_path)
        counters = _Counters()
        try:
            structure = self._structure_step(snapshot, prepared, state)
            inspection = self._inspection_step(
                snapshot, prepared, state, structure
            )
            update_manifest_page_count(prepared, inspection.page_count)
            pages = self._pages_step(
                snapshot,
                prepared,
                state,
                structure,
                inspection,
                counters,
                cfg,
            )
            manifest = self._manifest(
                snapshot, structure, subject=subject, book=book
            )
            export = ObsidianExporter(output_dir).export(manifest, pages)
            state.set_run_status(prepared.manifest.run_id, "COMPLETED")
        except Exception as exc:
            state.set_run_status(prepared.manifest.run_id, "FAILED", str(exc))
            raise
        needs_review = sum(
            record.status in {BookTextStatus.NEEDS_REVIEW, BookTextStatus.UNRESOLVED}
            for page in pages
            for record in page.text_records
        ) + sum(
            annotation.status in {BookTextStatus.NEEDS_REVIEW, BookTextStatus.UNRESOLVED}
            for page in pages
            for annotation in page.annotations
        )
        result = BookPipelineResult(
            run_id=prepared.manifest.run_id,
            run_dir=prepared.output_dir,
            output_dir=output_dir.expanduser().resolve(),
            source_sha256=snapshot.sha256,
            selected_pdf_pages=structure.selected_pdf_pages,
            pages=pages,
            processed=counters.processed,
            reused=counters.reused,
            cache_hit=counters.cache_hit,
            ocr_executed=counters.ocr_executed,
            needs_review=needs_review,
            failed=counters.failed,
            elapsed_seconds=round(perf_counter() - started, 3),
            export=export,
        )
        atomic_write_json(
            prepared.output_dir / "book_summary.json", result.model_dump(mode="json")
        )
        return result
