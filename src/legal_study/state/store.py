from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from legal_study.run_manifest import RunManifest


class StepStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StepRecord(BaseModel):
    run_id: str
    step_name: str
    status: StepStatus
    started_at: str
    completed_at: str | None
    input_hash: str
    output_hash: str | None
    retry_count: int
    error: str | None
    version: str


class RunStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    input_hash TEXT NOT NULL,
                    output_hash TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    version TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_name),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                """
            )

    def register_run(self, manifest: RunManifest, manifest_path: Path) -> None:
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT input_hash, source_sha256 FROM runs WHERE run_id = ?",
                (manifest.run_id,),
            ).fetchone()
            if existing is not None and (
                existing["input_hash"] != manifest.input_hash
                or existing["source_sha256"] != manifest.source.sha256
            ):
                raise RuntimeError(f"Run state identity mismatch: {manifest.run_id}")
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, input_hash, source_sha256, manifest_path,
                    status, created_at, updated_at, error
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                    manifest_path = excluded.manifest_path,
                    status = 'RUNNING',
                    updated_at = excluded.updated_at,
                    error = NULL
                """,
                (
                    manifest.run_id,
                    manifest.input_hash,
                    manifest.source.sha256,
                    str(manifest_path),
                    manifest.created_at.isoformat(),
                    now,
                ),
            )

    def set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ?, error = ? WHERE run_id = ?",
                (status, self._now(), error, run_id),
            )

    def get_step(self, run_id: str, step_name: str) -> StepRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_name = ?",
                (run_id, step_name),
            ).fetchone()
        return StepRecord.model_validate(dict(row)) if row is not None else None

    def begin_step(
        self, run_id: str, step_name: str, *, input_hash: str, version: str
    ) -> StepRecord:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT retry_count FROM steps WHERE run_id = ? AND step_name = ?",
                (run_id, step_name),
            ).fetchone()
            retry_count = 0 if existing is None else int(existing["retry_count"]) + 1
            connection.execute(
                """
                INSERT INTO steps (
                    run_id, step_name, status, started_at, completed_at,
                    input_hash, output_hash, retry_count, error, version
                ) VALUES (?, ?, 'RUNNING', ?, NULL, ?, NULL, ?, NULL, ?)
                ON CONFLICT(run_id, step_name) DO UPDATE SET
                    status = 'RUNNING',
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    input_hash = excluded.input_hash,
                    output_hash = NULL,
                    retry_count = excluded.retry_count,
                    error = NULL,
                    version = excluded.version
                """,
                (run_id, step_name, now, input_hash, retry_count, version),
            )
        record = self.get_step(run_id, step_name)
        assert record is not None
        return record

    def complete_step(self, run_id: str, step_name: str, *, output_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steps
                SET status = 'COMPLETED', completed_at = ?, output_hash = ?, error = NULL
                WHERE run_id = ? AND step_name = ?
                """,
                (self._now(), output_hash, run_id, step_name),
            )

    def fail_step(self, run_id: str, step_name: str, *, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steps
                SET status = 'FAILED', completed_at = ?, error = ?
                WHERE run_id = ? AND step_name = ?
                """,
                (self._now(), error, run_id, step_name),
            )

    def can_resume(
        self,
        run_id: str,
        step_name: str,
        *,
        input_hash: str,
        version: str,
        output_hash: str,
    ) -> bool:
        record = self.get_step(run_id, step_name)
        return bool(
            record is not None
            and record.status == StepStatus.COMPLETED
            and record.input_hash == input_hash
            and record.version == version
            and record.output_hash == output_hash
        )
