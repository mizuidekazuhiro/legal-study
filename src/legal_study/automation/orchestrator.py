from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.automation.file_watcher import FileUpdateEvent, FileUpdateWatcher
from legal_study.automation.queue import AutomationStateStore, WorkStatus
from legal_study.automation.sync_stability import (
    SyncStabilityResult,
    mark_question_sync_stable_from_verified_hash,
    wait_for_sync_stable,
)
from legal_study.completion.done_marker import detect_embedded_done_markers
from legal_study.completion.question_resolution import (
    CompletionApplyResult,
    apply_done_markers_to_snapshot,
    resolve_question_for_done_page,
)
from legal_study.page_identity import (
    PageAlignment,
    SourcePageIndex,
    align_page_indexes,
    ensure_source_page_index,
    load_source_page_index,
)
from legal_study.pdf.ocr.base import OcrEngine
from legal_study.settings import LocalSettings
from legal_study.source_store import SourceSnapshot, snapshot_source
from legal_study.state import QuestionStateStore, QuestionStatus


class QuestionAutomationResult(BaseModel):
    question: str
    done_page: int
    status: QuestionStatus | None = None
    sync_state_updated: bool = False
    queue_item_id: int | None = None
    queue_status: WorkStatus | None = None


class AutomationCycleResult(BaseModel):
    source_path: str
    reason: str
    stable: bool
    stable_sha256: str | None = None
    baseline_source_sha256: str | None = None
    current_source_sha256: str | None = None
    recovered_work_items: int = 0
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


def _advance_and_queue_completions(
    completions: list[CompletionApplyResult],
    *,
    subject: str,
    verified_sha256: str,
    snapshot: SourceSnapshot,
    settings: LocalSettings,
) -> list[QuestionAutomationResult]:
    question_state = QuestionStateStore(settings.state_db)
    automation_state = AutomationStateStore(settings.state_db)
    results: list[QuestionAutomationResult] = []
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
                verified_sha256=verified_sha256,
                settings=settings,
            )
            status = synced.current_status
            sync_updated = synced.state_updated

        queue_item_id = None
        queue_status = None
        if status in {QuestionStatus.SYNC_STABLE, QuestionStatus.PROCESSING}:
            record = question_state.get(subject, question)
            if record is not None and record.latest_source_sha256 == verified_sha256:
                queued = automation_state.enqueue(
                    subject=subject,
                    question=question,
                    source_sha256=verified_sha256,
                    stable_page_ids=record.stable_page_ids,
                    source_snapshot=snapshot,
                )
                queue_item_id = queued.id
                queue_status = queued.status

        results.append(
            QuestionAutomationResult(
                question=question,
                done_page=item.done_detection.page_number,
                status=status,
                sync_state_updated=sync_updated,
                queue_item_id=queue_item_id,
                queue_status=queue_status,
            )
        )
    return results


def reconcile_unregistered_done(
    snapshot: SourceSnapshot,
    *,
    subject: str,
    ocr_engine: OcrEngine,
    settings: LocalSettings,
    allowed_questions: set[str] | None = None,
    include_revisions: bool = False,
    done_pages: list[int] | None = None,
    max_backtrack: int = 16,
) -> tuple[list[int], list[QuestionAutomationResult]]:
    """Queue DONE-stamped questions missing from the current-source workflow.

    Discovery uses embedded marker signatures only, so an unchanged PDF does not
    trigger a full render or OCR pass. Resolution OCR is limited to detected pages.
    """
    detections = detect_embedded_done_markers(
        snapshot.snapshot_path,
        pages=done_pages,
    )
    if not detections:
        return [], []
    if allowed_questions is not None:
        queue = AutomationStateStore(settings.state_db)
        completed = [
            queue.get_by_identity(subject, question, snapshot.sha256)
            for question in allowed_questions
        ]
        if completed and all(
            item is not None and item.status == WorkStatus.COMPLETED
            for item in completed
        ):
            return [item.page_number for item in detections], []
    selected_pages: list[int] = []
    resolutions = {}
    for detection in detections:
        resolution = resolve_question_for_done_page(
            snapshot.snapshot_path,
            detection.page_number,
            ocr_engine=ocr_engine,
            temp_dir=settings.temp_dir / "question_headers",
            max_backtrack=max_backtrack,
        )
        if (
            resolution.resolved
            and resolution.question is not None
            and (allowed_questions is None or resolution.question in allowed_questions)
        ):
            selected_pages.append(detection.page_number)
            resolutions[detection.page_number] = resolution
    if not selected_pages:
        return [item.page_number for item in detections], []

    completions = apply_done_markers_to_snapshot(
        snapshot,
        subject=subject,
        ocr_engine=ocr_engine,
        settings=settings,
        pages=selected_pages,
        max_backtrack=max_backtrack,
        resolved_questions=resolutions,
    )
    if include_revisions:
        page_index = ensure_source_page_index(snapshot, settings.cache_dir)
        pages_by_number = {item.page_number: item for item in page_index.pages}
        state = QuestionStateStore(settings.state_db)
        retry_pages: list[int] = []
        retry_questions: set[str] = set()
        for item in completions:
            resolution = item.resolution
            if resolution.question is None or resolution.start_page is None:
                continue
            desired_ids = [
                pages_by_number[page].stable_page_id
                for page in range(resolution.start_page, item.done_detection.page_number + 1)
            ]
            existing = state.get(subject, resolution.question)
            if (
                existing is not None
                and existing.stable_page_ids != desired_ids
                and existing.status in {
                    QuestionStatus.PROCESSING,
                    QuestionStatus.REVIEW_READY,
                    QuestionStatus.DONE_DETECTED,
                    QuestionStatus.SYNC_STABLE,
                }
            ):
                state.transition(
                    subject,
                    resolution.question,
                    QuestionStatus.IN_PROGRESS,
                    latest_source_sha256=snapshot.sha256,
                    stable_page_ids=desired_ids,
                )
                retry_pages.append(item.done_detection.page_number)
                retry_questions.add(resolution.question)
        if retry_pages:
            retried = apply_done_markers_to_snapshot(
                snapshot,
                subject=subject,
                ocr_engine=ocr_engine,
                settings=settings,
                pages=retry_pages,
                max_backtrack=max_backtrack,
                resolved_questions={page: resolutions[page] for page in retry_pages},
            )
            completions = [
                item
                for item in completions
                if item.resolution.question not in retry_questions
            ] + retried
            completions.sort(key=lambda item: item.done_detection.page_number)
    questions = _advance_and_queue_completions(
        completions,
        subject=subject,
        verified_sha256=snapshot.sha256,
        snapshot=snapshot,
        settings=settings,
    )
    return [item.page_number for item in detections], questions


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

    result.questions = _advance_and_queue_completions(
        completions,
        subject=subject,
        verified_sha256=stable.sha256,
        snapshot=snapshot,
        settings=cfg,
    )
    return current_index, result


