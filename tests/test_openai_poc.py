import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from legal_study.openai_poc import (
    OpenAIPocConfig,
    build_study_draft_bundle_from_run,
    run_openai_study_draft_poc,
)
from legal_study.settings import LocalSettings
from legal_study.state import QuestionStateStore
from legal_study.study_draft import DraftSource
from legal_study.study_draft_request import (
    build_study_draft_request_bundle,
    required_instruction_names,
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
                "reconciled_text": "講師答案本文",
                "repair_review_count": 0,
            }
        ],
        "reconciliation": {"records": []},
        "markers": [],
        "logical_markers": [],
        "ocr_supplements": [],
        "needs_review": [],
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
page_review_count: 0
review_issue_count: 0
---

# Page Reading Pack

## PDF page 110

### Primary Reading Text

講師答案本文
""",
        encoding="utf-8",
    )
    (run_dir / "handoff_review/page-0110-review.png").write_bytes(b"png")

    instruction_dir.mkdir(parents=True)
    for index, name in enumerate(required_instruction_names("criminal"), start=1):
        (instruction_dir / name).write_text(
            f"# instruction {index}\n{name}\n",
            encoding="utf-8",
        )


def _draft_payload(bundle=None) -> dict[str, object]:
    evidence = {
        "evidence_id": "page:110:primary_text",
        "page_number": 110,
        "source_kind": "handoff_primary_text",
        "artifact_path": "criminal_22_handoff.md",
        "source_anchor": "PDF page 110 / Primary Reading Text",
        "review_required": False,
    }
    return {
        "schema_version": "study_draft.v1",
        "subject": "criminal",
        "question": "22",
        "source": {
            "source_sha256": "a" * 64,
            "source_snapshot_path": "C:/Users/test/.legal-study/sources/sha256/source.pdf",
            "stable_page_ids": ["stable-110"],
            "requested_pages": [110],
            "run_id": "run-22",
            "handoff_path": "criminal_22_handoff.md",
            "canonical_source_path": "canonical_source.json",
            "problem_validation_path": "problem_validation.json",
        },
        "instruction_sources": (
            [
                {"name": item.name, "sha256": item.sha256}
                for item in bundle.instructions
            ]
            if bundle is not None
            else [
                {
                    "name": "00_論文作成・登録_本番_プロジェクト指示.md",
                    "sha256": None,
                }
            ]
        ),
        "visual_reviews": [
            {
                "page_number": 110,
                "review_sheet_path": "handoff_review/page-0110-review.png",
                "reviewed": True,
                "unresolved_issue_ids": [],
            }
        ],
        "anki_cards": [
            {
                "name": "刑法第22問-01",
                "subject": "刑法",
                "source": "論文マスター_刑法",
                "pdf_page": "110",
                "topic": "サンプル",
                "subtopic": "サンプル",
                "level": "L4",
                "anki_type": "基本",
                "front": {
                    "text": "事案を踏まえて処理順序を示せ。",
                    "evidence_refs": [evidence],
                    "transformation": "EXTRACTED_REORDERED",
                    "review_required": False,
                },
                "back": {
                    "text": "【回答】<br>資料に基づく回答。",
                    "evidence_refs": [evidence],
                    "transformation": "MINIMAL_CONNECTIVE",
                    "review_required": False,
                },
                "extra": {
                    "text": "【正本｜Obsidian「講師答案例」原文】<br>講師答案本文",
                    "evidence_refs": [evidence],
                    "transformation": "VERBATIM",
                    "review_required": False,
                },
                "anki_tags": ["刑法"],
                "anki_deck": "刑法 論文試験",
                "status": "DRAFT",
                "export_to_anki": False,
            }
        ],
        "obsidian_note": {
            "relative_path": "30_論文マスター/刑法/第22問.md",
            "sections": [
                {
                    "heading": "講師答案例",
                    "blocks": [
                        {
                            "text": "講師答案本文",
                            "evidence_refs": [evidence],
                            "transformation": "VERBATIM",
                            "review_required": False,
                        }
                    ],
                }
            ],
        },
        "unresolved": [],
    }


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


class FakeResponses:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.responses = FakeResponses(response)


def _response(*, status: str = "completed", output_text: str | None = None):
    return SimpleNamespace(
        id="resp_test_123",
        status=status,
        model="gpt-5.6-2026-09-15",
        output_text=output_text or "",
        output=[],
        incomplete_details=(
            SimpleNamespace(reason="max_output_tokens")
            if status == "incomplete"
            else None
        ),
        usage=SimpleNamespace(
            input_tokens=1234,
            output_tokens=567,
            total_tokens=1801,
        ),
    )


def test_one_question_poc_calls_responses_once_and_accepts_candidate(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    client = FakeClient(
        _response(
            output_text=json.dumps(_draft_payload(bundle), ensure_ascii=False),
        )
    )

    result = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=client,
        config=OpenAIPocConfig(),
    )

    assert result.called is True
    assert result.response_id == "resp_test_123"
    assert result.response_status == "completed"
    assert result.accepted is True
    assert result.study_draft_path == "study_draft.json"
    assert result.raw_response_path == "study_draft_api_raw_response.json"
    assert result.acceptance_issues == []
    assert len(client.responses.calls) == 1

    request = client.responses.calls[0]
    assert request["model"] == "gpt-5.6"
    assert request["reasoning"] == {"effort": "high", "mode": "standard"}
    assert request["store"] is False
    assert request["max_output_tokens"] == 64000
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["input"][0]["content"][1]["type"] == "input_image"

    receipt = json.loads(
        (run_dir / "study_draft_api_receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["response_id"] == "resp_test_123"
    assert receipt["accepted"] is True
    assert receipt["usage"]["total_tokens"] == 1801
    assert receipt["raw_response_path"] == "study_draft_api_raw_response.json"
    assert receipt["acceptance_issues"] == []
    assert (run_dir / "study_draft_api_raw_response.json").is_file()


def test_incomplete_response_is_rejected_without_study_draft(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    client = FakeClient(_response(status="incomplete"))

    result = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=client,
    )

    assert result.called is True
    assert result.accepted is False
    assert result.reason == "RESPONSE_INCOMPLETE"
    assert not (run_dir / "study_draft.json").exists()
    assert (run_dir / "study_draft_api_receipt.json").is_file()


def test_refusal_is_rejected_without_study_draft(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    response = _response(status="completed")
    response.output = [
        SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="refusal", refusal="cannot comply")],
        )
    ]
    client = FakeClient(response)

    result = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=client,
    )

    assert result.accepted is False
    assert result.reason == "RESPONSE_REFUSAL"
    assert result.refusal == "cannot comply"
    assert not (run_dir / "study_draft.json").exists()


def test_invalid_model_output_flows_through_acceptance_gate(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    raw_output = '{"not":"study-draft"}'
    client = FakeClient(_response(output_text=raw_output))

    result = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=client,
    )

    assert result.accepted is False
    assert result.reason == "DRAFT_REJECTED"
    assert "SCHEMA_VALIDATION_FAILED" in result.acceptance_issue_codes
    assert result.raw_response_path == "study_draft_api_raw_response.json"
    assert len(result.acceptance_issues) == 1
    assert result.acceptance_issues[0].code == "SCHEMA_VALIDATION_FAILED"
    assert "schema_version" in result.acceptance_issues[0].message
    assert (run_dir / "study_draft_api_raw_response.json").read_text(
        encoding="utf-8"
    ) == raw_output

    receipt = json.loads(
        (run_dir / "study_draft_api_receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["raw_response_path"] == "study_draft_api_raw_response.json"
    assert receipt["acceptance_issues"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert "schema_version" in receipt["acceptance_issues"][0]["message"]
    assert receipt["acceptance_issues"][0]["location"] is None
    assert not (run_dir / "study_draft.json").exists()


def test_existing_receipt_prevents_accidental_second_api_call(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    first_client = FakeClient(
        _response(output_text=json.dumps(_draft_payload(bundle), ensure_ascii=False))
    )
    first = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=first_client,
    )
    assert first.accepted is True

    second_client = FakeClient(
        _response(output_text=json.dumps(_draft_payload(bundle), ensure_ascii=False))
    )
    with pytest.raises(RuntimeError, match="API receipt already exists"):
        run_openai_study_draft_poc(
            bundle=bundle,
            run_dir=run_dir,
            client=second_client,
        )

    assert second_client.responses.calls == []


def test_network_exception_records_failed_receipt(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)

    class FailingResponses:
        def create(self, **kwargs):
            raise RuntimeError("network exploded")

    client = SimpleNamespace(responses=FailingResponses())

    with pytest.raises(RuntimeError, match="network exploded"):
        run_openai_study_draft_poc(
            bundle=bundle,
            run_dir=run_dir,
            client=client,
        )

    receipt = json.loads(
        (run_dir / "study_draft_api_receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["accepted"] is False
    assert receipt["reason"] == "API_CALL_FAILED"
    assert "network exploded" in receipt["error"]


def test_bundle_can_be_reconstructed_from_run_manifest_and_question_state(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    instruction_dir = tmp_path / "instructions"
    run_dir.mkdir()
    _write_inputs(run_dir, instruction_dir)

    home = tmp_path / "home"
    settings = LocalSettings(home=home)
    settings.ensure()
    QuestionStateStore(settings.state_db).ensure_in_progress(
        "criminal",
        "22",
        latest_source_sha256="a" * 64,
        stable_page_ids=["stable-110"],
    )

    manifest = {
        "schema_version": "1",
        "run_id": "run-22",
        "input_hash": "b" * 64,
        "created_at": "2026-09-23T00:00:00+00:00",
        "updated_at": "2026-09-23T00:00:00+00:00",
        "subject": "criminal",
        "question": "22",
        "requested_pages": [110],
        "source": {
            "original_path": "C:/study/論文マスター_刑法.pdf",
            "original_filename": "論文マスター_刑法.pdf",
            "source_size": 123,
            "source_mtime_ns": 456,
            "sha256": "a" * 64,
            "snapshot_path": str(home / "sources/sha256/source.pdf"),
            "snapshot_created_at": "2026-09-23T00:00:00+00:00",
        },
        "output_dir": ".",
        "page_count": 200,
        "pipeline_config": {},
        "app_version": "0.1.0",
        "python_version": "3.12.0",
        "platform": "test",
        "pymupdf_version": "1.28.2",
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    bundle = build_study_draft_bundle_from_run(
        run_dir=run_dir,
        instruction_dir=instruction_dir,
        settings=settings,
    )

    assert bundle.subject == "criminal"
    assert bundle.question == "22"
    assert bundle.source.stable_page_ids == ["stable-110"]
    assert bundle.source.source_sha256 == "a" * 64
    assert bundle.source.handoff_path == "criminal_22_handoff.md"


def test_response_source_must_match_request_bundle(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    payload = _draft_payload(bundle)
    payload["source"]["stable_page_ids"] = ["invented-stable-id"]
    client = FakeClient(_response(output_text=json.dumps(payload, ensure_ascii=False)))

    result = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=client,
    )

    assert result.accepted is False
    assert result.reason == "DRAFT_BUNDLE_MISMATCH"
    assert "BUNDLE_SOURCE_MISMATCH" in result.acceptance_issue_codes
    assert not (run_dir / "study_draft.json").exists()


def test_response_instruction_sources_must_match_request_bundle(tmp_path: Path) -> None:
    run_dir, bundle = _bundle(tmp_path)
    payload = _draft_payload(bundle)
    payload["instruction_sources"] = payload["instruction_sources"][:-1]
    client = FakeClient(_response(output_text=json.dumps(payload, ensure_ascii=False)))

    result = run_openai_study_draft_poc(
        bundle=bundle,
        run_dir=run_dir,
        client=client,
    )

    assert result.accepted is False
    assert result.reason == "DRAFT_BUNDLE_MISMATCH"
    assert "BUNDLE_INSTRUCTION_SOURCES_MISMATCH" in result.acceptance_issue_codes
    assert not (run_dir / "study_draft.json").exists()
