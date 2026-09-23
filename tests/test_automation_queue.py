from pathlib import Path

from legal_study.automation.queue import AutomationStateStore, WorkStatus


def test_queue_is_idempotent_for_same_question_and_source(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path / "state.sqlite3")

    store.enqueue(
        subject="criminal",
        question="22",
        source_sha256="sha-a",
        stable_page_ids=["p1", "p2"],
    )
    store.enqueue(
        subject="criminal",
        question="22",
        source_sha256="sha-a",
        stable_page_ids=["p1", "p2"],
    )

    assert first.id == second.id
    assert second.status == WorkStatus.PENDING
    assert len(store.list_pending()) == 1


def test_running_item_is_recovered_to_pending_after_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = AutomationStateStore(db)
    queued = store.enqueue(
        subject="criminal",
        question="22",
        source_sha256="sha-a",
        stable_page_ids=["p1"],
    )
    claimed = store.claim_next()

    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == WorkStatus.RUNNING
    assert claimed.attempt_count == 1

    reopened = AutomationStateStore(db)
    recovered = reopened.recover_interrupted()

    assert recovered == 1
    pending = reopened.list_pending()
    assert len(pending) == 1
    assert pending[0].id == queued.id
    assert pending[0].status == WorkStatus.PENDING
    assert pending[0].attempt_count == 1


def test_watch_baseline_persists_across_store_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"pdf")

    store = AutomationStateStore(db)
    store.set_watch_baseline("criminal", pdf, "sha-a")

    reopened = AutomationStateStore(db)
    state = reopened.get_watch_state("criminal", pdf)

    assert state is not None
    assert state.last_processed_sha256 == "sha-a"
    assert state.source_path == str(pdf.resolve())


def test_queue_complete_and_fail_are_persistent(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path / "state.sqlite3")
    first = store.enqueue(
        subject="criminal",
        question="22",
        source_sha256="sha-a",
        stable_page_ids=["p1"],
    )
    second = store.enqueue(
        subject="criminal",
        question="23",
        source_sha256="sha-b",
        stable_page_ids=["p2"],
    )

    claimed_first = store.claim_next()
    assert claimed_first is not None
    completed = store.mark_completed(claimed_first.id)
    assert completed.status == WorkStatus.COMPLETED

    claimed_second = store.claim_next()
    assert claimed_second is not None
    failed = store.mark_failed(claimed_second.id, "boom")
    assert failed.status == WorkStatus.FAILED
    assert failed.last_error == "boom"
    assert store.list_pending() == []
