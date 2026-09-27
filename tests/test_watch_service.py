import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from legal_study.automation import watch_service
from legal_study.automation.watch_lock import WatchLock
from legal_study.automation.watch_service import (
    bridge_paths_available,
    record_child_exit,
    signature,
)
from legal_study.chat_bridge_worker import watch_bridge_commands
from legal_study.settings import LocalSettings


def read_heartbeat(path):
    # Windows atomic replacement can briefly deny concurrent readers.
    for attempt in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


@pytest.mark.parametrize(
    "kind,command", [("study", "watch-study"), ("bridge", "watch-chat-bridge")]
)
def test_cli_duplicate_stops_before_processing(tmp_path, monkeypatch, kind, command):
    from typer.testing import CliRunner

    from legal_study.cli import app

    monkeypatch.setenv("LEGAL_STUDY_HOME", str(tmp_path / "home"))
    target = tmp_path / ("input.pdf" if kind == "study" else "bridge")
    if kind == "study":
        target.write_bytes(b"not a PDF; duplicate must exit before opening it")
        args = [command, str(target)]
    else:
        target.mkdir()
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        args = [command, "--bridge-root", str(target), "--obsidian-inbox", str(inbox)]
    lock = WatchLock(tmp_path / "home", kind, target)
    try:
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
        assert "already running" in result.output
        assert not (tmp_path / "home" / "state.sqlite3").exists()
    finally:
        lock.close()


def test_process_lock_excludes_duplicates_and_releases_after_exit(tmp_path):
    lock = WatchLock(tmp_path, "study", tmp_path / "input.pdf")
    code = "from pathlib import Path; from legal_study.automation.watch_lock import WatchLock; WatchLock(Path(__import__('sys').argv[1]),'study',Path(__import__('sys').argv[1])/'input.pdf')"
    denied = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, check=False
    )
    assert denied.returncode != 0
    assert b"already running" in denied.stderr
    lock.close()
    allowed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, check=False
    )
    assert allowed.returncode == 0, allowed.stderr


def test_signature_uses_metadata_and_tolerates_missing(tmp_path):
    pdf = tmp_path / "input.pdf"
    assert signature(pdf) is None
    pdf.write_bytes(b"not a PDF: metadata inspection only")
    before = signature(pdf)
    pdf.write_bytes(b"updated content")
    assert signature(pdf) != before


