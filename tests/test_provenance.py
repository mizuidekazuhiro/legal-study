from legal_study.provenance import resolve_code_revision


def test_code_revision_prefers_explicit_environment(monkeypatch) -> None:
    commit = "a" * 40
    monkeypatch.setenv("LEGAL_STUDY_GIT_COMMIT", commit)

    revision = resolve_code_revision()

    assert revision.sha == commit
    assert revision.source == "environment"
    assert revision.dirty is None
