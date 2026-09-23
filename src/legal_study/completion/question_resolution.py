from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path

import pymupdf
from pydantic import BaseModel, Field

from legal_study.completion.done_marker import DoneDetection, detect_done_markers
from legal_study.page_identity import ensure_source_page_index
from legal_study.pdf.ocr.base import OcrEngine
from legal_study.settings import LocalSettings
from legal_study.source_store import SourceSnapshot, snapshot_source
from legal_study.state import QuestionStateStore, QuestionStatus

_QUESTION_HEADER = re.compile(r"第\s*([0-9]{1,3})\s*問")


class QuestionResolution(BaseModel):
    done_page: int
    resolved: bool
    question: str | None = None
    start_page: int | None = None
    scanned_pages: list[int] = Field(default_factory=list)
    header_text: str | None = None
    header_confidence: float | None = None
    reason: str | None = None


class CompletionApplyResult(BaseModel):
    subject: str
    source_sha256: str
    done_detection: DoneDetection
    resolution: QuestionResolution
    previous_status: QuestionStatus | None = None
    current_status: QuestionStatus | None = None
    state_updated: bool = False


def _normalize_header_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _extract_question_number(text: str) -> str | None:
    match = _QUESTION_HEADER.search(_normalize_header_text(text))
    return match.group(1) if match else None


def _render_header_crop(
    page: pymupdf.Page,
    path: Path,
    *,
    dpi: int,
    height_ratio: float,
) -> None:
    height = page.rect.height * height_ratio
    clip = pymupdf.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + height)
    pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    pixmap.save(path)


def resolve_question_for_done_page(
    pdf: str | Path,
    done_page: int,
    *,
    ocr_engine: OcrEngine,
    temp_dir: Path,
    max_backtrack: int = 16,
    dpi: int = 200,
    header_height_ratio: float = 0.28,
) -> QuestionResolution:
    """Resolve a DONE page to the nearest preceding '第N問' title page.

    The resolver intentionally refuses to guess. It OCRs only the top region of
    candidate pages, walking backward from the DONE page until an exact question
    heading is found.
    """
    if max_backtrack < 0:
        raise ValueError("max_backtrack must be >= 0")
    if not 0.10 <= header_height_ratio <= 0.50:
        raise ValueError("header_height_ratio must be between 0.10 and 0.50")

    temp_dir.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(pdf)
    scanned_pages: list[int] = []
    try:
        if done_page < 1 or done_page > len(document):
            raise ValueError(f"Page out of range: {done_page}")

        first_page = max(1, done_page - max_backtrack)
        for page_number in range(done_page, first_page - 1, -1):
            scanned_pages.append(page_number)
            crop = temp_dir / (
                f"question-header-p{page_number:04d}-{uuid.uuid4().hex}.png"
            )
            try:
                _render_header_crop(
                    document[page_number - 1],
                    crop,
                    dpi=dpi,
                    height_ratio=header_height_ratio,
                )
                ocr = ocr_engine.recognize(crop)
            finally:
                crop.unlink(missing_ok=True)

            question = _extract_question_number(ocr.text)
            if question is not None:
                return QuestionResolution(
                    done_page=done_page,
                    resolved=True,
                    question=question,
                    start_page=page_number,
                    scanned_pages=scanned_pages,
                    header_text=ocr.text,
                    header_confidence=ocr.confidence,
                )
    finally:
        document.close()

    return QuestionResolution(
        done_page=done_page,
        resolved=False,
        scanned_pages=scanned_pages,
        reason=(
            "No exact '第N問' heading was found in the scanned header regions; "
            "question state was not changed."
        ),
    )


def apply_done_markers_to_snapshot(
    snapshot: SourceSnapshot,
    *,
    subject: str,
    ocr_engine: OcrEngine,
    settings: LocalSettings | None = None,
    pages: list[int] | None = None,
    max_backtrack: int = 16,
) -> list[CompletionApplyResult]:
    """Apply DONE markers to an already immutable source snapshot."""
    cfg = settings or LocalSettings()
    cfg.ensure()
    detections = detect_done_markers(snapshot.snapshot_path, pages=pages)
    detected = [item for item in detections if item.detected]
    if not detected:
        return []

    page_index = ensure_source_page_index(snapshot, cfg.cache_dir)
    pages_by_number = {item.page_number: item for item in page_index.pages}
    state = QuestionStateStore(cfg.state_db)
    results: list[CompletionApplyResult] = []

    for detection in detected:
        resolution = resolve_question_for_done_page(
            snapshot.snapshot_path,
            detection.page_number,
            ocr_engine=ocr_engine,
            temp_dir=cfg.temp_dir / "question_headers",
            max_backtrack=max_backtrack,
        )
        if not resolution.resolved or resolution.question is None or resolution.start_page is None:
            results.append(
                CompletionApplyResult(
                    subject=subject,
                    source_sha256=snapshot.sha256,
                    done_detection=detection,
                    resolution=resolution,
                )
            )
            continue

        stable_ids = [
            pages_by_number[page_number].stable_page_id
            for page_number in range(resolution.start_page, detection.page_number + 1)
        ]
        existing = state.get(subject, resolution.question)
        previous_status = existing.status if existing is not None else None

        if existing is None:
            existing = state.ensure_in_progress(
                subject,
                resolution.question,
                latest_source_sha256=snapshot.sha256,
                stable_page_ids=stable_ids,
            )

        state_updated = False
        if existing.status in {QuestionStatus.IN_PROGRESS, QuestionStatus.DONE_DETECTED}:
            updated = state.transition(
                subject,
                resolution.question,
                QuestionStatus.DONE_DETECTED,
                latest_source_sha256=snapshot.sha256,
                stable_page_ids=stable_ids,
            )
            state_updated = (
                previous_status != QuestionStatus.DONE_DETECTED
                or existing.latest_source_sha256 != snapshot.sha256
                or existing.stable_page_ids != stable_ids
            )
        else:
            updated = existing

        results.append(
            CompletionApplyResult(
                subject=subject,
                source_sha256=snapshot.sha256,
                done_detection=detection,
                resolution=resolution,
                previous_status=previous_status,
                current_status=updated.status,
                state_updated=state_updated,
            )
        )

    return results


def apply_done_markers(
    pdf: str | Path,
    *,
    subject: str,
    ocr_engine: OcrEngine,
    settings: LocalSettings | None = None,
    pages: list[int] | None = None,
    max_backtrack: int = 16,
) -> list[CompletionApplyResult]:
    """Detect DONE markers, resolve their question, and persist DONE_DETECTED."""
    cfg = settings or LocalSettings()
    cfg.ensure()
    snapshot = snapshot_source(pdf, settings=cfg)
    return apply_done_markers_to_snapshot(
        snapshot,
        subject=subject,
        ocr_engine=ocr_engine,
        settings=cfg,
        pages=pages,
        max_backtrack=max_backtrack,
    )
