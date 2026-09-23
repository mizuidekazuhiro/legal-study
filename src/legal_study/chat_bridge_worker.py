from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from legal_study.chat_result import apply_chat_result, validate_chat_result
from legal_study.io_utils import atomic_write_json, file_sha256
from legal_study.run_manifest import RunManifest
from legal_study.settings import LocalSettings


class StrictBridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BridgeAction(StrEnum):
    APPLY_OBSIDIAN = "apply_obsidian"
    REGISTER_NOTION = "register_notion"
    APPLY_ALL = "apply_all"


class BridgeCommand(StrictBridgeModel):
    schema_version: Literal["chat_bridge_command.v1"] = "chat_bridge_command.v1"
    command_id: str = Field(min_length=8, max_length=160)
    action: BridgeAction
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    run_id: str = Field(min_length=64, max_length=64)
    result_file: str = Field(min_length=1)
    result_sha256: str = Field(min_length=64, max_length=64)
    approved_at: str
    approval_text: str = Field(min_length=1)
    notion_registration_authorized: bool = False

    @field_validator("command_id")
    @classmethod
    def validate_command_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("command_id must not be empty")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._" for char in cleaned):
            raise ValueError("command_id contains unsupported characters")
        return cleaned

    @field_validator("result_file")
    @classmethod
    def validate_result_file(cls, value: str) -> str:
        cleaned = value.strip()
        path = Path(cleaned)
        if (
            not cleaned
            or path.is_absolute()
            or PureWindowsPath(cleaned).is_absolute()
            or ".." in path.parts
            or path.suffix.lower() != ".json"
            or path.parts != ("10_approved", path.name)
            or cleaned != path.as_posix()
        ):
            raise ValueError("result_file must be 10_approved/<filename>.json")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_notion_authorization(self) -> BridgeCommand:
        if (
            self.action in {BridgeAction.REGISTER_NOTION, BridgeAction.APPLY_ALL}
            and not self.notion_registration_authorized
        ):
            raise ValueError(
                "Notion actions require notion_registration_authorized=true"
            )
        return self


class BridgeReceipt(StrictBridgeModel):
    schema_version: Literal["chat_bridge_receipt.v1"] = "chat_bridge_receipt.v1"
    command_id: str
    status: Literal["success", "failed", "skipped"]
    action: BridgeAction
    subject: str
    question: str
    source_sha256: str
    run_id: str
    command_sha256: str
    result_sha256: str
    processed_at: str
    obsidian_status: str | None = None
    obsidian_path: str | None = None
    notion_status: str | None = None
    notion_created: int = 0
    notion_verified: int = 0
    error: str | None = None


class NotionRegistrationResult(StrictBridgeModel):
    status: str
    created: int = 0
    verified: int = 0


class NotionRegistrar(Protocol):
    def register(self, *, result_path: Path, run_dir: Path) -> NotionRegistrationResult:
        ...


