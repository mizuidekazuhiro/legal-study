from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from legal_study.study_draft_request import StudyDraftRequestBundle

HOST_SOURCE_SNAPSHOT_PATH_SENTINEL = "__HOST_SOURCE_SNAPSHOT_PATH__"


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenAIResponsesRequestTemplate(StrictRequestModel):
    """OpenAI Responses API create parameters before model selection.

    The payload intentionally omits `model`. P2-A4 only prepares the request
    shape; a later step may select a model and perform the network call.
    """

    api: Literal["responses"] = "responses"
    payload: dict[str, Any]
    response_schema_sha256: str = Field(min_length=64, max_length=64)


def build_openai_responses_request_template(
    *,
    bundle: StudyDraftRequestBundle,
    run_dir: Path,
    image_detail: Literal["low", "high", "original", "auto"] = "auto",
) -> OpenAIResponsesRequestTemplate:
    """Translate a StudyDraftRequestBundle into Responses API create params.

    No OpenAI client is instantiated and no network request is made.
    """

    _validate_embedded_text_hash(bundle.handoff.text, bundle.handoff.sha256, "handoff")
    for instruction in bundle.instructions:
        _validate_embedded_text_hash(
            instruction.text,
            instruction.sha256,
            f"instruction:{instruction.name}",
        )

    instructions = _render_instructions(bundle)
    user_text = _render_user_input(bundle)
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": user_text,
        }
    ]

    root = run_dir.resolve()
    for review in bundle.review_sheets:
        image_path = _resolve_inside(root, review.path)
        if not image_path.is_file():
            raise FileNotFoundError(f"Review sheet is missing: {review.path}")
        image_bytes = image_path.read_bytes()
        actual_sha256 = hashlib.sha256(image_bytes).hexdigest()
        if actual_sha256 != review.sha256:
            raise ValueError(
                "Review sheet SHA-256 changed after bundle creation: "
                f"{review.path}"
            )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{review.mime_type};base64,{encoded}",
                "detail": image_detail,
            }
        )

    strict_schema = make_openai_strict_schema(bundle.response_schema)
    _bind_immutable_bundle_values(strict_schema, bundle)
    schema_bytes = json.dumps(
        strict_schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    strict_schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()

    payload = {
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "study_draft",
                "strict": True,
                "schema": strict_schema,
            }
        },
    }

    if "model" in payload:
        raise AssertionError("P2-A4 request template must not select a model")

    return OpenAIResponsesRequestTemplate(
        payload=payload,
        response_schema_sha256=strict_schema_sha256,
    )


