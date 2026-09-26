from pathlib import Path

import pymupdf
import pytest

from legal_study.automation.orchestrator import (
    process_pdf_update,
    reconcile_unregistered_done,
    recover_watch_startup,
)
from legal_study.automation.queue import AutomationStateStore, WorkStatus
from legal_study.completion.done_marker import done_stamp_png_bytes
from legal_study.page_identity import ensure_source_page_index
from legal_study.pdf.ocr.base import OcrLine, OcrResult
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source
from legal_study.state import QuestionStateStore, QuestionStatus


class HeaderOcr:
    name = "fake-header"

    def recognize(self, image_path: Path) -> OcrResult:
        page_number = int(image_path.name.split("-p", 1)[1].split("-", 1)[0])
        text = "第22問" if page_number == 2 else "ordinary page"
        return OcrResult(
            engine=self.name,
            text=text,
            confidence=0.99,
            lines=[OcrLine(text=text, confidence=0.99)],
        )


def _write_pdf(path: Path, *, done: bool = False, changed_text: bool = False) -> None:
    document = pymupdf.open()
    for page_number in range(1, 5):
        page = document.new_page(width=400, height=550)
        text = f"page {page_number} ordinary content"
        if page_number == 2:
            text = "question start"
        if page_number == 3 and changed_text:
            text += " updated"
        page.insert_text((40, 80), text)
        if page_number == 3 and done:
            page.insert_image(
                pymupdf.Rect(250, 470, 370, 514),
                stream=done_stamp_png_bytes(),
            )
    document.save(path)
    document.close()


