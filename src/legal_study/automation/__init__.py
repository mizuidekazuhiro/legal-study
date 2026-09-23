"""Automation primitives for local study workflow orchestration."""

from legal_study.automation.file_watcher import (
    FileUpdateEvent,
    FileUpdateWatcher,
    WatchedFileSignature,
    iter_file_updates,
)
from legal_study.automation.orchestrator import (
    AutomationCycleResult,
    QuestionAutomationResult,
    changed_current_pages,
    establish_stable_baseline,
    process_pdf_update,
    watch_pdf_updates,
)
from legal_study.automation.sync_stability import (
    FileObservation,
    SyncStabilityResult,
    mark_question_sync_stable,
    mark_question_sync_stable_from_verified_hash,
    wait_for_sync_stable,
)

__all__ = [
    "AutomationCycleResult",
    "FileObservation",
    "FileUpdateEvent",
    "FileUpdateWatcher",
    "QuestionAutomationResult",
    "SyncStabilityResult",
    "WatchedFileSignature",
    "changed_current_pages",
    "establish_stable_baseline",
    "iter_file_updates",
    "mark_question_sync_stable",
    "mark_question_sync_stable_from_verified_hash",
    "process_pdf_update",
    "wait_for_sync_stable",
    "watch_pdf_updates",
]
