from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class QuestionStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    DONE_DETECTED = "DONE_DETECTED"
    SYNC_STABLE = "SYNC_STABLE"
    PROCESSING = "PROCESSING"
    REVIEW_READY = "REVIEW_READY"
    APPROVED = "APPROVED"


_ALLOWED_TRANSITIONS: dict[QuestionStatus, set[QuestionStatus]] = {
    QuestionStatus.IN_PROGRESS: {QuestionStatus.DONE_DETECTED},
    QuestionStatus.DONE_DETECTED: {
        QuestionStatus.IN_PROGRESS,
        QuestionStatus.SYNC_STABLE,
    },
    QuestionStatus.SYNC_STABLE: {
        QuestionStatus.IN_PROGRESS,
        QuestionStatus.PROCESSING,
    },
    QuestionStatus.PROCESSING: {
        QuestionStatus.IN_PROGRESS,
        QuestionStatus.REVIEW_READY,
    },
    QuestionStatus.REVIEW_READY: {
        QuestionStatus.IN_PROGRESS,
        QuestionStatus.APPROVED,
    },
    QuestionStatus.APPROVED: set(),
}


class QuestionRecord(BaseModel):
    subject: str
    question: str
    status: QuestionStatus
    latest_source_sha256: str | None = None
    stable_page_ids: list[str] = Field(default_factory=list)
    draft_revision: int = 0
    created_at: str
    updated_at: str


class InvalidQuestionTransition(RuntimeError):
    """Raised when question workflow state is advanced out of order."""


class QuestionStateStore:
    """Persistent per-question workflow state, separate from pipeline step state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS question_workflow (
                    subject TEXT NOT NULL,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latest_source_sha256 TEXT,
                    stable_page_ids_json TEXT NOT NULL DEFAULT '[]',
                    draft_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (subject, question)
                )
                """
            )

    def get(self, subject: str, question: str) -> QuestionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT subject, question, status, latest_source_sha256,
                       stable_page_ids_json, draft_revision, created_at, updated_at
                FROM question_workflow
                WHERE subject = ? AND question = ?
                """,
                (subject, question),
            ).fetchone()
        if row is None:
            return None
        return QuestionRecord(
            subject=row["subject"],
            question=row["question"],
            status=QuestionStatus(row["status"]),
            latest_source_sha256=row["latest_source_sha256"],
            stable_page_ids=json.loads(row["stable_page_ids_json"]),
            draft_revision=int(row["draft_revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def ensure_in_progress(
        self,
        subject: str,
        question: str,
        *,
        latest_source_sha256: str | None = None,
        stable_page_ids: list[str] | None = None,
    ) -> QuestionRecord:
        now = self._now()
        pages_json = json.dumps(stable_page_ids or [], ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO question_workflow (
                    subject, question, status, latest_source_sha256,
                    stable_page_ids_json, draft_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(subject, question) DO NOTHING
                """,
                (
                    subject,
                    question,
                    QuestionStatus.IN_PROGRESS.value,
                    latest_source_sha256,
                    pages_json,
                    now,
                    now,
                ),
            )
        record = self.get(subject, question)
        assert record is not None
        return record

    def transition(
        self,
        subject: str,
        question: str,
        to_status: QuestionStatus,
        *,
        latest_source_sha256: str | None = None,
        stable_page_ids: list[str] | None = None,
        draft_revision: int | None = None,
    ) -> QuestionRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM question_workflow WHERE subject = ? AND question = ?",
                (subject, question),
            ).fetchone()
            if row is None:
                raise KeyError(f"Question state not found: {subject}/{question}")

            current = QuestionStatus(row["status"])
            if to_status != current and to_status not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidQuestionTransition(
                    f"Invalid question transition: {current.value} -> {to_status.value}"
                )

            source_sha = (
                latest_source_sha256
                if latest_source_sha256 is not None
                else row["latest_source_sha256"]
            )
            page_ids_json = (
                json.dumps(stable_page_ids, ensure_ascii=False, sort_keys=True)
                if stable_page_ids is not None
                else row["stable_page_ids_json"]
            )
            revision = (
                int(draft_revision)
                if draft_revision is not None
                else int(row["draft_revision"])
            )
            connection.execute(
                """
                UPDATE question_workflow
                SET status = ?, latest_source_sha256 = ?, stable_page_ids_json = ?,
                    draft_revision = ?, updated_at = ?
                WHERE subject = ? AND question = ?
                """,
                (
                    to_status.value,
                    source_sha,
                    page_ids_json,
                    revision,
                    self._now(),
                    subject,
                    question,
                ),
            )

        record = self.get(subject, question)
        assert record is not None
        return record
