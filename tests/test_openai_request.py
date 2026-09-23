import base64
import json
from pathlib import Path

import pytest

from legal_study.openai_request import (
    build_openai_responses_request_template,
    make_openai_strict_schema,
)
from legal_study.study_draft import DraftSource
from legal_study.study_draft_request import (
    build_study_draft_request_bundle,
    required_instruction_names,
)
from legal_study.supplemental_retrieval import (
    NotionStatuteRecord,
    ObsidianArgumentPattern,
    SupplementalRetrievalBundle,
    SupplementalSearchAttempt,
    sha256_text,
)


def _source() -> DraftSource:
    return DraftSource(
        source_sha256="a" * 64,
        source_snapshot_path="C:/Users/test/.legal-study/sources/sha256/source.pdf",
        stable_page_ids=["stable-110"],
        requested_pages=[110],
        run_id="run-22",
        handoff_path="criminal_22_handoff.md",
        canonical_source_path="canonical_source.json",
        problem_validation_path="problem_validation.json",
    )


def _write_inputs(run_dir: Path, instruction_dir: Path) -> None:
    (run_dir / "handoff_review").mkdir(parents=True)
    canonical = {
        "schema_version": 3,
        "subject": "criminal",
        "question": "22",
        "source": {
            "filename": "論文マスター_刑法.pdf",
            "sha256": "a" * 64,
            "requested_pages": [110],
        },
        "pages": [
            {
                "page_number": 110,
                "repair_review_count": 1,
            }
        ],
        "handoff_review_sheets": {
            "110": "handoff_review/page-0110-review.png",
        },
    }
    (run_dir / "canonical_source.json").write_text(
        json.dumps(canonical, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "problem_validation.json").write_text(
        json.dumps({"valid": True}),
        encoding="utf-8",
    )
    (run_dir / "criminal_22_handoff.md").write_text(
        """---
handoff_schema_version: 2
subject: criminal
question: "22"
source_file: 論文マスター_刑法.pdf
source_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
source_pages: [110]
page_review_count: 1
review_issue_count: 1
---

# Page Reading Pack

## PDF page 110

### Primary Reading Text

講師答案本文
""",
        encoding="utf-8",
    )
    (run_dir / "handoff_review/page-0110-review.png").write_bytes(
        b"review-sheet-110"
    )

    instruction_dir.mkdir(parents=True)
    for index, name in enumerate(required_instruction_names("criminal"), start=1):
        (instruction_dir / name).write_text(
            f"# instruction {index}\n{name}\n",
            encoding="utf-8",
        )


def _bundle(tmp_path: Path):
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)
    bundle = build_study_draft_request_bundle(
        subject="criminal",
        question="22",
        source=_source(),
        run_dir=run_dir,
        instruction_dir=instruction_dir,
    )
    return run_dir, bundle


def test_request_template_matches_responses_multimodal_shape(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)

    request = build_openai_responses_request_template(
        bundle=bundle,
        run_dir=run_dir,
    )

    payload = request.payload
    assert request.api == "responses"
    assert "model" not in payload
    assert payload["instructions"].startswith(
        "You are generating exactly one study_draft.json candidate."
    )

    input_items = payload["input"]
    assert len(input_items) == 1
    assert input_items[0]["role"] == "user"
    content = input_items[0]["content"]
    assert content[0]["type"] == "input_text"
    assert "source_sha256: " + "a" * 64 in content[0]["text"]
    assert "page:110:primary_text" in content[0]["text"]
    assert "review_required=true" in content[0]["text"]
    assert "review-sheet:110" in content[0]["text"]

    image = content[1]
    assert image["type"] == "input_image"
    assert image["detail"] == "auto"
    prefix = "data:image/png;base64,"
    assert image["image_url"].startswith(prefix)
    assert base64.b64decode(image["image_url"][len(prefix) :]) == b"review-sheet-110"

    output_format = payload["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["name"] == "study_draft"
    assert output_format["strict"] is True
    assert output_format["schema"]["type"] == "object"
    assert len(request.response_schema_sha256) == 64


def test_governing_instruction_documents_are_preserved_verbatim(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)

    request = build_openai_responses_request_template(
        bundle=bundle,
        run_dir=run_dir,
    )

    rendered = request.payload["instructions"]
    for instruction in bundle.instructions:
        assert instruction.text.rstrip() in rendered
        assert instruction.name in rendered
        assert instruction.sha256 in rendered


def test_request_template_detects_review_sheet_changed_after_bundle(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    (run_dir / "handoff_review/page-0110-review.png").write_bytes(b"changed")

    with pytest.raises(
        ValueError,
        match="Review sheet SHA-256 changed after bundle creation",
    ):
        build_openai_responses_request_template(
            bundle=bundle,
            run_dir=run_dir,
        )


def test_request_template_detects_embedded_instruction_tampering(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    changed = bundle.instructions[0].model_copy(update={"text": "tampered"})
    bundle = bundle.model_copy(
        update={"instructions": [changed, *bundle.instructions[1:]]}
    )

    with pytest.raises(ValueError, match="Embedded text SHA-256 mismatch"):
        build_openai_responses_request_template(
            bundle=bundle,
            run_dir=run_dir,
        )


def test_strict_schema_requires_every_object_property() -> None:
    run_dir_schema = {
        "type": "object",
        "title": "Example",
        "properties": {
            "fixed": {"const": "v1", "default": "v1"},
            "optional": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "default": "x"},
                        },
                    },
                    {"type": "null"},
                ],
                "default": None,
            },
        },
    }

    strict = make_openai_strict_schema(run_dir_schema)

    assert strict["required"] == ["fixed", "optional"]
    assert strict["additionalProperties"] is False
    assert "title" not in strict
    assert "default" not in strict["properties"]["fixed"]
    assert "const" not in strict["properties"]["fixed"]
    assert strict["properties"]["fixed"]["enum"] == ["v1"]

    nested = strict["properties"]["optional"]["anyOf"][0]
    assert nested["required"] == ["name"]
    assert nested["additionalProperties"] is False
    assert "default" not in nested["properties"]["name"]