class BridgeCommandState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BridgeStateStore:
    """Idempotency ledger for Drive-synced Chat bridge commands."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
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
                CREATE TABLE IF NOT EXISTS chat_bridge_commands (
                    command_id TEXT PRIMARY KEY,
                    command_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_error TEXT,
                    receipt_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def begin(self, command_id: str, command_sha256: str) -> Literal["new", "resume", "done"]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_bridge_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            now = self._now()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO chat_bridge_commands (
                        command_id, command_sha256, state, last_error,
                        receipt_path, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        command_id,
                        command_sha256,
                        BridgeCommandState.RUNNING.value,
                        now,
                        now,
                    ),
                )
                return "new"

            if row["command_sha256"] != command_sha256:
                raise RuntimeError(
                    "command_id was reused with different command bytes"
                )
            state = BridgeCommandState(row["state"])
            if state == BridgeCommandState.COMPLETED:
                return "done"

            connection.execute(
                """
                UPDATE chat_bridge_commands
                SET state = ?, last_error = NULL, updated_at = ?
                WHERE command_id = ?
                """,
                (BridgeCommandState.RUNNING.value, now, command_id),
            )
            return "resume"

    def complete(self, command_id: str, receipt_path: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_bridge_commands
                SET state = ?, last_error = NULL, receipt_path = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (
                    BridgeCommandState.COMPLETED.value,
                    str(receipt_path),
                    self._now(),
                    command_id,
                ),
            )

    def fail(self, command_id: str, error: str, receipt_path: Path | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_bridge_commands
                SET state = ?, last_error = ?, receipt_path = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (
                    BridgeCommandState.FAILED.value,
                    error,
                    str(receipt_path) if receipt_path else None,
                    self._now(),
                    command_id,
                ),
            )


class BridgeLayout(StrictBridgeModel):
    root: Path

    @property
    def approved(self) -> Path:
        return self.root / "10_approved"

    @property
    def commands(self) -> Path:
        return self.root / "20_commands"

    @property
    def receipts(self) -> Path:
        return self.root / "30_receipts"

    @property
    def failed(self) -> Path:
        return self.root / "99_failed"

    def verify(self) -> None:
        missing = [
            str(path)
            for path in (self.approved, self.commands, self.receipts, self.failed)
            if not path.is_dir()
        ]
        if missing:
            raise FileNotFoundError(
                "Chat bridge folder structure is incomplete: " + ", ".join(missing)
            )


class BridgeWorkerResult(StrictBridgeModel):
    processed: bool
    command_id: str | None = None
    status: str
    receipt_path: str | None = None
    error: str | None = None


def resolve_run_dir(
    *,
    runs_dir: Path,
    run_id: str,
    subject: str,
    question: str,
    source_sha256: str,
) -> Path:
    matches: list[Path] = []
    for manifest_path in runs_dir.rglob("run_manifest.json"):
        try:
            manifest = RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if (
            manifest.run_id == run_id
            and manifest.subject == subject
            and manifest.question == question
            and manifest.source.sha256 == source_sha256
        ):
            matches.append(manifest_path.parent.resolve())

    if len(matches) != 1:
        raise RuntimeError(
            "Could not resolve exactly one local run for bridge command: "
            f"matches={matches}"
        )
    return matches[0]


def _resolve_bridge_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Bridge file escapes bridge root") from exc
    return candidate


def process_bridge_command(
    *,
    command_path: Path,
    bridge_root: Path,
    obsidian_inbox: Path,
    settings: LocalSettings | None = None,
    notion_registrar: NotionRegistrar | None = None,
) -> BridgeWorkerResult:
    cfg = settings or LocalSettings()
    cfg.ensure()

    layout = BridgeLayout(root=bridge_root.expanduser().resolve())
    layout.verify()
    command_file = command_path.expanduser().resolve()

    if command_file.parent != layout.commands.resolve():
        raise ValueError("Command must be located directly in 20_commands")
    if command_file.suffix.lower() != ".json":
        raise ValueError("Bridge command must be JSON")

    command_sha = file_sha256(command_file)
    command = BridgeCommand.model_validate_json(
        command_file.read_text(encoding="utf-8")
    )
    state = BridgeStateStore(cfg.state_db)
    begin_state = state.begin(command.command_id, command_sha)

    receipt_path = layout.receipts / f"{command.command_id}.receipt.json"
    failed_path = layout.failed / f"{command.command_id}.receipt.json"

    if begin_state == "done":
        return BridgeWorkerResult(
            processed=False,
            command_id=command.command_id,
            status="ALREADY_COMPLETED",
            receipt_path=str(receipt_path) if receipt_path.exists() else None,
        )

    try:
        result_path = _resolve_bridge_file(layout.root, command.result_file)
        if result_path.parent != layout.approved.resolve():
            raise ValueError("Approved result must remain in 10_approved")
        if not result_path.is_file():
            raise FileNotFoundError(f"Approved result is missing: {result_path}")
        actual_result_sha = file_sha256(result_path)
        if actual_result_sha != command.result_sha256:
            raise RuntimeError("Approved result SHA does not match command")

        run_dir = resolve_run_dir(
            runs_dir=cfg.runs_dir,
            run_id=command.run_id,
            subject=command.subject,
            question=command.question,
            source_sha256=command.source_sha256,
        )
        validation = validate_chat_result(result_path=result_path, run_dir=run_dir)
        if not validation.valid:
            raise RuntimeError(
                "Chat result validation failed: " + ", ".join(validation.issues)
            )

        obsidian_status: str | None = None
        obsidian_path: str | None = None
        notion_status: str | None = None
        notion_created = 0
        notion_verified = 0

        if command.action in {BridgeAction.APPLY_OBSIDIAN, BridgeAction.APPLY_ALL}:
            apply_report = apply_chat_result(
                result_path=result_path,
                run_dir=run_dir,
                obsidian_inbox=obsidian_inbox,
                update_existing=True,
            )
            obsidian_status = apply_report.inbox_status
            obsidian_path = apply_report.inbox_path

        if command.action in {BridgeAction.REGISTER_NOTION, BridgeAction.APPLY_ALL}:
            if notion_registrar is None:
                raise RuntimeError(
                    "Notion registration was authorized but no Notion registrar "
                    "is configured on this PC"
                )
            notion = notion_registrar.register(
                result_path=result_path,
                run_dir=run_dir,
            )
            notion_status = notion.status
            notion_created = notion.created
            notion_verified = notion.verified
            if notion.created != notion.verified:
                raise RuntimeError(
                    "Notion post-registration verification count mismatch"
                )

        receipt = BridgeReceipt(
            command_id=command.command_id,
            status="success",
            action=command.action,
            subject=command.subject,
            question=command.question,
            source_sha256=command.source_sha256,
            run_id=command.run_id,
            command_sha256=command_sha,
            result_sha256=command.result_sha256,
            processed_at=datetime.now(UTC).isoformat(),
            obsidian_status=obsidian_status,
            obsidian_path=obsidian_path,
            notion_status=notion_status,
            notion_created=notion_created,
            notion_verified=notion_verified,
        )
        atomic_write_json(receipt_path, receipt.model_dump(mode="json"))
        state.complete(command.command_id, receipt_path)
        return BridgeWorkerResult(
            processed=True,
            command_id=command.command_id,
            status="SUCCESS",
            receipt_path=str(receipt_path),
        )
    except Exception as exc:  # noqa: BLE001 -- worker boundary must receipt all failures
        receipt = BridgeReceipt(
            command_id=command.command_id,
            status="failed",
            action=command.action,
            subject=command.subject,
            question=command.question,
            source_sha256=command.source_sha256,
            run_id=command.run_id,
            command_sha256=command_sha,
            result_sha256=command.result_sha256,
            processed_at=datetime.now(UTC).isoformat(),
            error=repr(exc),
        )
        atomic_write_json(failed_path, receipt.model_dump(mode="json"))
        state.fail(command.command_id, repr(exc), failed_path)
        return BridgeWorkerResult(
            processed=True,
            command_id=command.command_id,
            status="FAILED",
            receipt_path=str(failed_path),
            error=repr(exc),
        )


def watch_bridge_commands(
    *,
    bridge_root: Path,
    obsidian_inbox: Path,
    settings: LocalSettings | None = None,
    notion_registrar_factory: Callable[[], NotionRegistrar | None] | None = None,
    poll_interval_seconds: float = 5.0,
    stable_seconds: float = 3.0,
) -> Iterator[BridgeWorkerResult]:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")
    if stable_seconds < 0:
        raise ValueError("stable_seconds must be >= 0")

    cfg = settings or LocalSettings()
    cfg.ensure()
    layout = BridgeLayout(root=bridge_root.expanduser().resolve())
    layout.verify()

    observations: dict[Path, tuple[int, int, float]] = {}

    while True:
        now = time.monotonic()
        present = sorted(layout.commands.glob("*.json"))

        for path in list(observations):
            if path not in present:
                observations.pop(path, None)

        for path in present:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = observations.get(path)

            if previous is None or previous[:2] != signature:
                observations[path] = (signature[0], signature[1], now)
                continue

            unchanged_since = previous[2]
            if now - unchanged_since < stable_seconds:
                continue

            registrar = None
            if notion_registrar_factory is not None:
                preview = BridgeCommand.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if preview.action in {
                    BridgeAction.REGISTER_NOTION,
                    BridgeAction.APPLY_ALL,
                }:
                    registrar = notion_registrar_factory()

            yield process_bridge_command(
                command_path=path,
                bridge_root=layout.root,
                obsidian_inbox=obsidian_inbox,
                settings=cfg,
                notion_registrar=registrar,
            )
            observations[path] = (signature[0], signature[1], now + 10**9)

        time.sleep(poll_interval_seconds)
