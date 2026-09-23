from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from legal_study.io_utils import atomic_write_json
from legal_study.openai_request import build_openai_responses_request_template
from legal_study.study_draft_acceptance import accept_study_draft_response
from legal_study.study_draft_request import StudyDraftRequestBundle


class StrictPocModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenAIPocConfig(StrictPocModel):
    model: str = Field(default="gpt-5.6", min_length=1)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "high"
    reasoning_mode: Literal["standard", "pro"] = "standard"
    store: Literal[False] = False
    max_output_tokens: int = Field(default=64000, ge=1024, le=128000)
    image_detail: Literal["low", "high", "original", "auto"] = "original"


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
    usage: dict[str, int] = Field(default_factory=dict)
    receipt_path: str = "study_draft_api_receipt.json"


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

    acceptance = accept_study_draft_response(
        raw_response_text=output_text,
        run_dir=root,
    )
    issue_codes = [issue.code for issue in acceptance.issues]
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


def _default_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI SDK is not installed; install the optional API dependency"
        ) from exc
    return OpenAI()


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
    }
    if incomplete_reason is not None:
        payload["incomplete_reason"] = incomplete_reason
    if output_text_sha256 is not None:
        payload["output_text_sha256"] = output_text_sha256
    atomic_write_json(receipt_path, payload)
