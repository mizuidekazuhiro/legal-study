from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.automation.file_watcher import FileUpdateEvent, FileUpdateWatcher
from legal_study.automation.sync_stability import (
    SyncStabilityResult,
    mark_question_sync_stable_from_verified_hash,
    wait_for_sync_stable,
)
from legal_study.completion.question_resolution import (
    CompletionApplyResult,
    apply_done_markers_to_snapshot,
)
from legal_study.page_identity import (
    PageAlignment,
    SourcePageIndex,
    align_page_indexes,
    ensure_source_page_index,
)
from legal_study.pdf.ocr.base import OcrEngine
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source
from legal_study.state import QuestionStatus


class QuestionAutomationResult(BaseModel):
    question: str
    done_page: int
    status: QuestionStatus | None = None
    sync_state_updated: bool = False


class AutomationCycleResult(BaseModel):
    source_path: str
    reason: str
    stable: bool
    stable_sha256: str | None = None
    baseline_source_sha256: str | None = None
    current_source_sha256: str | None = None
    alignment_counts: dict[str, int] = Field(default_factory=dict)
    candidate_pages: list[int] = Field(default_factory=list)
    detected_done_pages: list[int] = Field(default_factory=list)
    questions: list[QuestionAutomationResult] = Field(default_factory=list)


def changed_current_pages(alignment: PageAlignment) -> list[int]:
    """Return current pages that contain actual content/markup changes.

    Pure movement is ignored because moving a page does not create a new DONE
    signal.
    """
    ignored = {"SAME", "MOVED"}
    return sorted(
        {
            int(record.current_page)
            for record in alignment.records
            if record.current_page is not None and record.classification not in ignored
        }
    )


def establish_stable_baseline(
    pdf: str | Path,
    *,
    settings: LocalSettings | None = None,
    interval_seconds: float = 5.0,
    required_equal_observations: int = 3,
    timeout_seconds: float = 90.0,
) -> tuple[SourcePageIndex | None, SyncStabilityResult]:
    """Create the watcher baseline only from a verified-stable source."""
    cfg = settings or LocalSettings()
    cfg.ensure()
    stable = wait_for_sync_stable(
        pdf,
        interval_seconds=interval_seconds,
        required_equal_observations=required_equal_observations,
        timeout_seconds=timeout_seconds,
    )
    if not stable.stable or stable.sha256 is None:
        return None, stable

    snapshot = snapshot_source(pdf, settings=cfg)
    if snapshot.sha256 != stable.sha256:
        stable.stable = False
        stable.reason = "SOURCE_CHANGED_AFTER_STABILITY_CHECK"
        return None, stable
    return ensure_source_page_index(snapshot, cfg.cache_dir), stable


def process_pdf_update(
    pdf: str | Path,
    *,
    subject: str,
    ocr_engine: OcrEngine,
    previous_index: SourcePageIndex,
    settings: LocalSettings | None = None,
    interval_seconds: float = 5.0,
    required_equal_observations: int = 3,
    timeout_seconds: float = 90.0,
    max_backtrack: int = 16,
) -> tuple[SourcePageIndex, AutomationCycleResult]:
    """Process one detected file update after it becomes stable."""
    cfg = settings or LocalSettings()
    cfg.ensure()
    source_path = str(Path(pdf).expanduser().resolve())

    stable = wait_for_sync_stable(
        pdf,
        interval_seconds=interval_seconds,
        required_equal_observations=required_equal_observations,
        timeout_seconds=timeout_seconds,
    )
    if not stable.stable or stable.sha256 is None:
        return previous_index, AutomationCycleResult(
            source_path=source_path,
            reason=stable.reason,
            stable=False,
            stable_sha256=stable.sha256,
            baseline_source_sha256=previous_index.source_sha256,
        )

    snapshot = snapshot_source(pdf, settings=cfg)
    if snapshot.sha256 != stable.sha256:
        return previous_index, AutomationCycleResult(
            source_path=source_path,
            reason="SOURCE_CHANGED_AFTER_STABILITY_CHECK",
            stable=False,
            stable_sha256=stable.sha256,
            baseline_source_sha256=previous_index.source_sha256,
            current_source_sha256=snapshot.sha256,
        )

    current_index = ensure_source_page_index(snapshot, cfg.cache_dir)
    alignment = align_page_indexes(previous_index, current_index)
    candidate_pages = changed_current_pages(alignment)

    result = AutomationCycleResult(
        source_path=source_path,
        reason="NO_RELEVANT_PAGE_CHANGES" if not candidate_pages else "PROCESSED_STABLE_UPDATE",
        stable=True,
        stable_sha256=stable.sha256,
        baseline_source_sha256=previous_index.source_sha256,
        current_source_sha256=current_index.source_sha256,
        alignment_counts=alignment.counts,
        candidate_pages=candidate_pages,
    )
    if not candidate_pages:
        return current_index, result

    completions: list[CompletionApplyResult] = apply_done_markers_to_snapshot(
        snapshot,
        subject=subject,
        ocr_engine=ocr_engine,
        settings=cfg,
        pages=candidate_pages,
        max_backtrack=max_backtrack,
    )
    result.detected_done_pages = sorted(
        item.done_detection.page_number for item in completions if item.done_detection.detected
    )

    for item in completions:
        question = item.resolution.question
        if question is None:
            continue
        sync_updated = False
        status = item.current_status
        if status == QuestionStatus.DONE_DETECTED:
            synced = mark_question_sync_stable_from_verified_hash(
                subject=subject,
                question=question,
                verified_sha256=stable.sha256,
                settings=cfg,
            )
            status = synced.current_status
            sync_updated = synced.state_updated
        result.questions.append(
            QuestionAutomationResult(
                question=question,
                done_page=item.done_detection.page_number,
                status=status,
                sync_state_updated=sync_updated,
            )
        )
    return current_index, result


def watch_pdf_updates(
    pdf: str | Path,
    *,
    subject: str,
    ocr_engine: OcrEngine,
    settings: LocalSettings | None = None,
    poll_interval_seconds: float = 60.0,
    stability_interval_seconds: float = 5.0,
    stability_equal_observations: int = 3,
    stability_timeout_seconds: float = 90.0,
    max_backtrack: int = 16,
) -> Iterator[AutomationCycleResult]:
    """Continuously watch one PDF and yield a result for each detected update."""
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")

    cfg = settings or LocalSettings()
    cfg.ensure()
    baseline, stable = establish_stable_baseline(
        pdf,
        settings=cfg,
        interval_seconds=stability_interval_seconds,
        required_equal_observations=stability_equal_observations,
        timeout_seconds=stability_timeout_seconds,
    )
    if baseline is None:
        yield AutomationCycleResult(
            source_path=str(Path(pdf).expanduser().resolve()),
            reason=stable.reason,
            stable=False,
            stable_sha256=stable.sha256,
        )
        return

    watcher = FileUpdateWatcher(pdf)
    while True:
        time.sleep(poll_interval_seconds)
        event: FileUpdateEvent | None = watcher.poll_once()
        if event is None:
            continue

        baseline, result = process_pdf_update(
            pdf,
            subject=subject,
            ocr_engine=ocr_engine,
            previous_index=baseline,
            settings=cfg,
            interval_seconds=stability_interval_seconds,
            required_equal_observations=stability_equal_observations,
            timeout_seconds=stability_timeout_seconds,
            max_backtrack=max_backtrack,
        )
        yield result
