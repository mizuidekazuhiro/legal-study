from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.source_store import SourceSnapshot


class WorkStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WorkItem(BaseModel):
    id: int
    subject: str
    question: str
    source_sha256: str
    stable_page_ids: list[str] = Field(default_factory=list)
    source_snapshot: SourceSnapshot | None = None
    status: WorkStatus
    attempt_count: int = 0
    last_error: str | None = None
    output_dir: str | None = None
    run_id: str | None = None
    created_at: str
    updated_at: str


class WatchState(BaseModel):
    subject: str
    source_path: str
    last_processed_sha256: str
    created_at: str
    updated_at: str


class AutomationStateStore:
    """Persistent watcher baseline and serialized work queue."""

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
                CREATE TABLE IF NOT EXISTS watched_documents (
                    subject TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    last_processed_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (subject, source_path)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_work_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    question TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    stable_page_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_snapshot_json TEXT,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    output_dir TEXT,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (subject, question, source_sha256)
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(automation_work_queue)"
                ).fetchall()
            }
            for name, definition in (
                ("source_snapshot_json", "TEXT"),
                ("output_dir", "TEXT"),
                ("run_id", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE automation_work_queue ADD COLUMN {name} {definition}"
                    )

    @staticmethod
    def _work_item(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            id=int(row["id"]),
            subject=row["subject"],
            question=row["question"],
            source_sha256=row["source_sha256"],
            stable_page_ids=json.loads(row["stable_page_ids_json"]),
            source_snapshot=(
                SourceSnapshot.model_validate_json(row["source_snapshot_json"])
                if row["source_snapshot_json"]
                else None
            ),
            status=WorkStatus(row["status"]),
            attempt_count=int(row["attempt_count"]),
            last_error=row["last_error"],
            output_dir=row["output_dir"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_watch_state(self, subject: str, source_path: str | Path) -> WatchState | None:
        normalized = str(Path(source_path).expanduser().resolve())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT subject, source_path, last_processed_sha256, created_at, updated_at
                FROM watched_documents
                WHERE subject = ? AND source_path = ?
                """,
                (subject, normalized),
            ).fetchone()
        if row is None:
            return None
        return WatchState(
            subject=row["subject"],
            source_path=row["source_path"],
            last_processed_sha256=row["last_processed_sha256"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def set_watch_baseline(
        self,
        subject: str,
        source_path: str | Path,
        source_sha256: str,
    ) -> WatchState:
        normalized = str(Path(source_path).expanduser().resolve())
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watched_documents (
                    subject, source_path, last_processed_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subject, source_path) DO UPDATE SET
                    last_processed_sha256 = excluded.last_processed_sha256,
                    updated_at = excluded.updated_at
                """,
                (subject, normalized, source_sha256, now, now),
            )
        state = self.get_watch_state(subject, normalized)
        assert state is not None
        return state

    def enqueue(
        self,
        *,
        subject: str,
        question: str,
        source_sha256: str,
        stable_page_ids: list[str],
        source_snapshot: SourceSnapshot | None = None,
    ) -> WorkItem:
        now = self._now()
        pages_json = json.dumps(stable_page_ids, ensure_ascii=False, sort_keys=True)
        snapshot_json = (
            source_snapshot.model_dump_json() if source_snapshot is not None else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_work_queue (
                    subject, question, source_sha256, stable_page_ids_json,
                    source_snapshot_json, status, attempt_count, last_error,
                    output_dir, run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(subject, question, source_sha256) DO NOTHING
                """,
                (
                    subject,
                    question,
                    source_sha256,
                    pages_json,
                    snapshot_json,
                    WorkStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM automation_work_queue
                WHERE subject = ? AND question = ? AND source_sha256 = ?
                """,
                (subject, question, source_sha256),
            ).fetchone()
        assert row is not None
        return self._work_item(row)

    def get_work_item(self, item_id: int) -> WorkItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_work_queue WHERE id = ?",
                (item_id,),
            ).fetchone()
        return self._work_item(row) if row is not None else None

    def list_pending(self) -> list[WorkItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_work_queue
                WHERE status = ?
                ORDER BY id
                """,
                (WorkStatus.PENDING.value,),
            ).fetchall()
        return [self._work_item(row) for row in rows]

    def claim_next(self) -> WorkItem | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM automation_work_queue
                WHERE status = ?
                ORDER BY id
                LIMIT 1
                """,
                (WorkStatus.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            now = self._now()
            connection.execute(
                """
                UPDATE automation_work_queue
                SET status = ?, attempt_count = attempt_count + 1,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (WorkStatus.RUNNING.value, now, int(row["id"])),
            )
            updated = connection.execute(
                "SELECT * FROM automation_work_queue WHERE id = ?",
                (int(row["id"]),),
            ).fetchone()
        assert updated is not None
        return self._work_item(updated)

    def mark_completed(
        self,
        item_id: int,
        *,
        output_dir: str | Path | None = None,
        run_id: str | None = None,
    ) -> WorkItem:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE automation_work_queue
                SET status = ?, last_error = NULL, output_dir = ?, run_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    WorkStatus.COMPLETED.value,
                    str(Path(output_dir).resolve()) if output_dir is not None else None,
                    run_id,
                    self._now(),
                    item_id,
                ),
            )
        item = self.get_work_item(item_id)
        if item is None:
            raise KeyError(f"Work item not found: {item_id}")
        return item

    def mark_failed(self, item_id: int, error: str) -> WorkItem:
        return self._set_terminal(item_id, WorkStatus.FAILED, error)

    def _set_terminal(
        self,
        item_id: int,
        status: WorkStatus,
        error: str | None,
    ) -> WorkItem:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE automation_work_queue
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error, self._now(), item_id),
            )
        item = self.get_work_item(item_id)
        if item is None:
            raise KeyError(f"Work item not found: {item_id}")
        return item

    def recover_interrupted(self) -> int:
        """Return RUNNING items to PENDING after an unclean shutdown."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_work_queue
                SET status = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    WorkStatus.PENDING.value,
                    self._now(),
                    WorkStatus.RUNNING.value,
                ),
            )
            return int(cursor.rowcount)
