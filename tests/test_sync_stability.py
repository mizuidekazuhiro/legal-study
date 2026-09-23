from pathlib import Path

from legal_study.automation.sync_stability import (
    FileObservation,
    mark_question_sync_stable,
    wait_for_sync_stable,
)
from legal_study.io_utils import file_sha256
from legal_study.settings import LocalSettings
from legal_study.state import QuestionStateStore, QuestionStatus


def _prepare_done_state(
    settings: LocalSettings,
    *,
    subject: str = "criminal",
    question: str = "22",
    source_sha256: str,
) -> None:
    store = QuestionStateStore(settings.state_db)
    store.ensure_in_progress(
        subject,
        question,
        latest_source_sha256=source_sha256,
        stable_page_ids=["page-a"],
    )
    store.transition(subject, question, QuestionStatus.DONE_DETECTED)


def test_wait_for_sync_stable_requires_metadata_and_matching_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"stable source")

    result = wait_for_sync_stable(
        source,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
        sleep_fn=lambda _: None,
    )

    assert result.stable is True
    assert result.reason == "STABLE_METADATA_AND_HASH"
    assert result.sha256 == file_sha256(source)
    assert len(result.observations) >= 3


def test_wait_for_sync_stable_times_out_before_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")

    counter = {"value": 0}

    def changing_observation(_: Path) -> FileObservation:
        counter["value"] += 1
        return FileObservation(size=100 + counter["value"], mtime_ns=counter["value"])

    result = wait_for_sync_stable(
        source,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=0,
        observation_fn=changing_observation,
        sleep_fn=lambda _: None,
    )

    assert result.stable is False
    assert result.reason == "TIMEOUT_BEFORE_STABLE"
    assert result.sha256 is None


def test_mark_question_sync_stable_advances_matching_done_source(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"stable source")
    settings = LocalSettings(home=tmp_path / "home")
    digest = file_sha256(source)
    _prepare_done_state(settings, source_sha256=digest)

    result = mark_question_sync_stable(
        source,
        subject="criminal",
        question="22",
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
        sleep_fn=lambda _: None,
    )

    assert result.stable is True
    assert result.state_updated is True
    assert result.previous_status == QuestionStatus.DONE_DETECTED
    assert result.current_status == QuestionStatus.SYNC_STABLE
    stored = QuestionStateStore(settings.state_db).get("criminal", "22")
    assert stored is not None
    assert stored.status == QuestionStatus.SYNC_STABLE


def test_changed_source_requires_done_recheck_and_does_not_advance(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"new stable source")
    settings = LocalSettings(home=tmp_path / "home")
    _prepare_done_state(settings, source_sha256="old-source-sha")

    result = mark_question_sync_stable(
        source,
        subject="criminal",
        question="22",
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
        sleep_fn=lambda _: None,
    )

    assert result.stable is True
    assert result.state_updated is False
    assert result.source_changed_requires_done_recheck is True
    assert result.reason == "SOURCE_CHANGED_AFTER_DONE_DETECTION"
    stored = QuestionStateStore(settings.state_db).get("criminal", "22")
    assert stored is not None
    assert stored.status == QuestionStatus.DONE_DETECTED


def test_non_done_question_is_never_advanced(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"stable source")
    settings = LocalSettings(home=tmp_path / "home")
    store = QuestionStateStore(settings.state_db)
    store.ensure_in_progress("criminal", "22", latest_source_sha256=file_sha256(source))

    result = mark_question_sync_stable(
        source,
        subject="criminal",
        question="22",
        settings=settings,
        interval_seconds=0,
        required_equal_observations=2,
        timeout_seconds=1,
        sleep_fn=lambda _: None,
    )

    assert result.stable is False
    assert result.reason == "QUESTION_NOT_DONE_DETECTED"
    assert result.current_status == QuestionStatus.IN_PROGRESS
