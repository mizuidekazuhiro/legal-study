"""Process-lifetime locks shared by scheduled and manually launched watchers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class WatchLock:
    def __init__(self, home: Path, kind: str, target: Path):
        key = hashlib.sha256(os.path.normcase(str(target.resolve())).encode()).hexdigest()[:24]
        directory = home / "watcher-locks"
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / f"{kind}-{key}.lock").open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise BlockingIOError(f"{kind} watcher is already running") from None

    def close(self):
        self.file.close()  # The OS also releases this lock after an abnormal exit.

    def __del__(self):
        if hasattr(self, "file"):
            self.file.close()
