"""Read-only setup and normal-startup contract checks."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from legal_study.cli import app

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_chat_bridge_setup.ps1"
STARTER = REPO_ROOT / "scripts" / "start_chat_bridge.ps1"


def test_doctor_does_not_create_workspace(tmp_path: Path) -> None:
    home = tmp_path / "missing-home"
    result = CliRunner().invoke(app, ["doctor"], env={"LEGAL_STUDY_HOME": str(home)})

    assert result.exit_code == 0
    assert not home.exists()


def test_normal_startup_never_enables_pc_notion_or_requires_api_keys() -> None:
    assert STARTER.read_bytes().startswith(b"\xef\xbb\xbf")
    script = STARTER.read_text(encoding="utf-8")
    assert "watch-study" in script
    assert "watch-chat-bridge" in script
    assert "--bridge-root" in script
    assert "--obsidian-inbox" in script
    assert "--enable-notion" not in script
    assert "EnableNotion" not in script
    assert "NOTION_TOKEN" not in script
    assert "OPENAI_API_KEY" not in script
    assert "-WindowStyle Hidden" in script
    assert "LEGAL_STUDY_CHAT_BRIDGE_ROOT" in script
    assert "LEGAL_STUDY_OBSIDIAN_INBOX" in script


def test_checker_is_windows_powershell_unicode_safe() -> None:
    assert CHECKER.read_bytes().startswith(b"\xef\xbb\xbf")


def test_readme_documents_normal_action_and_direct_notion() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "start_chat_bridge.ps1" in readme
    assert "apply_obsidian" in readme
    assert "ChatGPT通常チャット" in readme
    assert "接続済みNotion" in readme
    assert "-EnableNotion" not in readme


def test_packet_normal_contract_only_emits_obsidian_command() -> None:
    source = (REPO_ROOT / "src/legal_study/chat_packet.py").read_text(encoding="utf-8")
    assert '"allowed_actions": [BridgeAction.APPLY_OBSIDIAN.value]' in source
    assert "接続済みNotion" in source


def _run_checker(tmp_path: Path, *, omit: str | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable on this platform")

    paths = {
        "RepoRoot": tmp_path / "repo",
        "PythonPath": tmp_path / ("fake-python.cmd" if os.name == "nt" else "fake-python.sh"),
        "HomePath": tmp_path / "home",
        "BridgeRoot": tmp_path / "LegalStudy_ChatBridge",
        "ObsidianInbox": tmp_path / "Obsidian_Inbox",
        "ObsidianVault": tmp_path / "vault",
    }
    for name in ("RepoRoot", "HomePath", "BridgeRoot", "ObsidianInbox", "ObsidianVault"):
        if name != omit:
            paths[name].mkdir()
    (paths["RepoRoot"] / "scripts").mkdir(exist_ok=True)
    (paths["RepoRoot"] / "scripts" / "start_chat_bridge.ps1").touch()
    subprocess.run(
        ["git", "init", "-q", "-b", "feat/study-automation-v1", str(paths["RepoRoot"])],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(paths["RepoRoot"]), "add", "scripts/start_chat_bridge.ps1"],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(paths["RepoRoot"]), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "test",
        ],
        check=True,
    )
    if omit != "PythonPath":
        if os.name == "nt":
            fake_python = "@echo off\r\necho Offline PaddleOCR: READY\r\n"
        else:
            fake_python = "#!/bin/sh\nprintf 'Offline PaddleOCR: READY\\n'\n"
        paths["PythonPath"].write_text(fake_python, encoding="ascii")
        if os.name != "nt":
            paths["PythonPath"].chmod(0o755)
    (paths["HomePath"] / "state.sqlite3").touch()
    for folder in ("00_pending", "10_approved", "20_commands", "30_receipts", "99_failed"):
        if folder != omit and omit != "BridgeRoot":
            (paths["BridgeRoot"] / folder).mkdir(exist_ok=True)
    before = {path for path in tmp_path.rglob("*")}
    args = [powershell, "-NoProfile"]
    if os.name == "nt":
        args.extend(["-ExecutionPolicy", "Bypass"])
    args.extend(["-File", str(CHECKER)])
    for key, path in paths.items():
        args.extend([f"-{key}", str(path)])
    completed = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert {path for path in tmp_path.rglob("*")} == before
    return completed, paths


def test_checker_reports_ready_for_complete_temp_setup(tmp_path: Path) -> None:
    result, _ = _run_checker(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Overall: READY" in result.stdout
    assert "OCR: READY" in result.stdout


@pytest.mark.parametrize("missing", ["BridgeRoot", "00_pending", "ObsidianInbox", "PythonPath"])
def test_checker_fails_without_creating_missing_path(tmp_path: Path, missing: str) -> None:
    result, paths = _run_checker(tmp_path, omit=missing)
    assert result.returncode != 0
    assert "Overall: NG" in result.stdout
    if missing in paths:
        assert not paths[missing].exists()
    else:
        assert not (paths["BridgeRoot"] / missing).exists()
