from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.automation.queue import AutomationStateStore, WorkStatus
from legal_study.page_identity import ensure_source_page_index
from legal_study.pdf.pipeline import PdfIngestPipeline
from legal_study.run_manifest import prepare_run
from legal_study.settings import LocalSettings
from legal_study.source_store import SourceSnapshot
from legal_study.state import QuestionStateStore, QuestionStatus


class QueueWorkerResult(BaseModel):
    claimed: bool
    work_item_id: int | None = None
    subject: str | None = None
    question: str | None = None
    source_sha256: str | None = None
    pages: list[int] = Field(default_factory=list)
    queue_status: WorkStatus | None = None
    question_status: QuestionStatus | None = None
    output_dir: str | None = None
    run_id: str | None = None
    reason: str
    error: str | None = None


def _resolve_stable_pages(
    *,
    stable_page_ids: list[str],
    source_sha256: str,
    settings: LocalSettings,
    snapshot: SourceSnapshot,
) -> list[int]:
    if not stable_page_ids:
        raise RuntimeError("Work item has no stable_page_ids")
    if snapshot.sha256 != source_sha256:
        raise RuntimeError("Queued source snapshot SHA does not match work item SHA")

    index = ensure_source_page_index(snapshot, settings.cache_dir)
    by_id: dict[str, list[int]] = {}
    for page in index.pages:
        by_id.setdefault(page.stable_page_id, []).append(page.page_number)

    resolved: list[int] = []
    for stable_page_id in stable_page_ids:
        matches = by_id.get(stable_page_id, [])
        if len(matches) != 1:
            raise RuntimeError(
                "Stable page id did not resolve uniquely: "
                f"{stable_page_id} -> {matches}"
            )
        resolved.append(matches[0])
    return sorted(set(resolved))


def process_next_work_item(
    *,
    pipeline: PdfIngestPipeline,
    settings: LocalSettings | None = None,
) -> QueueWorkerResult:
    """Claim and process exactly one queued question.

    The queue remains the serialization boundary. Question state advances to
    PROCESSING before the ingest pipeline starts. On a handled failure, the
    question returns to SYNC_STABLE so the failed item can be retried later.
    """
    cfg = settings or LocalSettings()
    cfg.ensure()
    queue = AutomationStateStore(cfg.state_db)
    questions = QuestionStateStore(cfg.state_db)

    item = queue.claim_next()
    if item is None:
        return QueueWorkerResult(
            claimed=False,
            reason="QUEUE_EMPTY",
        )

    result = QueueWorkerResult(
        claimed=True,
        work_item_id=item.id,
        subject=item.subject,
        question=item.question,
        source_sha256=item.source_sha256,
        queue_status=item.status,
        reason="CLAIMED",
    )

    try:
        if item.source_snapshot is None:
            raise RuntimeError("Queued work item is missing immutable source snapshot")

        question = questions.get(item.subject, item.question)
        if question is None:
            raise RuntimeError("Question workflow state is missing")
        if question.latest_source_sha256 != item.source_sha256:
            raise RuntimeError(
                "Question source SHA does not match queued work item source SHA"
            )
        if question.stable_page_ids != item.stable_page_ids:
            raise RuntimeError(
                "Question stable_page_ids do not match queued work item"
            )
        if question.status == QuestionStatus.SYNC_STABLE:
            question = questions.transition(
                item.subject,
                item.question,
                QuestionStatus.PROCESSING,
            )
        elif question.status != QuestionStatus.PROCESSING:
            raise RuntimeError(
                "Question must be SYNC_STABLE or PROCESSING before queue work; "
                f"got {question.status.value}"
            )

        pages = _resolve_stable_pages(
            stable_page_ids=item.stable_page_ids,
            source_sha256=item.source_sha256,
            settings=cfg,
            snapshot=item.source_snapshot,
        )
        result.pages = pages
        result.question_status = QuestionStatus.PROCESSING

        prepared = prepare_run(
            snapshot=item.source_snapshot,
            subject=item.subject,
            question=item.question,
            pages=pages,
            pipeline_config=pipeline.input_config(),
            settings=cfg,
        )
        pipeline.run(
            item.source_snapshot,
            prepared,
            pages=pages,
        )

        completed = queue.mark_completed(
            item.id,
            output_dir=prepared.output_dir,
            run_id=prepared.manifest.run_id,
        )
        result.queue_status = completed.status
        result.output_dir = str(prepared.output_dir)
        result.run_id = prepared.manifest.run_id
        result.reason = "INGEST_COMPLETED"
        return result
    except Exception as exc:
        current = questions.get(item.subject, item.question)
        if (
            current is not None
            and current.status == QuestionStatus.PROCESSING
            and current.latest_source_sha256 == item.source_sha256
        ):
            questions.transition(
                item.subject,
                item.question,
                QuestionStatus.SYNC_STABLE,
            )
            result.question_status = QuestionStatus.SYNC_STABLE
        elif current is not None:
            result.question_status = current.status

        failed = queue.mark_failed(item.id, repr(exc))
        result.queue_status = failed.status
        result.reason = "INGEST_FAILED"
        result.error = repr(exc)
        return result


def drain_pending_work(
    *,
    pipeline: PdfIngestPipeline,
    settings: LocalSettings | None = None,
) -> list[QueueWorkerResult]:
    """Process queued work serially until no PENDING item remains."""
    results: list[QueueWorkerResult] = []
    while True:
        result = process_next_work_item(pipeline=pipeline, settings=settings)
        if not result.claimed:
            break
        results.append(result)
    return results
