from pathlib import Path

from legal_study.settings import LocalSettings
from legal_study.workspace import run_dir


def test_portable_workspace_uses_configured_home(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"test-pdf-placeholder")
    settings = LocalSettings(home=tmp_path / "home")

    path = run_dir(subject="criminal", question="15", source=source, settings=settings)

    assert path.parent == settings.runs_dir / "criminal"
    assert path.name.startswith("15-")
    assert path.exists()