def test_generated_study_draft_schema_has_no_defaults_or_consts(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)

    request = build_openai_responses_request_template(
        bundle=bundle,
        run_dir=run_dir,
        image_detail="original",
    )
    schema = request.payload["text"]["format"]["schema"]

    def walk(node):
        if isinstance(node, dict):
            assert "default" not in node
            assert "const" not in node
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert node["required"] == list(properties)
                assert node["additionalProperties"] is False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert request.payload["input"][0]["content"][1]["detail"] == "original"


def test_request_schema_binds_immutable_source_values(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)

    request = build_openai_responses_request_template(
        bundle=bundle,
        run_dir=run_dir,
    )
    schema = request.payload["text"]["format"]["schema"]
    assert schema["properties"]["subject"]["enum"] == [bundle.subject]
    assert schema["properties"]["question"]["enum"] == [bundle.question]

    source = schema["$defs"]["DraftSource"]["properties"]

    assert source["source_sha256"]["enum"] == [bundle.source.source_sha256]
    assert source["source_snapshot_path"]["enum"] == [
        bundle.source.source_snapshot_path
    ]
    assert source["run_id"]["enum"] == [bundle.source.run_id]
    assert source["handoff_path"]["enum"] == [bundle.source.handoff_path]
    assert source["canonical_source_path"]["enum"] == [
        "canonical_source.json"
    ]
    assert source["problem_validation_path"]["enum"] == [
        "problem_validation.json"
    ]


def test_request_renders_obsidian_patterns_and_notion_statutes_without_navigate_pdf(
    tmp_path: Path,
) -> None:
    run_dir, base_bundle = _bundle(tmp_path)
    pattern_body = "危険の現実化を基準として因果関係を判断する。"
    supplemental = SupplementalRetrievalBundle(
        subject="criminal",
        question="22",
        search_attempts=[
            SupplementalSearchAttempt(
                source="obsidian_argument_pattern",
                query="因果関係",
                matched_ids=["刑001"],
            )
        ],
        argument_patterns=[
            ObsidianArgumentPattern(
                pattern_id="刑001",
                aliases=["因果関係"],
                title="因果関係",
                body=pattern_body,
                related_statutes=["刑法199条"],
                source_path="20_論証パターン/刑法/刑001_因果関係.md",
                source_sha256=sha256_text(pattern_body),
            )
        ],
        statutes=[
            NotionStatuteRecord(
                record_id="statute-199",
                law_name="刑法",
                article="199条",
                text="人を殺した者は、死刑又は無期若しくは五年以上の拘禁刑に処する。",
                notion_url="https://www.notion.so/statute-199",
            )
        ],
    )
    bundle = base_bundle.model_copy(update={"supplemental": supplemental})

    request = build_openai_responses_request_template(
        bundle=bundle,
        run_dir=run_dir,
    )
    text = request.payload["input"][0]["content"][0]["text"]

    assert "BEGIN OBSIDIAN ARGUMENT PATTERN 刑001" in text
    assert pattern_body in text
    assert "BEGIN NOTION STATUTE statute-199" in text
    assert "論文ナビゲートテキスト PDF" in text
    assert "Do not require or request" in text
