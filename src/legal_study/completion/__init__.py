"""Completion-signal detection for study workflows."""

from legal_study.completion.done_marker import (
    DoneDetection,
    detect_done_markers,
    save_done_stamp,
)

__all__ = ["DoneDetection", "detect_done_markers", "save_done_stamp"]
