from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path

import pymupdf
from pydantic import BaseModel, Field

from legal_study.completion.done_marker import DoneDetection, detect_done_markers
from legal_study.io_utils import atomic_write_json
from legal_study.page_identity import ensure_source_page_index
from legal_study.pdf.ocr.base import OcrEngine
from legal_study.settings import LocalSettings
from legal_study.source_store import SourceSnapshot, snapshot_source
from legal_study.state import QuestionStateStore, QuestionStatus

_QUESTION_HEADER = re.compile(r"第\s*([0-9]{1,3})\s*問")
_HEADER_LIKE = re.compile(r"第\s*[^\s問]{0,8}\s*問")
_AUXILIARY_ROLES = ("指針", "総括", "MEMO", "メモ", "答案例", "答案")
_PROBLEM_CUES = ("問題文", "設問", "次の", "以下", "罪責", "論ぜ", "答えよ")


class QuestionStartCandidate(BaseModel):
    page_number: int
    question: str | None = None
    decision: str
    reasons: list[str] = Field(default_factory=list)
    text: str
    confidence: float | None = None


class QuestionResolution(BaseModel):
    done_page: int
    resolved: bool
    question: str | None = None
    start_page: int | None = None
    scanned_pages: list[int] = Field(default_factory=list)
    header_text: str | None = None
    header_confidence: float | None = None
    reason: str | None = None
    candidates: list[QuestionStartCandidate] = Field(default_factory=list)


class CompletionApplyResult(BaseModel):
    subject: str
    source_sha256: str
    done_detection: DoneDetection
    resolution: QuestionResolution
    previous_status: QuestionStatus | None = None
    current_status: QuestionStatus | None = None
    state_updated: bool = False


def write_question_resolution_audit(
    *,
    settings: LocalSettings,
    snapshot: SourceSnapshot,
    resolution: QuestionResolution,
) -> Path:
    path = (
        settings.cache_dir
        / "completion_audit"
        / snapshot.sha256
        / f"done-page-{resolution.done_page:04d}.json"
    )
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "source_sha256": snapshot.sha256,
            "resolver_version": 2,
            "resolution": resolution.model_dump(mode="json"),
        },
    )
    return path


def _normalize_header_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _extract_question_number(text: str) -> str | None:
    match = _QUESTION_HEADER.fullmatch(_normalize_header_text(text))
    return match.group(1) if match else None


def _assess_question_start(
    *, page_number: int, text: str, confidence: float | None
) -> QuestionStartCandidate | None:
    normalized = _normalize_header_text(text)
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    header_candidates = _HEADER_LIKE.findall(normalized)
    if not header_candidates:
        return None
    if "目次" in normalized:
        return QuestionStartCandidate(
            page_number=page_number,
            decision="REJECT_TABLE_OF_CONTENTS",
            reasons=["table_of_contents"],
            text=text,
            confidence=confidence,
        )
    title_lines = [
        line
        for line in lines
        if re.fullmatch(r"第\s*[0-9]{1,3}\s*問(?:\s*[^\s]{1,8})?", line)
    ]
    if not title_lines:
        malformed_title = next(
            (line for line in lines if _HEADER_LIKE.fullmatch(line)),
            None,
        )
        if malformed_title is not None:
            return QuestionStartCandidate(
                page_number=page_number,
                decision="UNRESOLVED_AMBIGUOUS_HEADING",
                reasons=["ambiguous_or_malformed_heading"],
                text=text,
                confidence=confidence,
            )
        first = _QUESTION_HEADER.search(normalized)
        return QuestionStartCandidate(
            page_number=page_number,
            question=first.group(1) if first else None,
            decision="REJECT_CROSS_REFERENCE",
            reasons=["heading_is_not_a_page_title"],
            text=text,
            confidence=confidence,
        )
    questions = [
        _QUESTION_HEADER.search(line).group(1)  # type: ignore[union-attr]
        for line in title_lines
    ]
    header_line = title_lines[0]
    if None in questions or len(set(questions)) != 1:
        return QuestionStartCandidate(
            page_number=page_number,
            decision="UNRESOLVED_AMBIGUOUS_HEADING",
            reasons=["ambiguous_or_malformed_heading"],
            text=text,
            confidence=confidence,
        )
    question = questions[0]
    assert question is not None
    auxiliary_heading = any(role in header_line for role in _AUXILIARY_ROLES)
    if auxiliary_heading:
        return QuestionStartCandidate(
            page_number=page_number,
            question=question,
            decision="REJECT_AUXILIARY_HEADING",
            reasons=["auxiliary_heading_without_problem_text"],
            text=text,
            confidence=confidence,
        )
    has_problem_cue = any(cue in normalized for cue in _PROBLEM_CUES)
    reasons = ["exact_question_heading"]
    if re.search(rf"(?<!\d){re.escape(question)}\s*[-－]\s*1(?!\d)", normalized):
        reasons.append("booklet_part_1")
    if has_problem_cue:
        reasons.append("problem_or_question_text")
    return QuestionStartCandidate(
        page_number=page_number,
        question=question,
        decision="ACCEPT_PROBLEM_START",
        reasons=reasons,
        text=text,
        confidence=confidence,
    )


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
    candidates: list[QuestionStartCandidate] = []
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

            candidate = _assess_question_start(
                page_number=page_number,
                text=ocr.text,
                confidence=ocr.confidence,
            )
            if candidate is None:
                continue
            candidates.append(candidate)
            if candidate.decision == "UNRESOLVED_AMBIGUOUS_HEADING":
                return QuestionResolution(
                    done_page=done_page,
                    resolved=False,
                    scanned_pages=scanned_pages,
                    header_text=ocr.text,
                    header_confidence=ocr.confidence,
                    candidates=candidates,
                    reason="Ambiguous or malformed question heading; state was not changed.",
                )
            if candidate.decision != "ACCEPT_PROBLEM_START":
                continue
            question = candidate.question
            if question is not None:
                return QuestionResolution(
                    done_page=done_page,
                    resolved=True,
                    question=question,
                    start_page=page_number,
                    scanned_pages=scanned_pages,
                    header_text=ocr.text,
                    header_confidence=ocr.confidence,
                    candidates=candidates,
                )
    finally:
        document.close()

    return QuestionResolution(
        done_page=done_page,
        resolved=False,
        scanned_pages=scanned_pages,
        candidates=candidates,
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
    resolved_questions: dict[int, QuestionResolution] | None = None,
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
        resolution = (resolved_questions or {}).get(detection.page_number)
        if resolution is None:
            resolution = resolve_question_for_done_page(
                snapshot.snapshot_path,
                detection.page_number,
                ocr_engine=ocr_engine,
                temp_dir=cfg.temp_dir / "question_headers",
                max_backtrack=max_backtrack,
            )
        write_question_resolution_audit(
            settings=cfg,
            snapshot=snapshot,
            resolution=resolution,
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
