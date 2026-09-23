"""Automation primitives for local study workflow orchestration."""

from legal_study.automation.file_watcher import (
    FileUpdateEvent,
    FileUpdateWatcher,
    WatchedFileSignature,
    iter_file_updates,
)
from legal_study.automation.sync_stability import (
    FileObservation,
    SyncStabilityResult,
    mark_question_sync_stable,
    wait_for_sync_stable,
)

__all__ = [
    "FileObservation",
    "FileUpdateEvent",
    "FileUpdateWatcher",
    "SyncStabilityResult",
    "WatchedFileSignature",
    "iter_file_updates",
    "mark_question_sync_stable",
    "wait_for_sync_stable",
]