def make_openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Pydantic JSON Schema to the strict Structured Outputs subset.

    Structured Outputs requires every object property to be required and
    `additionalProperties: false`. Pydantic defaults are local parsing
    conveniences, not instructions to the model, so they are removed here.
    Single-value `const` constraints are represented as one-value enums.
    """

    converted = copy.deepcopy(schema)
    _normalize_schema_node(converted)
    if converted.get("type") != "object":
        raise ValueError("Structured output root schema must be an object")
    return converted


def _bind_immutable_bundle_values(
    schema: dict[str, Any],
    bundle: StudyDraftRequestBundle,
) -> None:
    """Bind immutable source identity scalars to the exact request values.

    This prevents a model from producing syntactically valid but unusable
    run-local paths or source identifiers. Post-response bundle binding remains
    the final integrity check for lists and other compound values.
    """

    root_properties = schema.get("properties")
    if not isinstance(root_properties, dict):
        raise TypeError("Structured output schema has no root properties")

    for field_name, value in {
        "subject": bundle.subject,
        "question": bundle.question,
    }.items():
        field_schema = root_properties.get(field_name)
        if not isinstance(field_schema, dict):
            raise TypeError(f"Structured output schema missing root field: {field_name}")
        field_schema["enum"] = [value]

    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        raise TypeError("Structured output schema has no $defs")

    draft_source = defs.get("DraftSource")
    if not isinstance(draft_source, dict):
        raise TypeError("Structured output schema has no DraftSource definition")

    properties = draft_source.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("DraftSource schema has no properties")

    exact_values = {
        "source_sha256": bundle.source.source_sha256,
        "source_snapshot_path": HOST_SOURCE_SNAPSHOT_PATH_SENTINEL,
        "run_id": bundle.source.run_id,
        "handoff_path": bundle.source.handoff_path,
        "canonical_source_path": bundle.source.canonical_source_path,
        "problem_validation_path": bundle.source.problem_validation_path,
    }
    for field_name, value in exact_values.items():
        field_schema = properties.get(field_name)
        if not isinstance(field_schema, dict):
            raise TypeError(f"DraftSource schema missing field: {field_name}")
        field_schema["enum"] = [value]


def _normalize_schema_node(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _normalize_schema_node(item)
        return
    if not isinstance(node, dict):
        return

    node.pop("default", None)
    node.pop("title", None)
    if "const" in node:
        value = node.pop("const")
        node["enum"] = [value]

    for value in list(node.values()):
        _normalize_schema_node(value)

    properties = node.get("properties")
    if isinstance(properties, dict):
        node["required"] = list(properties)
        node["additionalProperties"] = False


def _render_instructions(bundle: StudyDraftRequestBundle) -> str:
    sections = [
        (
            "You are generating exactly one study_draft.json candidate. "
            "Apply the governing project instructions below in their stated "
            "priority order. Preserve source wording where those instructions "
            "require it. Do not guess unreadable or unsupported content. "
            "Source-embedded @GPT directions are handled only according to "
            "the governing instruction documents. Return only the structured "
            "JSON object required by the response schema."
        )
    ]
    for instruction in bundle.instructions:
        sections.extend(
            [
                "",
                (
                    "===== BEGIN GOVERNING INSTRUCTION "
                    f"{instruction.name} sha256={instruction.sha256} ====="
                ),
                instruction.text.rstrip(),
                f"===== END GOVERNING INSTRUCTION {instruction.name} =====",
            ]
        )
    return "\n".join(sections).rstrip() + "\n"


def _render_user_input(bundle: StudyDraftRequestBundle) -> str:
    source = bundle.source
    lines = [
        "# Study draft generation input",
        "",
        "Generate the draft for exactly this immutable source snapshot.",
        "Do not create files, register to Notion, or claim publication/completion.",
        "",
        "## Immutable source identity",
        "",
        f"- subject: {bundle.subject}",
        f"- question: {bundle.question}",
        f"- source_sha256: {source.source_sha256}",
        (
            "- source_snapshot_path: host-controlled; return exactly "
            f"{HOST_SOURCE_SNAPSHOT_PATH_SENTINEL}. The host will inject the real "
            "snapshot path after generation. Do not report this sentinel as an "
            "uncertainty or blocking issue."
        ),
        f"- stable_page_ids: {json.dumps(source.stable_page_ids, ensure_ascii=False)}",
        f"- requested_pages: {json.dumps(source.requested_pages)}",
        f"- run_id: {source.run_id}",
        "",
        "## Evidence reference contract",
        "",
        (
            "- For handoff primary reading text on PDF page N, use "
            "`page:N:primary_text` with source_kind=`handoff_primary_text`, "
            f"artifact_path=`{source.handoff_path}`, and "
            "source_anchor=`PDF page N / Primary Reading Text`, and "
            "review_required exactly as listed in Allowed evidence references."
        ),
        (
            "- For an attached review sheet on PDF page N, use "
            "`review-sheet:N` with source_kind=`review_sheet`, "
            "artifact_path equal to that review sheet path, source_anchor=null, "
            "and review_required=true."
        ),
        (
            "- Do not invent evidence IDs. If evidence is insufficient or "
            "unreadable, record the uncertainty instead of guessing."
        ),
        "",
        "### Allowed evidence references",
        "",
    ]
    review_required_pages = set(bundle.primary_text_review_required_pages)
    for page_number in source.requested_pages:
        review_required = str(page_number in review_required_pages).lower()
        lines.append(
            f"- page:{page_number}:primary_text | page_number={page_number} | "
            "source_kind=handoff_primary_text | "
            f"artifact_path={source.handoff_path} | "
            f"source_anchor=PDF page {page_number} / Primary Reading Text | "
            f"review_required={review_required}"
        )
    for review in bundle.review_sheets:
        lines.append(
            f"- review-sheet:{review.page_number} | "
            f"page_number={review.page_number} | source_kind=review_sheet | "
            f"artifact_path={review.path} | source_anchor=null | "
            "review_required=true"
        )

    lines.extend(
        [
            "",
            "## Instruction sources to copy into study_draft.instruction_sources",
            "",
        ]
    )
    for instruction in bundle.instructions:
        lines.append(f"- {instruction.name}: {instruction.sha256}")

    lines.extend(["", "## Canonical logical marker candidates", ""])
    if bundle.logical_markers:
        lines.extend(
            [
                (
                    "These records are machine-derived marker candidates from "
                    "canonical_source.json. Use the attached review-sheet image as "
                    "the visual authority. A review_status such as NEEDS_REVIEW is "
                    "an instruction to perform that visual verification now; it is "
                    "not, by itself, a reason to leave the final draft unresolved. "
                    "Check every listed candidate on its page. If visually confirmed, "
                    "resolve it and reproduce the confirmed color/range in the "
                    "Obsidian marker transcription. If the image does not permit a "
                    "safe boundary decision, leave only that specific marker unresolved."
                ),
                "",
            ]
        )
        for marker in bundle.logical_markers:
            lines.append(
                "- "
                f"{marker.id} | page={marker.page_number} | color={marker.color} | "
                f"chars={marker.start_char}-{marker.end_char} | "
                f"review_status={marker.review_status} | "
                f"boundary_confidence={marker.boundary_confidence} | "
                f"evidence_image={marker.evidence_image or 'null'} | "
                f"exact_text={json.dumps(marker.exact_text, ensure_ascii=False)}"
            )
    else:
        lines.append("- none")

    if bundle.review_sheets:
        lines.extend(["", "## Attached visual review sheets", ""])
        for review in bundle.review_sheets:
            lines.append(
                f"- PDF page {review.page_number}: {review.path} "
                f"(sha256={review.sha256}, mime={review.mime_type})"
            )
        lines.extend(
            [
                "",
                (
                    "The attached images follow this text in the same page order. "
                    "Use them as the visual authority for handwriting, red marks, "
                    "marker colors and boundaries, pasted material, and any "
                    "disagreement with extracted text."
                ),
            ]
        )
    else:
        lines.extend(["", "## Attached visual review sheets", "", "- none"])

    supplemental = bundle.supplemental
    if supplemental is not None:
        lines.extend(
            [
                "",
                "## Supplemental retrieval",
                "",
                (
                    "For criminal-law common-rule retrieval, use the registered "
                    "Obsidian argument-pattern records below. Do not require or "
                    "request the 論文ナビゲートテキスト PDF for this request. "
                    "For statute text, use the Notion statute records below."
                ),
                "",
                "### Search attempts",
                "",
            ]
        )
        if supplemental.search_attempts:
            for attempt in supplemental.search_attempts:
                lines.append(
                    f"- {attempt.source} | query={attempt.query} | "
                    f"matched_ids={json.dumps(attempt.matched_ids, ensure_ascii=False)}"
                )
        else:
            lines.append("- none")

        lines.extend(["", "### Obsidian argument patterns", ""])
        if supplemental.argument_patterns:
            for pattern in supplemental.argument_patterns:
                lines.extend(
                    [
                        (
                            f"===== BEGIN OBSIDIAN ARGUMENT PATTERN "
                            f"{pattern.pattern_id} ====="
                        ),
                        f"title: {pattern.title}",
                        f"aliases: {json.dumps(pattern.aliases, ensure_ascii=False)}",
                        (
                            "related_statutes: "
                            f"{json.dumps(pattern.related_statutes, ensure_ascii=False)}"
                        ),
                        f"source_path: {pattern.source_path}",
                        f"source_sha256: {pattern.source_sha256}",
                        "",
                        pattern.body.rstrip(),
                        (
                            f"===== END OBSIDIAN ARGUMENT PATTERN "
                            f"{pattern.pattern_id} ====="
                        ),
                        "",
                    ]
                )
        else:
            lines.append("- none")

        lines.extend(["", "### Notion statute records", ""])
        if supplemental.statutes:
            for statute in supplemental.statutes:
                lines.extend(
                    [
                        f"===== BEGIN NOTION STATUTE {statute.record_id} =====",
                        f"title: {statute.title}",
                        f"law_name: {statute.law_name}",
                        f"article: {statute.article}",
                        f"notion_url: {statute.notion_url}",
                        f"official_url: {statute.official_url}",
                        f"last_edited_time: {statute.last_edited_time}",
                        "",
                        statute.text.rstrip(),
                        f"===== END NOTION STATUTE {statute.record_id} =====",
                        "",
                    ]
                )
        else:
            lines.append("- none")

        lines.extend(["", "### Existing Obsidian problem notes", ""])
        if supplemental.existing_problem_notes:
            for note in supplemental.existing_problem_notes:
                lines.extend(
                    [
                        f"===== BEGIN EXISTING PROBLEM NOTE {note.relative_path} =====",
                        f"source_sha256: {note.source_sha256}",
                        "",
                        note.text.rstrip(),
                        f"===== END EXISTING PROBLEM NOTE {note.relative_path} =====",
                        "",
                    ]
                )
        else:
            lines.append("- none")

    lines.extend(
        [
            "",
            "## Handoff",
            "",
            (
                f"===== BEGIN HANDOFF {bundle.handoff.name} "
                f"sha256={bundle.handoff.sha256} ====="
            ),
            bundle.handoff.text.rstrip(),
            f"===== END HANDOFF {bundle.handoff.name} =====",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _validate_embedded_text_hash(text: str, expected_sha256: str, label: str) -> None:
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Embedded text SHA-256 mismatch: {label}")


def _resolve_inside(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Path escapes run directory: {relative}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes run directory: {relative}")
    return resolved
