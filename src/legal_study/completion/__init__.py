"""Completion-signal detection and question resolution for study workflows."""

from legal_study.completion.done_marker import (
    DoneDetection,
    detect_done_markers,
    save_done_stamp,
)
from legal_study.completion.question_resolution import (
    CompletionApplyResult,
    QuestionResolution,
    apply_done_markers,
    resolve_question_for_done_page,
)

__all__ = [
    "CompletionApplyResult",
    "DoneDetection",
    "QuestionResolution",
    "apply_done_markers",
    "detect_done_markers",
    "resolve_question_for_done_page",
    "save_done_stamp",
]
