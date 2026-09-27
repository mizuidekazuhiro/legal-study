from pathlib import Path

from legal_study.automation.file_watcher import FileUpdateWatcher


def test_watcher_does_not_emit_without_change(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"v1")
    watcher = FileUpdateWatcher(source)

    assert watcher.poll_once() is None


def test_watcher_emits_when_file_changes(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"v1")
    watcher = FileUpdateWatcher(source)
    before = watcher.baseline

    source.write_bytes(b"version-two")
    event = watcher.poll_once()

    assert event is not None
    assert event.previous == before
    assert event.current.exists is True
    assert event.current.size == len(b"version-two")
    assert watcher.baseline == event.current


def test_watcher_ignores_transient_missing_then_same_file(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"v1")
    watcher = FileUpdateWatcher(source)
    original = source.read_bytes()

    source.unlink()
    assert watcher.poll_once() is None

    source.write_bytes(original)
    # Preserve mtime so the restored file has the same cheap signature where possible.
    baseline = watcher.baseline
    if baseline.mtime_ns is not None:
        import os

        os.utime(source, ns=(baseline.mtime_ns, baseline.mtime_ns))

    event = watcher.poll_once()
    if event is not None:
        # Some platforms expose a different file id after recreation. That is a real
        # replacement signal and is valid to surface.
        assert event.current.file_id != event.previous.file_id
    else:
        assert watcher.baseline == baseline


def test_watcher_emits_after_atomic_replacement(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"v1")
    watcher = FileUpdateWatcher(source)

    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"v2 replacement")
    replacement.replace(source)

    event = watcher.poll_once()

    assert event is not None
    assert event.current.exists is True
    assert event.current.size == len(b"v2 replacement")
