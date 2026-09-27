from pathlib import Path


def test_health_script_reports_unavailable_chat_bridge() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "Test-LegalStudyHealth.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "$state.bridge_available -eq $false" in script
    assert "CHAT_BRIDGE_PATH_UNAVAILABLE_IN_USER_SESSION" in script
