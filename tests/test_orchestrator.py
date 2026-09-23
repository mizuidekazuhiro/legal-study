from pathlib import Path

import pymupdf

from legal_study.automation.orchestrator import process_pdf_update, recover_watch_startup
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


def test_first_watch_startup_establishes_baseline_without_historical_replay(
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
    assert result.questions == []
    assert AutomationStateStore(settings.state_db).list_pending() == []
