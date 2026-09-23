import pytest
from pydantic import ValidationError

from legal_study.supplemental_retrieval import (
    ExistingProblemNote,
    NotionStatuteRecord,
    ObsidianArgumentPattern,
    SupplementalRetrievalBundle,
    SupplementalSearchAttempt,
    sha256_text,
)


def test_bundle_accepts_only_obsidian_patterns_and_notion_statutes() -> None:
    body = "因果関係の規範本文"
    note = "# 第12問\n既存ノート"
    bundle = SupplementalRetrievalBundle(
        subject="criminal",
        question="p1c6-real-updated",
        search_attempts=[
            SupplementalSearchAttempt(
                source="obsidian_argument_pattern",
                query="因果関係",
                matched_ids=["criminal-causation-001"],
            ),
            SupplementalSearchAttempt(
                source="notion_statute",
                query="刑法199条",
                matched_ids=["notion-page-199"],
            ),
        ],
        argument_patterns=[
            ObsidianArgumentPattern(
                pattern_id="criminal-causation-001",
                aliases=["因果関係", "危険の現実化"],
                title="因果関係",
                body=body,
                related_statutes=["刑法199条"],
                source_path="20_論証パターン/刑法/因果関係.md",
                source_sha256=sha256_text(body),
            )
        ],
        statutes=[
            NotionStatuteRecord(
                record_id="notion-page-199",
                law_name="刑法",
                article="199条",
                text="人を殺した者は、死刑又は無期若しくは五年以上の拘禁刑に処する。",
                notion_url="https://www.notion.so/example",
            )
        ],
        existing_problem_notes=[
            ExistingProblemNote(
                relative_path="30_論文マスター/刑法/第12問.md",
                text=note,
                source_sha256=sha256_text(note),
            )
        ],
    )

    assert bundle.schema_version == "supplemental_retrieval.v1"
    assert bundle.argument_patterns[0].pattern_id == "criminal-causation-001"
    assert bundle.statutes[0].article == "199条"


def test_obsidian_paths_must_be_vault_relative() -> None:
    with pytest.raises(ValidationError, match="vault-relative"):
        ObsidianArgumentPattern(
            pattern_id="x",
            title="x",
            body="x",
            source_path="C:/Obsidian/pattern.md",
            source_sha256="a" * 64,
        )

    with pytest.raises(ValidationError, match="vault-relative"):
        ExistingProblemNote(
            relative_path="../escape.md",
            text="x",
            source_sha256="a" * 64,
        )


def test_no_navigate_pdf_field_is_accepted() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SupplementalRetrievalBundle.model_validate(
            {
                "subject": "criminal",
                "question": "12",
                "navigate_pdf": "論文ナビゲートテキスト_刑法.pdf",
            }
        )
