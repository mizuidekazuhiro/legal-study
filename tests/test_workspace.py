from pathlib import Path

from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source
from legal_study.workspace import run_dir, safe_path_component


def test_portable_workspace_uses_configured_home(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")

    path = run_dir(
        subject="criminal",
        question="15",
        source_sha256="a" * 64,
        input_hash="b" * 64,
        settings=settings,
    )

    assert path.parent == settings.runs_dir / "criminal"
    assert path.name.startswith("15-")
    assert path.exists()


def test_safe_path_component_blocks_parent_and_windows_reserved_names() -> None:
    assert safe_path_component("..", fallback="unknown") == "unknown"
    assert safe_path_component("CON", fallback="unknown") == "_CON"
    assert safe_path_component('criminal:/\\*?"<>|', fallback="unknown") == "criminal---------"


def test_source_snapshot_is_content_addressed_and_immutable(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"first PDF version")
    settings = LocalSettings(home=tmp_path / "home")

    snapshot = snapshot_source(source, settings=settings)

    assert snapshot.snapshot_path.parent == settings.sources_dir
    assert snapshot.snapshot_path.name == f"{snapshot.sha256}.pdf"
    assert snapshot.snapshot_path.read_bytes() == b"first PDF version"

    source.write_bytes(b"second PDF version")
    assert snapshot.snapshot_path.read_bytes() == b"first PDF version"


def test_identical_sources_reuse_the_same_snapshot(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    settings = LocalSettings(home=tmp_path / "home")

    first_snapshot = snapshot_source(first, settings=settings)
    second_snapshot = snapshot_source(second, settings=settings)

    assert first_snapshot.snapshot_path == second_snapshot.snapshot_path
    assert second_snapshot.original_filename == "second.pdf"
