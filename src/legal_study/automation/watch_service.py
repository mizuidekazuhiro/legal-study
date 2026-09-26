"""One logon supervisor; no OCR until the selected PDF changes after startup."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .watch_lock import WatchLock


class ChildJob:
    """Kill only owned children if Task Scheduler terminates this supervisor."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("limits", Limits), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limit = Extended()
        limit.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limit), ctypes.sizeof(limit)):
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, child):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(child._handle))):
            child.terminate()
            child.wait(timeout=10)
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def signature(path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def logger_for(directory, name):
    log = logging.getLogger(name + str(directory))
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = RotatingFileHandler(directory / f"{name}.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stop", action="store_true")
    options = parser.parse_args()
    cfg = json.loads(options.config.read_text(encoding="utf-8-sig"))
    root = Path(cfg["service_directory"])
    root.mkdir(parents=True, exist_ok=True)
    stop_file = root / "stop.request"
    if options.stop:
        stop_file.write_text(str(time.time()), encoding="ascii")
        return 0
    home = Path(cfg["home"])
    try:
        lock = WatchLock(home, "service", home)
    except BlockingIOError:
        return 0
    launched = time.time()
    log = logger_for(root, "supervisor")
    job = ChildJob()
    children = {}
    state = {"pid": os.getpid(), "python": sys.executable, "started_at": launched,
             "status": "waiting_for_paths", "children": {}, "last_error": None}
    pdf = Path(cfg["pdf"])
    baseline = signature(pdf)
    armed = baseline is not None
    previous = {}
    try:
        previous = json.loads((root / "service-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    state["last_error"] = previous.get("last_error")
    if previous.get("pdf") == str(pdf) and previous.get("pdf_signature"):
        baseline = tuple(previous["pdf_signature"])
        armed = True
    state["pdf"] = str(pdf)
    state["study_started"] = bool(previous.get("study_started") and previous.get("pdf") == str(pdf))
    bridge = Path(cfg["bridge_root"])
    inbox = Path(cfg["obsidian_inbox"])
    env = dict(os.environ, LEGAL_STUDY_HOME=str(home), PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    def publish():
        state["heartbeat_at"] = time.time()
        state["children"] = {name: child.pid for name, child in children.items() if child.poll() is None}
        state["pdf_available"] = signature(pdf) is not None
        state["pdf_signature"] = baseline
        state["free_bytes"] = __import__("shutil").disk_usage(home).free
        temp = root / "service-state.next"
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        # Windows readers/antivirus can temporarily deny DELETE sharing.
        for attempt in range(20):
            try:
                os.replace(temp, root / "service-state.json")
                break
            except PermissionError:
                if attempt == 19:
                    log.warning("heartbeat replace deferred: file is being read")
                else:
                    time.sleep(0.05)

    def start(name, arguments):
        childlog = logger_for(root, name)
        child = subprocess.Popen([sys.executable, "-u", "-m", "legal_study", *arguments],
                                 cwd=cfg["repository"], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        job.add(child)
        children[name] = child
        def relay():
            for line in child.stdout:
                line = line.rstrip()
                childlog.info(line[:16384])
                if "Traceback (most recent call last)" in line or '"status":"failed"' in line.lower() or "ERROR" in line:
                    state["last_error"] = {"at": time.time(), "worker": name, "message": line[:500]}
            child.stdout.close()
        threading.Thread(target=relay, daemon=True).start()
        log.info("started %s pid=%s", name, child.pid)

    exit_code = 0
    try:
        log.info("service started; existing PDF is metadata baseline only; no startup OCR")
        while not stopping.is_set():
            if stop_file.exists() and stop_file.stat().st_mtime >= launched:
                break
            for name, child in children.items():
                code = child.poll()
                if code is not None:
                    if code != 0:
                        state["last_error"] = {"at": time.time(), "worker": name, "message": f"exit {code}"}
                    exit_code = 1 if code else 0
                    state["status"] = "failed" if exit_code else "stopped"
                    return exit_code
            if "bridge" not in children and bridge.is_dir() and inbox.is_dir():
                start("bridge", ["watch-chat-bridge", "--bridge-root", str(bridge), "--obsidian-inbox", str(inbox)])
            current = signature(pdf)
            if not armed and current is not None:
                baseline, armed = current, True
            if armed and current is not None and (current != baseline or state["study_started"]) and "study" not in children and bridge.is_dir():
                start("study", ["watch-study", str(pdf), "--subject", cfg.get("subject", "criminal"), "--bridge-root", str(bridge)])
                state["study_started"] = True
            state["status"] = "watching" if "study" in children else ("armed" if armed and "bridge" in children else "waiting_for_paths")
            publish()
            stopping.wait(float(cfg.get("poll_seconds", 30)))
    except Exception as exc:
        exit_code = 1
        state["status"] = "failed"
        state["last_error"] = {"at": time.time(), "worker": "supervisor", "message": str(exc)[:500]}
        log.exception("service failed")
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        job.close()
        state["status"] = "failed" if exit_code else "stopped"
        publish()
        log.info("service stopped exit=%s", exit_code)
        lock.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
