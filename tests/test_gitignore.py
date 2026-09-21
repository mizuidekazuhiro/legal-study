from pathlib import Path
from subprocess import run

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    [
        "source.pdf",
        ".legal-study/state.sqlite3",
        "sources/sha256/abc.pdf",
        "custom-output/renders/page-0001.png",
        "custom-output/ocr_crops/crop.png",
        "custom-output/review_crops/crop.png",
        "custom-output/orphans/OCR_COMPLETE/ocr.json.abc.orphan",
        "custom-output/inspection.json",
        "custom-output/ocr.json",
        "custom-output/run_manifest.json",
        "custom-output/canonical_source.json",
        "criminal_15_problem.md",
        "state.sqlite3-wal",
        ".env.local",
        "credentials-local.json",
        "token-cache.json",
    ],
)
def test_private_generated_artifacts_are_gitignored(relative_path: str) -> None:
    result = run(
        ["git", "check-ignore", "--quiet", "--no-index", relative_path],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"Expected Git to ignore {relative_path}"