def test_bridge_availability_requires_worker_subfolders(tmp_path):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    assert not bridge_paths_available(bridge, inbox)
    for name in ("10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir()

    assert not bridge_paths_available(bridge, inbox)
    (bridge / "00_pending").mkdir()

    assert bridge_paths_available(bridge, inbox)


def test_clean_child_exit_does_not_set_last_error() -> None:
    state: dict[str, object] = {"last_error": None}

    record_child_exit(state, name="bridge", code=0, now=100.0)

    assert state["last_error"] is None

    record_child_exit(state, name="bridge", code=1, now=101.0)
    assert state["last_error"] == {
        "at": 101.0,
        "worker": "bridge",
        "message": "exit 1; restart scheduled",
    }


def test_stale_heartbeat_is_not_alive() -> None:
    state = {"pid": 123, "heartbeat_at": 100.0, "lease_expires_at": 130.0}

    assert watch_service.service_state_is_alive(state, now=120.0, process_exists=True)
    assert not watch_service.service_state_is_alive(state, now=131.0, process_exists=True)
    assert not watch_service.service_state_is_alive(state, now=120.0, process_exists=False)


def test_bridge_waits_for_drive_and_recovers_without_restart(tmp_path, monkeypatch):
    bridge = tmp_path / "unavailable" / "LegalStudy_ChatBridge"
    inbox = tmp_path / "unavailable" / "Obsidian_Inbox"
    settings = LocalSettings(home=tmp_path / "home")
    monkeypatch.setattr("legal_study.chat_bridge_worker.time.sleep", lambda _seconds: None)
    watcher = watch_bridge_commands(
        bridge_root=bridge,
        obsidian_inbox=inbox,
        settings=settings,
        poll_interval_seconds=0.01,
        stable_seconds=0,
    )

    waiting = next(watcher)
    assert waiting.status == "WAITING_FOR_BRIDGE"
    for name in ("00_pending", "10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    inbox.mkdir(parents=True)

    available = next(watcher)
    assert available.status == "BRIDGE_AVAILABLE"

    (bridge / "20_commands").rmdir()
    disconnected = next(watcher)
    assert disconnected.status == "WAITING_FOR_BRIDGE"
    (bridge / "20_commands").mkdir()
    restored = next(watcher)
    assert restored.status == "BRIDGE_AVAILABLE"


def test_service_does_not_parse_existing_pdf_and_stops_cleanly(tmp_path):
    pdf = tmp_path / "never-read.pdf"
    pdf.write_bytes(b"invalid PDF deliberately: no existing PDF processing allowed")
    cfg = {
        "repository": str(Path(__file__).parents[1]),
        "home": str(tmp_path / "home"),
        "service_directory": str(tmp_path / "service"),
        "pdf": str(pdf),
        "bridge_root": str(tmp_path / "unavailable-drive"),
        "obsidian_inbox": str(tmp_path / "unavailable-inbox"),
        "poll_seconds": 0.1,
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "legal_study.automation.watch_service",
        "--config",
        str(config),
    ]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        state_path = tmp_path / "service" / "service-state.json"
        deadline = time.monotonic() + 15
        while not state_path.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert proc.poll() is None
        state = read_heartbeat(state_path)
        assert state["children"] == {}
        assert not state["study_started"]
        assert not (tmp_path / "home" / "state.sqlite3").exists()
        assert subprocess.run(command, timeout=10, check=False).returncode == 0
        assert read_heartbeat(state_path)["pid"] == state["pid"]
        assert subprocess.run(command + ["--stop"], timeout=10, check=False).returncode == 0
        assert proc.wait(timeout=10) == 0
        assert read_heartbeat(state_path)["status"] == "stopped"
    finally:
        if proc.poll() is None:
            subprocess.run(command + ["--stop"], timeout=10, check=False)
            proc.wait(timeout=10)
        proc.stderr.close()


def test_service_restarts_bridge_child_after_abnormal_exit(tmp_path):
    pdf = tmp_path / "never-read.pdf"
    pdf.write_bytes(b"invalid PDF deliberately")
    bridge = tmp_path / "bridge"
    for name in ("00_pending", "10_approved", "20_commands", "30_receipts", "99_failed"):
        (bridge / name).mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    cfg = {
        "repository": str(Path(__file__).parents[1]),
        "home": str(tmp_path / "home"),
        "service_directory": str(tmp_path / "service"),
        "pdf": str(pdf),
        "bridge_root": str(bridge),
        "obsidian_inbox": str(inbox),
        "poll_seconds": 0.1,
        "child_restart_seconds": 0.1,
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "legal_study.automation.watch_service",
        "--config",
        str(config),
    ]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    state_path = tmp_path / "service" / "service-state.json"
    try:
        deadline = time.monotonic() + 15
        first_pid = None
        while time.monotonic() < deadline and proc.poll() is None:
            if state_path.exists():
                first_pid = read_heartbeat(state_path).get("children", {}).get("bridge")
                if first_pid:
                    break
            time.sleep(0.1)
        assert first_pid
        os.kill(int(first_pid), 15)

        replacement_pid = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and proc.poll() is None:
            replacement_pid = read_heartbeat(state_path).get("children", {}).get("bridge")
            if replacement_pid and replacement_pid != first_pid:
                break
            time.sleep(0.1)

        assert proc.poll() is None
        assert replacement_pid and replacement_pid != first_pid
    finally:
        subprocess.run(command + ["--stop"], timeout=10, check=False)
        if proc.poll() is None:
            proc.wait(timeout=10)
        proc.stderr.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job object lifecycle")
def test_job_terminates_owned_child_when_closed(tmp_path):
    import ctypes
    from ctypes import wintypes

    from legal_study.automation.watch_service import ChildJob

    job = ChildJob()
    pid_file = tmp_path / "child.pid"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
            str(pid_file),
        ]
    )
    try:
        job.add(child)
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
        assert handle
        job.close()
        assert kernel.WaitForSingleObject(handle, 5000) == 0
        kernel.CloseHandle(handle)
        child.wait(timeout=10)
    finally:
        job.close()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
