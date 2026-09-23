from pathlib import Path

import pytest

from legal_study.state import (
    InvalidQuestionTransition,
    QuestionStateStore,
    QuestionStatus,
)


def test_question_state_persists_across_store_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = QuestionStateStore(db)
    created = store.ensure_in_progress(
        "criminal",
        "17",
        latest_source_sha256="source-a",
        stable_page_ids=["page-a", "page-b"],
    )

    assert created.status == QuestionStatus.IN_PROGRESS

    store.transition("criminal", "17", QuestionStatus.DONE_DETECTED)

    reopened = QuestionStateStore(db)
    record = reopened.get("criminal", "17")
    assert record is not None
    assert record.status == QuestionStatus.DONE_DETECTED
    assert record.latest_source_sha256 == "source-a"
    assert record.stable_page_ids == ["page-a", "page-b"]


def test_question_state_rejects_skipping_required_states(tmp_path: Path) -> None:
    store = QuestionStateStore(tmp_path / "state.sqlite3")
    store.ensure_in_progress("criminal", "17")

    with pytest.raises(InvalidQuestionTransition, match="IN_PROGRESS -> PROCESSING"):
        store.transition("criminal", "17", QuestionStatus.PROCESSING)


def test_question_can_return_to_in_progress_before_approval(tmp_path: Path) -> None:
    store = QuestionStateStore(tmp_path / "state.sqlite3")
    store.ensure_in_progress("criminal", "17")
    store.transition("criminal", "17", QuestionStatus.DONE_DETECTED)
    store.transition("criminal", "17", QuestionStatus.SYNC_STABLE)
    store.transition("criminal", "17", QuestionStatus.PROCESSING)
    store.transition("criminal", "17", QuestionStatus.REVIEW_READY)

    record = store.transition(
        "criminal",
        "17",
        QuestionStatus.IN_PROGRESS,
        latest_source_sha256="source-b",
        stable_page_ids=["page-c"],
    )

    assert record.status == QuestionStatus.IN_PROGRESS
    assert record.latest_source_sha256 == "source-b"
    assert record.stable_page_ids == ["page-c"]


def test_approved_question_is_terminal(tmp_path: Path) -> None:
    store = QuestionStateStore(tmp_path / "state.sqlite3")
    store.ensure_in_progress("criminal", "17")
    for status in (
        QuestionStatus.DONE_DETECTED,
        QuestionStatus.SYNC_STABLE,
        QuestionStatus.PROCESSING,
        QuestionStatus.REVIEW_READY,
        QuestionStatus.APPROVED,
    ):
        store.transition("criminal", "17", status)

    with pytest.raises(InvalidQuestionTransition, match="APPROVED -> IN_PROGRESS"):
        store.transition("criminal", "17", QuestionStatus.IN_PROGRESS)


def test_draft_revision_is_persisted(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = QuestionStateStore(db)
    store.ensure_in_progress("criminal", "17")
    store.transition("criminal", "17", QuestionStatus.DONE_DETECTED)
    store.transition("criminal", "17", QuestionStatus.SYNC_STABLE)
    store.transition("criminal", "17", QuestionStatus.PROCESSING)
    record = store.transition(
        "criminal",
        "17",
        QuestionStatus.REVIEW_READY,
        draft_revision=2,
    )

    assert record.draft_revision == 2
    assert QuestionStateStore(db).get("criminal", "17").draft_revision == 2