def test_process_pdf_update_advances_new_done_to_sync_stable(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_pdf(source)

    baseline_snapshot = snapshot_source(source, settings=settings)
    baseline = ensure_source_page_index(baseline_snapshot, settings.cache_dir)

    _write_pdf(source, done=True)

    current, result = process_pdf_update(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        previous_index=baseline,
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
    )

    assert current.source_sha256 != baseline.source_sha256
    assert result.stable is True
    assert result.reason == "PROCESSED_STABLE_UPDATE"
    assert 3 in result.candidate_pages
    assert result.detected_done_pages == [3]
    assert len(result.questions) == 1
    assert result.questions[0].question == "22"
    assert result.questions[0].status == QuestionStatus.SYNC_STABLE
    assert result.questions[0].sync_state_updated is True

    stored = QuestionStateStore(settings.state_db).get("criminal", "22")
    assert stored is not None
    assert stored.status == QuestionStatus.SYNC_STABLE
    assert stored.latest_source_sha256 == current.source_sha256


def test_process_pdf_update_without_done_only_advances_baseline(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_pdf(source)

    baseline_snapshot = snapshot_source(source, settings=settings)
    baseline = ensure_source_page_index(baseline_snapshot, settings.cache_dir)

    _write_pdf(source, changed_text=True)

    current, result = process_pdf_update(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        previous_index=baseline,
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
    )

    assert current.source_sha256 != baseline.source_sha256
    assert result.stable is True
    assert result.candidate_pages
    assert result.detected_done_pages == []
    assert result.questions == []
    assert QuestionStateStore(settings.state_db).get("criminal", "22") is None



def test_process_pdf_update_enqueues_sync_stable_question(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_pdf(source)

    baseline_snapshot = snapshot_source(source, settings=settings)
    baseline = ensure_source_page_index(baseline_snapshot, settings.cache_dir)

    _write_pdf(source, done=True)

    _current, result = process_pdf_update(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        previous_index=baseline,
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
    )

    assert result.questions[0].queue_status == WorkStatus.PENDING
    queue = AutomationStateStore(settings.state_db)
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].question == "22"
    assert pending[0].source_sha256 == result.current_source_sha256


def test_restart_recovery_processes_pdf_changed_while_watcher_was_off(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_pdf(source)

    baseline_snapshot = snapshot_source(source, settings=settings)
    baseline = ensure_source_page_index(baseline_snapshot, settings.cache_dir)
    automation = AutomationStateStore(settings.state_db)
    automation.set_watch_baseline("criminal", source, baseline.source_sha256)

    _write_pdf(source, done=True)

    current, result = recover_watch_startup(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        settings=settings,
        stability_interval_seconds=0,
        stability_equal_observations=2,
        stability_timeout_seconds=1,
    )

    assert current is not None
    assert current.source_sha256 != baseline.source_sha256
    assert result.reason == "PROCESSED_STABLE_UPDATE"
    assert result.detected_done_pages == [3]
    assert result.questions[0].question == "22"
    assert result.questions[0].status == QuestionStatus.SYNC_STABLE
    assert result.questions[0].queue_status == WorkStatus.PENDING

    watch_state = automation.get_watch_state("criminal", source)
    assert watch_state is not None
    assert watch_state.last_processed_sha256 == current.source_sha256


def test_restart_recovery_returns_running_queue_item_to_pending(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_pdf(source)

    snapshot = snapshot_source(source, settings=settings)
    baseline = ensure_source_page_index(snapshot, settings.cache_dir)
    automation = AutomationStateStore(settings.state_db)
    automation.set_watch_baseline("criminal", source, baseline.source_sha256)
    automation.enqueue(
        subject="criminal",
        question="21",
        source_sha256="old-sha",
        stable_page_ids=["p1"],
    )
    claimed = automation.claim_next()
    assert claimed is not None
    assert claimed.status == WorkStatus.RUNNING

    current, result = recover_watch_startup(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        settings=settings,
        stability_interval_seconds=0,
        stability_equal_observations=2,
        stability_timeout_seconds=1,
    )

    assert current is not None
    assert result.reason == "PERSISTED_BASELINE_UNCHANGED"
    assert result.recovered_work_items == 1
    pending = automation.list_pending()
    assert len(pending) == 1
    assert pending[0].question == "21"


def test_first_watch_startup_reconciles_unregistered_done_without_full_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_pdf(source, done=True)

    current, result = recover_watch_startup(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        settings=settings,
        stability_interval_seconds=0,
        stability_equal_observations=2,
        stability_timeout_seconds=1,
    )

    assert current is not None
    assert result.reason == "INITIAL_BASELINE_ESTABLISHED"
    assert [item.question for item in result.questions] == ["22"]
    pending = AutomationStateStore(settings.state_db).list_pending()
    assert len(pending) == 1
    assert pending[0].question == "22"

    _again, repeated = recover_watch_startup(
        source,
        subject="criminal",
        ocr_engine=HeaderOcr(),
        settings=settings,
        stability_interval_seconds=0,
        stability_equal_observations=2,
        stability_timeout_seconds=1,
    )

    assert repeated.reason == "PERSISTED_BASELINE_UNCHANGED"
    assert repeated.questions[0].queue_item_id == pending[0].id
    assert len(AutomationStateStore(settings.state_db).list_pending()) == 1



class MultiDoneHeaderOcr:
    name = "fake-multi-header"

    def recognize(self, image_path: Path) -> OcrResult:
        page_number = int(image_path.name.split("-p", 1)[1].split("-", 1)[0])
        if page_number == 2:
            text = "第12問"
        elif page_number == 4:
            text = "第20問"
        else:
            text = "ordinary page"
        return OcrResult(
            engine=self.name,
            text=text,
            confidence=0.99,
            lines=[OcrLine(text=text, confidence=0.99)],
        )


def _write_multi_done_pdf(path: Path, *, done_pages: set[int]) -> None:
    document = pymupdf.open()
    for page_number in range(1, 7):
        page = document.new_page(width=400, height=550)
        page.insert_text((40, 80), f"page {page_number} ordinary content")
        if page_number in done_pages:
            page.insert_image(
                pymupdf.Rect(250, 470, 370, 514),
                stream=done_stamp_png_bytes(),
            )
    document.save(path)
    document.close()


def test_persistent_old_done_stamp_does_not_retrigger_when_new_done_is_added(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")

    # Day 1: question 12 is already complete and its DONE stamp remains in the PDF.
    _write_multi_done_pdf(source, done_pages={3})
    baseline_snapshot = snapshot_source(source, settings=settings)
    baseline = ensure_source_page_index(baseline_snapshot, settings.cache_dir)

    # Day 2: question 20 is completed. The old question-12 stamp is intentionally
    # left in place, so the PDF now contains both DONE stamps.
    _write_multi_done_pdf(source, done_pages={3, 5})

    _current, result = process_pdf_update(
        source,
        subject="criminal",
        ocr_engine=MultiDoneHeaderOcr(),
        previous_index=baseline,
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
    )

    assert 3 not in result.candidate_pages
    assert 5 in result.candidate_pages
    assert result.detected_done_pages == [5]
    assert [item.question for item in result.questions] == ["20"]


def test_scoped_reconciliation_does_not_register_other_done_questions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_multi_done_pdf(source, done_pages={3, 5})
    snapshot = snapshot_source(source, settings=settings)

    _pages, questions = reconcile_unregistered_done(
        snapshot,
        subject="criminal",
        ocr_engine=MultiDoneHeaderOcr(),
        settings=settings,
        allowed_questions={"20"},
    )

    assert [item.question for item in questions] == ["20"]
    state = QuestionStateStore(settings.state_db)
    assert state.get("criminal", "12") is None
    assert state.get("criminal", "20") is not None


class SequenceHeaderOcr:
    name = "fake-sequence-header"

    def __init__(self, overrides: dict[int, str] | None = None) -> None:
        self.overrides = overrides or {}

    def recognize(self, image_path: Path) -> OcrResult:
        page_number = int(image_path.name.split("-p", 1)[1].split("-", 1)[0])
        question_number = 12 + (page_number - 1) // 2
        text = f"第{question_number}問" if page_number % 2 else "ordinary page"
        text = self.overrides.get(page_number, text)
        return OcrResult(
            engine=self.name,
            text=text,
            confidence=0.99,
            lines=[OcrLine(text=text, confidence=0.99)],
        )


def _write_sequence_pdf(path: Path, *, done_questions: set[int]) -> None:
    document = pymupdf.open()
    for question in range(12, 17):
        for part in ("start", "done"):
            page = document.new_page(width=400, height=550)
            page.insert_text((40, 80), f"question {question} {part}")
            if part == "done" and question in done_questions:
                page.insert_image(
                    pymupdf.Rect(250, 470, 370, 514),
                    stream=done_stamp_png_bytes(),
                )
    document.save(path)
    document.close()


@pytest.mark.parametrize(
    ("previous_done", "current_done", "new_questions"),
    [
        ({12}, {12, 13}, [13]),
        ({14}, {14, 16}, [16]),
        (set(), {13, 14, 16}, [13, 14, 16]),
    ],
)
def test_only_new_individual_done_questions_are_queued_with_exact_pages(
    tmp_path: Path,
    previous_done: set[int],
    current_done: set[int],
    new_questions: list[int],
) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_sequence_pdf(source, done_questions=previous_done)
    previous_snapshot = snapshot_source(source, settings=settings)
    previous_index = ensure_source_page_index(previous_snapshot, settings.cache_dir)

    _write_sequence_pdf(source, done_questions=current_done)
    current_index, result = process_pdf_update(
        source,
        subject="criminal",
        ocr_engine=SequenceHeaderOcr(),
        previous_index=previous_index,
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
    )

    expected_done_pages = [2 * (question - 11) for question in new_questions]
    assert result.detected_done_pages == expected_done_pages
    assert [int(item.question) for item in result.questions] == new_questions
    queue = AutomationStateStore(settings.state_db)
    pending = queue.list_pending()
    assert [int(item.question) for item in pending] == new_questions
    state = QuestionStateStore(settings.state_db)
    assert state.get("criminal", "15") is None
    for item in pending:
        assert state.get("criminal", item.question).status == QuestionStatus.SYNC_STABLE
        done_page = 2 * (int(item.question) - 11)
        expected_ids = [
            current_index.pages[page_number - 1].stable_page_id
            for page_number in range(done_page - 1, done_page + 1)
        ]
        assert item.stable_page_ids == expected_ids

    if new_questions == [13, 14, 16]:
        _same_index, repeated = process_pdf_update(
            source,
            subject="criminal",
            ocr_engine=SequenceHeaderOcr(),
            previous_index=current_index,
            settings=settings,
            interval_seconds=0,
            required_equal_observations=2,
            timeout_seconds=1,
        )
        assert repeated.candidate_pages == []
        assert [int(item.question) for item in queue.list_pending()] == new_questions


@pytest.mark.parametrize("bad_header", ["ordinary page", "第I6問"])
def test_unresolved_done_never_creates_state_or_queue(
    tmp_path: Path, bad_header: str
) -> None:
    source = tmp_path / "source.pdf"
    settings = LocalSettings(home=tmp_path / "home")
    _write_sequence_pdf(source, done_questions=set())
    previous_snapshot = snapshot_source(source, settings=settings)
    previous_index = ensure_source_page_index(previous_snapshot, settings.cache_dir)
    _write_sequence_pdf(source, done_questions={16})

    _current_index, result = process_pdf_update(
        source,
        subject="criminal",
        ocr_engine=SequenceHeaderOcr({9: bad_header, 7: "ordinary page"}),
        previous_index=previous_index,
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
        max_backtrack=2,
    )

    assert result.detected_done_pages == [10]
    assert result.questions == []
    assert AutomationStateStore(settings.state_db).list_pending() == []
    assert QuestionStateStore(settings.state_db).get("criminal", "16") is None
