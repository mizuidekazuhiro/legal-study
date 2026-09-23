import json
from pathlib import Path

import pymupdf

from legal_study.automation.queue import AutomationStateStore, WorkStatus
from legal_study.automation.worker import process_next_work_item
from legal_study.page_identity import ensure_source_page_index
from legal_study.problem_packet import handoff_markdown_filename
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source
from legal_study.state import QuestionStateStore, QuestionStatus


class FakePipeline:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, list[int]]] = []

    def input_config(self) -> dict[str, object]:
        return {"pipeline": "fake-worker-test"}

    def run(self, source, prepared, *, pages=None):
        page_list = list(pages or [])
        self.calls.append((source.sha256, page_list))
        if self.fail:
            raise RuntimeError("pipeline boom")
        (prepared.output_dir / "fake-complete.txt").write_text(
            "ok",
            encoding="utf-8",
        )


class PacketReadyPipeline(FakePipeline):
    def __init__(self, *, valid: bool = True) -> None:
        super().__init__()
        self.valid = valid

    def run(self, source, prepared, *, pages=None):
        super().run(source, prepared, pages=pages)
        root = prepared.output_dir
        selected = list(pages or [])
        canonical = {
            "source": {"sha256": source.sha256, "requested_pages": selected},
            "logical_markers": [],
        }
        (root / "canonical_source.json").write_text(
            json.dumps(canonical), encoding="utf-8"
        )
        (root / "problem_validation.json").write_text(
            json.dumps({"valid": self.valid}), encoding="utf-8"
        )
        handoff = handoff_markdown_filename(
            prepared.manifest.subject, prepared.manifest.question
        )
        (root / handoff).write_text("# Page Reading Pack\n", encoding="utf-8")
        review = root / "handoff_review"
        review.mkdir()
        for page in selected:
            (review / f"page-{page:04d}-review.png").write_bytes(b"png")


def _write_pdf(path: Path) -> None:
    document = pymupdf.open()
    for page_number in range(1, 4):
        page = document.new_page(width=400, height=550)
        page.insert_text((40, 80), f"page {page_number}")
    document.save(path)
    document.close()


def _queue_question(
    tmp_path: Path,
    *,
    question: str = "22",
) -> tuple[LocalSettings, AutomationStateStore, QuestionStateStore, str]:
    source = tmp_path / "source.pdf"
    _write_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    index = ensure_source_page_index(snapshot, settings.cache_dir)
    stable_ids = [index.pages[0].stable_page_id, index.pages[1].stable_page_id]

    questions = QuestionStateStore(settings.state_db)
    questions.ensure_in_progress(
        "criminal",
        question,
        latest_source_sha256=snapshot.sha256,
        stable_page_ids=stable_ids,
    )
    questions.transition("criminal", question, QuestionStatus.DONE_DETECTED)
    questions.transition("criminal", question, QuestionStatus.SYNC_STABLE)

    queue = AutomationStateStore(settings.state_db)
    queue.enqueue(
        subject="criminal",
        question=question,
        source_sha256=snapshot.sha256,
        stable_page_ids=stable_ids,
        source_snapshot=snapshot,
    )
    return settings, queue, questions, snapshot.sha256


def test_worker_processes_exact_stable_pages_and_persists_run(tmp_path: Path) -> None:
    settings, queue, questions, source_sha = _queue_question(tmp_path)
    pipeline = FakePipeline()

    result = process_next_work_item(pipeline=pipeline, settings=settings)

    assert result.claimed is True
    assert result.reason == "INGEST_COMPLETED"
    assert result.pages == [1, 2]
    assert result.queue_status == WorkStatus.COMPLETED
    assert result.question_status == QuestionStatus.PROCESSING
    assert result.source_sha256 == source_sha
    assert result.output_dir is not None
    assert result.run_id is not None
    assert pipeline.calls == [(source_sha, [1, 2])]

    stored_question = questions.get("criminal", "22")
    assert stored_question is not None
    assert stored_question.status == QuestionStatus.PROCESSING

    item = queue.get_work_item(result.work_item_id or -1)
    assert item is not None
    assert item.status == WorkStatus.COMPLETED
    assert item.output_dir == result.output_dir
    assert item.run_id == result.run_id
    assert result.chat_packet_status is None


