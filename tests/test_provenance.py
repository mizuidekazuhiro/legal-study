import legal_study.provenance as provenance
from legal_study.provenance import resolve_code_revision


def test_code_revision_prefers_explicit_environment(monkeypatch) -> None:
    commit = "a" * 40
    monkeypatch.setenv("LEGAL_STUDY_GIT_COMMIT", commit)

    revision = resolve_code_revision()

    assert revision.sha == commit
    assert revision.source == "environment"
    assert revision.dirty is None


def test_code_revision_reports_clean_git_worktree(monkeypatch) -> None:
    commit = "b" * 40

    def fake_run_git(*args: str) -> str | None:
        if args == ("rev-parse", "HEAD"):
            return commit
        if args == ("status", "--porcelain"):
            return ""
        raise AssertionError(args)

    monkeypatch.delenv("LEGAL_STUDY_GIT_COMMIT", raising=False)
    monkeypatch.setattr(provenance, "_run_git", fake_run_git)

    revision = resolve_code_revision()

    assert revision.sha == commit
    assert revision.source == "git"
    assert revision.dirty is False
