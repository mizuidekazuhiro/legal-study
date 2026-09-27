from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from legal_study.io_utils import atomic_write_json
from legal_study.openai_request import OpenAIResponsesRequestTemplate
from legal_study.study_draft_request import StudyDraftRequestBundle


class StrictCostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextCostComponent(StrictCostModel):
    name: str = Field(min_length=1)
    characters: int = Field(ge=0)
    utf8_bytes: int = Field(ge=0)
    estimated_tokens: int | None = Field(default=None, ge=0)


class ImageCostComponent(StrictCostModel):
    page_number: int = Field(ge=1)
    path: str = Field(min_length=1)
    file_bytes: int = Field(ge=0)
    mime_type: str = Field(min_length=1)
    detail: Literal["low", "high", "original", "auto"]


class OpenAIRequestCostDiagnostics(StrictCostModel):
    schema_version: Literal["study_draft_request_cost.v1"] = "study_draft_request_cost.v1"
    model: str = Field(min_length=1)
    tokenizer: str
    token_estimate_scope: str = (
        "Text estimates only. They exclude message-framing overhead, image input tokens, "
        "reasoning tokens, and any server-side accounting adjustments."
    )
    text_components: list[TextCostComponent]
    full_text_estimated_tokens: int | None = Field(default=None, ge=0)
    images: list[ImageCostComponent]
    image_count: int = Field(ge=0)
    total_image_file_bytes: int = Field(ge=0)


def write_openai_request_cost_diagnostics(
    *,
    bundle: StudyDraftRequestBundle,
    template: OpenAIResponsesRequestTemplate,
    run_dir: Path,
    model: str,
    image_detail: Literal["low", "high", "original", "auto"],
    filename: str = "study_draft_request_cost.json",
) -> OpenAIRequestCostDiagnostics:
    diagnostics = build_openai_request_cost_diagnostics(
        bundle=bundle,
        template=template,
        run_dir=run_dir,
        model=model,
        image_detail=image_detail,
    )
    atomic_write_json(
        run_dir.resolve() / filename,
        diagnostics.model_dump(mode="json"),
    )
    return diagnostics


def build_openai_request_cost_diagnostics(
    *,
    bundle: StudyDraftRequestBundle,
    template: OpenAIResponsesRequestTemplate,
    run_dir: Path,
    model: str,
    image_detail: Literal["low", "high", "original", "auto"],
) -> OpenAIRequestCostDiagnostics:
    tokenizer, encode = _tokenizer(model)

    instructions_text = str(template.payload.get("instructions", ""))
    user_text = _user_text(template.payload)
    strict_schema = (
        template.payload.get("text", {})
        .get("format", {})
        .get("schema", {})
    )
    schema_text = json.dumps(
        strict_schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    components: list[TextCostComponent] = [
        _text_component("governing_instructions_full", instructions_text, encode),
    ]
    for instruction in bundle.instructions:
        components.append(
            _text_component(
                f"instruction:{instruction.name}",
                instruction.text,
                encode,
            )
        )
    components.extend(
        [
            _text_component("user_input_full", user_text, encode),
            _text_component("handoff_document", bundle.handoff.text, encode),
            _text_component("response_schema", schema_text, encode),
        ]
    )

    full_text = instructions_text + "\n" + user_text + "\n" + schema_text
    full_text_tokens = encode(full_text) if encode is not None else None

    root = run_dir.resolve()
    images: list[ImageCostComponent] = []
    for review in bundle.review_sheets:
        path = _resolve_inside(root, review.path)
        images.append(
            ImageCostComponent(
                page_number=review.page_number,
                path=review.path,
                file_bytes=path.stat().st_size,
                mime_type=review.mime_type,
                detail=image_detail,
            )
        )

    return OpenAIRequestCostDiagnostics(
        model=model,
        tokenizer=tokenizer,
        text_components=components,
        full_text_estimated_tokens=full_text_tokens,
        images=images,
        image_count=len(images),
        total_image_file_bytes=sum(item.file_bytes for item in images),
    )


def _text_component(
    name: str,
    text: str,
    encode: Any | None,
) -> TextCostComponent:
    return TextCostComponent(
        name=name,
        characters=len(text),
        utf8_bytes=len(text.encode("utf-8")),
        estimated_tokens=encode(text) if encode is not None else None,
    )


def _tokenizer(model: str) -> tuple[str, Any | None]:
    try:
        import tiktoken
    except ImportError:
        return "unavailable (install legal-study[api] for tiktoken estimates)", None

    try:
        encoding = tiktoken.encoding_for_model(model)
        name = getattr(encoding, "name", "model-default")
        return f"{name} via encoding_for_model({model})", lambda text: len(encoding.encode(text))
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
        return (
            "o200k_base fallback (estimate; model-specific encoding unavailable)",
            lambda text: len(encoding.encode(text)),
        )


def _user_text(payload: dict[str, Any]) -> str:
    input_items = payload.get("input", [])
    if not isinstance(input_items, list) or not input_items:
        return ""
    content = input_items[0].get("content", [])
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "input_text":
            text = item.get("text")
            if isinstance(text, str):
                return text
    return ""


def _resolve_inside(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Path escapes run directory: {relative}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes run directory: {relative}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Cost diagnostic image is missing: {relative}")
    return resolved
