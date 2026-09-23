from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from legal_study.io_utils import atomic_write_json, atomic_write_text
from legal_study.openai_request import (
    HOST_SOURCE_SNAPSHOT_PATH_SENTINEL,
    build_openai_responses_request_template,
)
from legal_study.problem_packet import handoff_markdown_filename
from legal_study.run_manifest import RunManifest
from legal_study.settings import LocalSettings
from legal_study.state import QuestionStateStore
from legal_study.study_draft import DraftSource, StudyDraft
from legal_study.study_draft_acceptance import accept_study_draft_response
from legal_study.study_draft_request import (
    StudyDraftRequestBundle,
    build_study_draft_request_bundle,
)


class StrictPocModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenAIPocConfig(StrictPocModel):
    model: str = Field(default="gpt-5.6", min_length=1)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "high"
    reasoning_mode: Literal["standard", "pro"] = "standard"
    store: Literal[False] = False
    background: Literal[True] = True
    max_output_tokens: int = Field(default=64000, ge=1024, le=128000)
    image_detail: Literal["low", "high", "original", "auto"] = "original"
    poll_interval_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    poll_timeout_seconds: float = Field(default=3600.0, ge=30.0, le=7200.0)


class OpenAIPocAcceptanceIssue(StrictPocModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    location: str | None = None


class OpenAIPocResult(StrictPocModel):
    called: bool
    response_id: str | None = None
    response_status: str | None = None
    response_model: str | None = None
    accepted: bool
    reason: str
    refusal: str | None = None
    study_draft_path: str | None = None
    draft_sha256: str | None = None
    acceptance_issue_codes: list[str] = Field(default_factory=list)
    acceptance_issues: list[OpenAIPocAcceptanceIssue] = Field(default_factory=list)
    raw_response_path: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    receipt_path: str = "study_draft_api_receipt.json"



def build_study_draft_bundle_from_run(
    *,
    run_dir: Path,
    instruction_dir: Path,
    settings: LocalSettings | None = None,
) -> StudyDraftRequestBundle:
    """Reconstruct the immutable LLM input bundle for one completed ingest run."""

    root = run_dir.expanduser().resolve()
    manifest_path = root / "run_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"run_manifest.json not found: {root}")

    manifest = RunManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if not manifest.requested_pages:
        raise RuntimeError("Run manifest has no requested_pages")

    cfg = settings or LocalSettings()
    cfg.ensure()
    question = QuestionStateStore(cfg.state_db).get(
        manifest.subject,
        manifest.question,
    )
    if question is None:
        raise RuntimeError(
            f"Question workflow state not found: {manifest.subject}/{manifest.question}"
        )
    if question.latest_source_sha256 != manifest.source.sha256:
        raise RuntimeError("Question source SHA does not match run manifest")
    if not question.stable_page_ids:
        raise RuntimeError("Question workflow state has no stable_page_ids")

    source = DraftSource(
        source_sha256=manifest.source.sha256,
        source_snapshot_path=str(manifest.source.snapshot_path),
        stable_page_ids=question.stable_page_ids,
        requested_pages=manifest.requested_pages,
        run_id=manifest.run_id,
        handoff_path=handoff_markdown_filename(
            manifest.subject,
            manifest.question,
        ),
        canonical_source_path="canonical_source.json",
        problem_validation_path="problem_validation.json",
    )
    return build_study_draft_request_bundle(
        subject=manifest.subject,
        question=manifest.question,
        source=source,
        run_dir=root,
        instruction_dir=instruction_dir,
    )


def run_openai_study_draft_poc(
    *,
    bundle: StudyDraftRequestBundle,
    run_dir: Path,
    client: Any | None = None,
    config: OpenAIPocConfig | None = None,
) -> OpenAIPocResult:
    """Perform one explicit Responses API call and pass output through P2-A5.

    This function intentionally does not modify the persistent queue or question
    workflow state. A receipt file prevents accidental repeat calls in the same
    run directory.
    """

    root = run_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    cfg = config or OpenAIPocConfig()
    receipt_path = root / "study_draft_api_receipt.json"
    if receipt_path.exists():
        raise RuntimeError(
            "API receipt already exists; refusing a second API call for this run"
        )

    template = build_openai_responses_request_template(
        bundle=bundle,
        run_dir=root,
        image_detail=cfg.image_detail,
    )
    request_payload = dict(template.payload)
    request_payload.update(
        {
            "model": cfg.model,
            "reasoning": {
                "effort": cfg.reasoning_effort,
                "mode": cfg.reasoning_mode,
            },
            "store": cfg.store,
            "background": cfg.background,
            "max_output_tokens": cfg.max_output_tokens,
        }
    )
    request_hash = _request_sha256(request_payload)

    started_at = datetime.now(UTC).isoformat()
    atomic_write_json(
        receipt_path,
        {
            "schema_version": "study_draft_api_receipt.v1",
            "state": "CALLING",
            "started_at": started_at,
            "request_sha256": request_hash,
            "model_requested": cfg.model,
            "reasoning": request_payload["reasoning"],
            "store": cfg.store,
            "background": cfg.background,
            "max_output_tokens": cfg.max_output_tokens,
            "image_detail": cfg.image_detail,
            "accepted": False,
            "reason": "CALLING",
        },
    )

    api_client = client if client is not None else _default_openai_client()

    try:
        response = api_client.responses.create(**request_payload)
    except Exception as exc:
        atomic_write_json(
            receipt_path,
            {
                "schema_version": "study_draft_api_receipt.v1",
                "state": "FAILED",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "request_sha256": request_hash,
                "model_requested": cfg.model,
                "reasoning": request_payload["reasoning"],
                "store": cfg.store,
                "background": cfg.background,
                "max_output_tokens": cfg.max_output_tokens,
                "image_detail": cfg.image_detail,
                "accepted": False,
                "reason": "API_CALL_FAILED",
                "error": repr(exc),
            },
        )
        raise

    response_id = _string_attr(response, "id")
    response_status = _string_attr(response, "status")
    if response_id and response_status in {"queued", "in_progress"}:
        atomic_write_json(
            receipt_path,
            {
                "schema_version": "study_draft_api_receipt.v1",
                "state": "IN_PROGRESS",
                "started_at": started_at,
                "request_sha256": request_hash,
                "model_requested": cfg.model,
                "reasoning": request_payload["reasoning"],
                "store": cfg.store,
                "background": cfg.background,
                "max_output_tokens": cfg.max_output_tokens,
                "image_detail": cfg.image_detail,
                "response_id": response_id,
                "response_status": response_status,
                "accepted": False,
                "reason": "BACKGROUND_IN_PROGRESS",
            },
        )
        poll_started = time.monotonic()
        while response_status in {"queued", "in_progress"}:
            if time.monotonic() - poll_started > cfg.poll_timeout_seconds:
                atomic_write_json(
                    receipt_path,
                    {
                        "schema_version": "study_draft_api_receipt.v1",
                        "state": "POLL_TIMEOUT",
                        "started_at": started_at,
                        "finished_at": datetime.now(UTC).isoformat(),
                        "request_sha256": request_hash,
                        "model_requested": cfg.model,
                        "reasoning": request_payload["reasoning"],
                        "store": cfg.store,
                        "background": cfg.background,
                        "max_output_tokens": cfg.max_output_tokens,
                        "image_detail": cfg.image_detail,
                        "response_id": response_id,
                        "response_status": response_status,
                        "accepted": False,
                        "reason": "BACKGROUND_POLL_TIMEOUT",
                    },
                )
                raise TimeoutError(
                    "Background response polling timed out; "
                    f"response_id={response_id}"
                )
            time.sleep(cfg.poll_interval_seconds)
            try:
                response = api_client.responses.retrieve(response_id)
            except Exception as exc:
                atomic_write_json(
                    receipt_path,
                    {
                        "schema_version": "study_draft_api_receipt.v1",
                        "state": "POLL_FAILED",
                        "started_at": started_at,
                        "finished_at": datetime.now(UTC).isoformat(),
                        "request_sha256": request_hash,
                        "model_requested": cfg.model,
                        "reasoning": request_payload["reasoning"],
                        "store": cfg.store,
                        "background": cfg.background,
                        "max_output_tokens": cfg.max_output_tokens,
                        "image_detail": cfg.image_detail,
                        "response_id": response_id,
                        "response_status": response_status,
                        "accepted": False,
                        "reason": "BACKGROUND_POLL_FAILED",
                        "error": repr(exc),
                    },
                )
                raise
            response_status = _string_attr(response, "status")

    response_id = _string_attr(response, "id")
    response_status = _string_attr(response, "status")
    response_model = _string_attr(response, "model")
    usage = _usage_dict(getattr(response, "usage", None))
    refusal = _extract_refusal(response)

    if refusal is not None:
        result = OpenAIPocResult(
            called=True,
            response_id=response_id,
            response_status=response_status,
            response_model=response_model,
            accepted=False,
            reason="RESPONSE_REFUSAL",
            refusal=refusal,
            usage=usage,
        )
        _write_final_receipt(
            receipt_path=receipt_path,
            started_at=started_at,
            request_hash=request_hash,
            config=cfg,
            result=result,
        )
        return result

    if response_status != "completed":
        reason = "RESPONSE_INCOMPLETE" if response_status == "incomplete" else "RESPONSE_NOT_COMPLETED"
        result = OpenAIPocResult(
            called=True,
            response_id=response_id,
            response_status=response_status,
            response_model=response_model,
            accepted=False,
            reason=reason,
            usage=usage,
        )
        _write_final_receipt(
            receipt_path=receipt_path,
            started_at=started_at,
            request_hash=request_hash,
            config=cfg,
            result=result,
            incomplete_reason=_incomplete_reason(response),
        )
        return result

    output_text = getattr(response, "output_text", None)
    if not isinstance(output_text, str) or not output_text.strip():
        result = OpenAIPocResult(
            called=True,
            response_id=response_id,
            response_status=response_status,
            response_model=response_model,
            accepted=False,
            reason="RESPONSE_NO_OUTPUT",
            usage=usage,
        )
        _write_final_receipt(
            receipt_path=receipt_path,
            started_at=started_at,
            request_hash=request_hash,
            config=cfg,
            result=result,
        )
        return result

    raw_response_path = root / "study_draft_api_raw_response.json"
    atomic_write_text(raw_response_path, output_text)
    raw_response_rel = raw_response_path.name

    host_bound_output_text = _inject_host_source_snapshot_path(
        output_text=output_text,
        bundle=bundle,
    )

    binding_issue_codes = _bundle_binding_issue_codes(
        output_text=host_bound_output_text,
        bundle=bundle,
    )
    if binding_issue_codes:
        result = OpenAIPocResult(
            called=True,
            response_id=response_id,
            response_status=response_status,
            response_model=response_model,
            accepted=False,
            reason="DRAFT_BUNDLE_MISMATCH",
            acceptance_issue_codes=binding_issue_codes,
            raw_response_path=raw_response_rel,
            usage=usage,
        )
        _write_final_receipt(
            receipt_path=receipt_path,
            started_at=started_at,
            request_hash=request_hash,
            config=cfg,
            result=result,
            output_text_sha256=hashlib.sha256(
                output_text.encode("utf-8")
            ).hexdigest(),
        )
        return result

    acceptance = accept_study_draft_response(
        raw_response_text=host_bound_output_text,
        run_dir=root,
    )
    issue_codes = [issue.code for issue in acceptance.issues]
    acceptance_issues = [
        OpenAIPocAcceptanceIssue(
            code=issue.code,
            message=issue.message,
            location=issue.location,
        )
        for issue in acceptance.issues
    ]
    result = OpenAIPocResult(
        called=True,
        response_id=response_id,
        response_status=response_status,
        response_model=response_model,
        accepted=acceptance.accepted,
        reason="DRAFT_ACCEPTED" if acceptance.accepted else "DRAFT_REJECTED",
        study_draft_path=acceptance.output_path,
        draft_sha256=acceptance.draft_sha256,
        acceptance_issue_codes=issue_codes,
        acceptance_issues=acceptance_issues,
        raw_response_path=raw_response_rel,
        usage=usage,
    )
    _write_final_receipt(
        receipt_path=receipt_path,
        started_at=started_at,
        request_hash=request_hash,
        config=cfg,
        result=result,
        output_text_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
    )
    return result



def _inject_host_source_snapshot_path(
    *,
    output_text: str,
    bundle: StudyDraftRequestBundle,
) -> str:
    """Replace only the exact host-path sentinel with the immutable bundle value.

    Invalid JSON or an unexpected model-supplied path is left untouched so the
    normal schema/bundle gates reject it. This avoids trusting model-generated
    filesystem paths while preserving the original raw response for audit.
    """

    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError:
        return output_text
    if not isinstance(payload, dict):
        return output_text
    source = payload.get("source")
    if not isinstance(source, dict):
        return output_text
    if source.get("source_snapshot_path") != HOST_SOURCE_SNAPSHOT_PATH_SENTINEL:
        return output_text

    source["source_snapshot_path"] = bundle.source.source_snapshot_path
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _bundle_binding_issue_codes(
    *,
    output_text: str,
    bundle: StudyDraftRequestBundle,
) -> list[str]:
    try:
        draft = StudyDraft.model_validate_json(output_text)
    except ValidationError:
        return []

    issues: list[str] = []
    if draft.subject != bundle.subject or draft.question != bundle.question:
        issues.append("BUNDLE_IDENTITY_MISMATCH")

    if draft.source.model_dump(mode="json") != bundle.source.model_dump(mode="json"):
        issues.append("BUNDLE_SOURCE_MISMATCH")

    expected_instructions = [
        {"name": item.name, "sha256": item.sha256}
        for item in bundle.instructions
    ]
    actual_instructions = [
        item.model_dump(mode="json")
        for item in draft.instruction_sources
    ]
    if actual_instructions != expected_instructions:
        issues.append("BUNDLE_INSTRUCTION_SOURCES_MISMATCH")

    expected_reviews = [
        (item.page_number, item.path)
        for item in bundle.review_sheets
    ]
    actual_reviews = [
        (item.page_number, item.review_sheet_path)
        for item in draft.visual_reviews
    ]
    if actual_reviews != expected_reviews:
        issues.append("BUNDLE_REVIEW_SHEETS_MISMATCH")

    return issues


def _default_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI SDK is not installed; install the optional API dependency"
        ) from exc
    return OpenAI(timeout=60.0, max_retries=0)


