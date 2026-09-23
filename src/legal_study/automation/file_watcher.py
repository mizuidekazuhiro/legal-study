from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel


class WatchedFileSignature(BaseModel):
    exists: bool
    size: int | None = None
    mtime_ns: int | None = None
    file_id: int | None = None


class FileUpdateEvent(BaseModel):
    path: str
    previous: WatchedFileSignature
    current: WatchedFileSignature
    detected_at_monotonic: float


def read_file_signature(path: Path) -> WatchedFileSignature:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return WatchedFileSignature(exists=False)
    return WatchedFileSignature(
        exists=True,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        file_id=getattr(stat, "st_ino", None),
    )


class FileUpdateWatcher:
    """Cheap polling watcher for a Drive-synced local PDF.

    The watcher does not decide that a write is complete. It only emits a change
    signal. sync_stability remains the authoritative gate before downstream
    processing.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._last_present = read_file_signature(self.path)
        self._saw_missing_since_last_present = False

    @property
    def baseline(self) -> WatchedFileSignature:
        return self._last_present

    def poll_once(self) -> FileUpdateEvent | None:
        current = read_file_signature(self.path)

        if not current.exists:
            self._saw_missing_since_last_present = True
            return None

        previous = self._last_present
        if not previous.exists:
            self._last_present = current
            self._saw_missing_since_last_present = False
            return None

        changed = current != previous
        if changed:
            self._last_present = current
            self._saw_missing_since_last_present = False
            return FileUpdateEvent(
                path=str(self.path),
                previous=previous,
                current=current,
                detected_at_monotonic=time.monotonic(),
            )

        # A transient disappearance followed by the same signature is ignored.
        self._saw_missing_since_last_present = False
        return None


def iter_file_updates(
    path: str | Path,
    *,
    poll_interval_seconds: float = 60.0,
) -> Iterator[FileUpdateEvent]:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")
    watcher = FileUpdateWatcher(path)
    while True:
        time.sleep(poll_interval_seconds)
        event = watcher.poll_once()
        if event is not None:
            yield event