def test_worker_failure_returns_question_to_sync_stable(tmp_path: Path) -> None:
    settings, queue, questions, _source_sha = _queue_question(tmp_path)
    pipeline = FakePipeline(fail=True)

    result = process_next_work_item(pipeline=pipeline, settings=settings)

    assert result.claimed is True
    assert result.reason == "INGEST_FAILED"
    assert result.queue_status == WorkStatus.FAILED
    assert result.question_status == QuestionStatus.SYNC_STABLE
    assert "pipeline boom" in (result.error or "")

    stored_question = questions.get("criminal", "22")
    assert stored_question is not None
    assert stored_question.status == QuestionStatus.SYNC_STABLE

    item = queue.get_work_item(result.work_item_id or -1)
    assert item is not None
    assert item.status == WorkStatus.FAILED


def test_worker_resumes_question_left_processing_after_crash(tmp_path: Path) -> None:
    settings, queue, questions, source_sha = _queue_question(tmp_path)
    questions.transition("criminal", "22", QuestionStatus.PROCESSING)

    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.status == WorkStatus.RUNNING
    assert queue.recover_interrupted() == 1

    pipeline = FakePipeline()
    result = process_next_work_item(pipeline=pipeline, settings=settings)

    assert result.reason == "INGEST_COMPLETED"
    assert result.queue_status == WorkStatus.COMPLETED
    assert result.question_status == QuestionStatus.PROCESSING
    assert pipeline.calls == [(source_sha, [1, 2])]


def test_worker_rejects_legacy_item_without_source_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    index = ensure_source_page_index(snapshot, settings.cache_dir)
    stable_ids = [index.pages[0].stable_page_id]

    questions = QuestionStateStore(settings.state_db)
    questions.ensure_in_progress(
        "criminal",
        "22",
        latest_source_sha256=snapshot.sha256,
        stable_page_ids=stable_ids,
    )
    questions.transition("criminal", "22", QuestionStatus.DONE_DETECTED)
    questions.transition("criminal", "22", QuestionStatus.SYNC_STABLE)

    queue = AutomationStateStore(settings.state_db)
    queue.enqueue(
        subject="criminal",
        question="22",
        source_sha256=snapshot.sha256,
        stable_page_ids=stable_ids,
    )

    result = process_next_work_item(
        pipeline=FakePipeline(),
        settings=settings,
    )

    assert result.reason == "INGEST_FAILED"
    assert result.queue_status == WorkStatus.FAILED
    assert "missing immutable source snapshot" in (result.error or "")
    stored = questions.get("criminal", "22")
    assert stored is not None
    assert stored.status == QuestionStatus.SYNC_STABLE


def test_worker_completes_only_after_packet_is_published(tmp_path: Path) -> None:
    settings, queue, _questions, _source_sha = _queue_question(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    pending = bridge / "00_pending"
    pending.mkdir(parents=True)

    result = process_next_work_item(
        pipeline=PacketReadyPipeline(), settings=settings, bridge_root=bridge
    )

    assert result.queue_status == WorkStatus.COMPLETED
    assert result.chat_packet_status == "PUBLISHED"
    assert len(list(pending.glob("*.chat_packet.zip"))) == 1
    assert queue.get_work_item(result.work_item_id or -1).status == WorkStatus.COMPLETED


def test_worker_does_not_complete_when_publish_fails(tmp_path: Path) -> None:
    settings, queue, questions, _source_sha = _queue_question(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    bridge.mkdir()

    result = process_next_work_item(
        pipeline=PacketReadyPipeline(), settings=settings, bridge_root=bridge
    )

    assert result.queue_status == WorkStatus.FAILED
    assert result.chat_packet_status is None
    assert queue.get_work_item(result.work_item_id or -1).status == WorkStatus.FAILED
    assert questions.get("criminal", "22").status == QuestionStatus.SYNC_STABLE
    assert not (bridge / "00_pending").exists()


def test_worker_does_not_publish_invalid_run(tmp_path: Path) -> None:
    settings, queue, _questions, _source_sha = _queue_question(tmp_path)
    bridge = tmp_path / "LegalStudy_ChatBridge"
    pending = bridge / "00_pending"
    pending.mkdir(parents=True)

    result = process_next_work_item(
        pipeline=PacketReadyPipeline(valid=False),
        settings=settings,
        bridge_root=bridge,
    )

    assert result.queue_status == WorkStatus.FAILED
    assert queue.get_work_item(result.work_item_id or -1).status == WorkStatus.FAILED
    assert list(pending.iterdir()) == []