def _request_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_refusal(response: Any) -> str | None:
    for output in getattr(response, "output", []) or []:
        if getattr(output, "type", None) != "message":
            continue
        for content in getattr(output, "content", []) or []:
            if getattr(content, "type", None) == "refusal":
                refusal = getattr(content, "refusal", None)
                if isinstance(refusal, str) and refusal:
                    return refusal
    return None


def _incomplete_reason(response: Any) -> str | None:
    details = getattr(response, "incomplete_details", None)
    value = getattr(details, "reason", None)
    return value if isinstance(value, str) else None


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            result[name] = value
    return result


def _string_attr(value: Any, name: str) -> str | None:
    item = getattr(value, name, None)
    return item if isinstance(item, str) else None


def _write_final_receipt(
    *,
    receipt_path: Path,
    started_at: str,
    request_hash: str,
    config: OpenAIPocConfig,
    result: OpenAIPocResult,
    incomplete_reason: str | None = None,
    output_text_sha256: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": "study_draft_api_receipt.v1",
        "state": "COMPLETED",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "request_sha256": request_hash,
        "model_requested": config.model,
        "reasoning": {
            "effort": config.reasoning_effort,
            "mode": config.reasoning_mode,
        },
        "store": config.store,
        "background": config.background,
        "max_output_tokens": config.max_output_tokens,
        "image_detail": config.image_detail,
        "response_id": result.response_id,
        "response_status": result.response_status,
        "response_model": result.response_model,
        "usage": result.usage,
        "accepted": result.accepted,
        "reason": result.reason,
        "refusal": result.refusal,
        "study_draft_path": result.study_draft_path,
        "draft_sha256": result.draft_sha256,
        "acceptance_issue_codes": result.acceptance_issue_codes,
        "acceptance_issues": [
            issue.model_dump(mode="json")
            for issue in result.acceptance_issues
        ],
        "raw_response_path": result.raw_response_path,
    }
    if incomplete_reason is not None:
        payload["incomplete_reason"] = incomplete_reason
    if output_text_sha256 is not None:
        payload["output_text_sha256"] = output_text_sha256
    atomic_write_json(receipt_path, payload)