def recover_watch_startup(
    pdf: str | Path,
    *,
    subject: str,
    ocr_engine: OcrEngine,
    settings: LocalSettings | None = None,
    stability_interval_seconds: float = 5.0,
    stability_equal_observations: int = 3,
    stability_timeout_seconds: float = 90.0,
    max_backtrack: int = 16,
) -> tuple[SourcePageIndex | None, AutomationCycleResult]:
    """Restore interrupted work and process PDF changes missed while Windows was off."""
    cfg = settings or LocalSettings()
    cfg.ensure()
    source_path = str(Path(pdf).expanduser().resolve())
    automation_state = AutomationStateStore(cfg.state_db)
    recovered = automation_state.recover_interrupted()

    current_index, stable = establish_stable_baseline(
        pdf,
        settings=cfg,
        interval_seconds=stability_interval_seconds,
        required_equal_observations=stability_equal_observations,
        timeout_seconds=stability_timeout_seconds,
    )
    if current_index is None:
        return None, AutomationCycleResult(
            source_path=source_path,
            reason=stable.reason,
            stable=False,
            stable_sha256=stable.sha256,
            recovered_work_items=recovered,
        )

    watch_state = automation_state.get_watch_state(subject, pdf)
    if watch_state is None:
        automation_state.set_watch_baseline(
            subject,
            pdf,
            current_index.source_sha256,
        )
        result = AutomationCycleResult(
            source_path=source_path,
            reason="INITIAL_BASELINE_ESTABLISHED",
            stable=True,
            stable_sha256=current_index.source_sha256,
            current_source_sha256=current_index.source_sha256,
            recovered_work_items=recovered,
        )
        snapshot = snapshot_source(pdf, settings=cfg)
        result.detected_done_pages, result.questions = reconcile_unregistered_done(
            snapshot,
            subject=subject,
            ocr_engine=ocr_engine,
            settings=cfg,
            max_backtrack=max_backtrack,
        )
        return current_index, result

    if watch_state.last_processed_sha256 == current_index.source_sha256:
        result = AutomationCycleResult(
            source_path=source_path,
            reason="PERSISTED_BASELINE_UNCHANGED",
            stable=True,
            stable_sha256=current_index.source_sha256,
            baseline_source_sha256=watch_state.last_processed_sha256,
            current_source_sha256=current_index.source_sha256,
            recovered_work_items=recovered,
        )
        snapshot = snapshot_source(pdf, settings=cfg)
        result.detected_done_pages, result.questions = reconcile_unregistered_done(
            snapshot,
            subject=subject,
            ocr_engine=ocr_engine,
            settings=cfg,
            max_backtrack=max_backtrack,
        )
        return current_index, result

    try:
        previous_index = load_source_page_index(
            cfg.cache_dir,
            watch_state.last_processed_sha256,
        )
    except FileNotFoundError:
        return None, AutomationCycleResult(
            source_path=source_path,
            reason="PERSISTED_BASELINE_INDEX_MISSING",
            stable=True,
            stable_sha256=current_index.source_sha256,
            baseline_source_sha256=watch_state.last_processed_sha256,
            current_source_sha256=current_index.source_sha256,
            recovered_work_items=recovered,
        )

    processed_index, result = process_pdf_update(
        pdf,
        subject=subject,
        ocr_engine=ocr_engine,
        previous_index=previous_index,
        settings=cfg,
        interval_seconds=stability_interval_seconds,
        required_equal_observations=stability_equal_observations,
        timeout_seconds=stability_timeout_seconds,
        max_backtrack=max_backtrack,
    )
    result.recovered_work_items = recovered
    if result.stable and result.current_source_sha256 == processed_index.source_sha256:
        automation_state.set_watch_baseline(
            subject,
            pdf,
            processed_index.source_sha256,
        )
    return processed_index, result


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
    """Continuously watch one PDF, including changes missed during downtime."""
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")

    cfg = settings or LocalSettings()
    cfg.ensure()
    baseline, startup = recover_watch_startup(
        pdf,
        subject=subject,
        ocr_engine=ocr_engine,
        settings=cfg,
        stability_interval_seconds=stability_interval_seconds,
        stability_equal_observations=stability_equal_observations,
        stability_timeout_seconds=stability_timeout_seconds,
        max_backtrack=max_backtrack,
    )
    yield startup
    if baseline is None:
        return

    automation_state = AutomationStateStore(cfg.state_db)
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
        if result.stable and result.current_source_sha256 == baseline.source_sha256:
            automation_state.set_watch_baseline(
                subject,
                pdf,
                baseline.source_sha256,
            )
        yield result
