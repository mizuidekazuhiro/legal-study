import re
from pathlib import Path


def test_runtime_does_not_import_deprecated_fitz_module() -> None:
    source_root = Path(__file__).parents[1] / "src" / "legal_study"
    legacy_import = re.compile(r"^\s*(?:import\s+fitz|from\s+fitz\s+import)", re.MULTILINE)

    offenders = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if legacy_import.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
