"""Automation primitives for local study workflow orchestration."""

from legal_study.automation.sync_stability import (
    FileObservation,
    SyncStabilityResult,
    mark_question_sync_stable,
    wait_for_sync_stable,
)

__all__ = [
    "FileObservation",
    "SyncStabilityResult",
    "mark_question_sync_stable",
    "wait_for_sync_stable",
]
