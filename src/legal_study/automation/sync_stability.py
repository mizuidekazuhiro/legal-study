from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.io_utils import file_sha256
from legal_study.settings import LocalSettings
from legal_study.state import QuestionStateStore, QuestionStatus


class FileObservation(BaseModel):
    size: int
    mtime_ns: int


class SyncStabilityResult(BaseModel):
    stable: bool
    path: str
    observations: list[FileObservation] = Field(default_factory=list)
    sha256: str | None = None
    reason: str
    source_changed_requires_done_recheck: bool = False
    previous_status: QuestionStatus | None = None
    current_status: QuestionStatus | None = None
    state_updated: bool = False


ObservationFn = Callable[[Path], FileObservation]
SleepFn = Callable[[float], None]
HashFn = Callable[[Path], str]


def observe_file(path: Path) -> FileObservation:
    stat = path.stat()
    return FileObservation(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def wait_for_sync_stable(
    path: str | Path,
    *,
    interval_seconds: float = 5.0,
    required_equal_observations: int = 3,
    timeout_seconds: float = 90.0,
    observation_fn: ObservationFn = observe_file,
    sleep_fn: SleepFn = time.sleep,
    hash_fn: HashFn = file_sha256,
) -> SyncStabilityResult:
    """Wait until metadata is repeatedly stable, then verify the content hash twice.

    Metadata stability is a cheap gate. A candidate is only accepted after two
    SHA-256 reads separated by one more interval with unchanged size and mtime.
    """
    target = Path(path).expanduser().resolve()
    if required_equal_observations < 2:
        raise ValueError("required_equal_observations must be >= 2")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be >= 0")
    if not target.is_file():
        return SyncStabilityResult(
            stable=False,
            path=str(target),
            reason="SOURCE_MISSING",
        )

    started = time.monotonic()
    observations: list[FileObservation] = []
    equal_count = 0
    previous: FileObservation | None = None

    while True:
        current = observation_fn(target)
        observations.append(current)
        if previous is not None and current == previous:
            equal_count += 1
        else:
            equal_count = 1
        previous = current

        if equal_count >= required_equal_observations:
            first_hash = hash_fn(target)
            before_verify = observation_fn(target)
            if before_verify != current:
                observations.append(before_verify)
                previous = before_verify
                equal_count = 1
            else:
                sleep_fn(interval_seconds)
                after_verify = observation_fn(target)
                observations.append(after_verify)
                if after_verify == before_verify:
                    second_hash = hash_fn(target)
                    final_observation = observation_fn(target)
                    if final_observation != after_verify:
                        observations.append(final_observation)
                        previous = final_observation
                        equal_count = 1
                    elif second_hash == first_hash:
                        return SyncStabilityResult(
                            stable=True,
                            path=str(target),
                            observations=observations,
                            sha256=second_hash,
                            reason="STABLE_METADATA_AND_HASH",
                        )
                    else:
                        previous = final_observation
                        equal_count = 1
                else:
                    previous = after_verify
                    equal_count = 1

        if time.monotonic() - started >= timeout_seconds:
            return SyncStabilityResult(
                stable=False,
                path=str(target),
                observations=observations,
                reason="TIMEOUT_BEFORE_STABLE",
            )
        sleep_fn(interval_seconds)


def mark_question_sync_stable(
    pdf: str | Path,
    *,
    subject: str,
    question: str,
    settings: LocalSettings | None = None,
    interval_seconds: float = 5.0,
    required_equal_observations: int = 3,
    timeout_seconds: float = 90.0,
    observation_fn: ObservationFn = observe_file,
    sleep_fn: SleepFn = time.sleep,
    hash_fn: HashFn = file_sha256,
) -> SyncStabilityResult:
    """Advance DONE_DETECTED -> SYNC_STABLE only for the same verified source.

    If the Drive-backed PDF changed after DONE detection, the state is not
    advanced. The caller must re-run DONE detection on the final stable source.
    """
    cfg = settings or LocalSettings()
    cfg.ensure()
    store = QuestionStateStore(cfg.state_db)
    record = store.get(subject, question)
    if record is None:
        return SyncStabilityResult(
            stable=False,
            path=str(Path(pdf).expanduser().resolve()),
            reason="QUESTION_STATE_MISSING",
        )

    if record.status != QuestionStatus.DONE_DETECTED:
        return SyncStabilityResult(
            stable=False,
            path=str(Path(pdf).expanduser().resolve()),
            reason="QUESTION_NOT_DONE_DETECTED",
            previous_status=record.status,
            current_status=record.status,
        )

    result = wait_for_sync_stable(
        pdf,
        interval_seconds=interval_seconds,
        required_equal_observations=required_equal_observations,
        timeout_seconds=timeout_seconds,
        observation_fn=observation_fn,
        sleep_fn=sleep_fn,
        hash_fn=hash_fn,
    )
    result.previous_status = record.status
    result.current_status = record.status

    if not result.stable:
        return result

    if record.latest_source_sha256 != result.sha256:
        result.reason = "SOURCE_CHANGED_AFTER_DONE_DETECTION"
        result.source_changed_requires_done_recheck = True
        return result

    updated = store.transition(
        subject,
        question,
        QuestionStatus.SYNC_STABLE,
        latest_source_sha256=result.sha256,
    )
    result.current_status = updated.status
    result.state_updated = True
    return result
