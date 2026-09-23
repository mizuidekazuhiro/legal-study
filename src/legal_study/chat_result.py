from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from legal_study.io_utils import atomic_write_json, atomic_write_text, file_sha256
from legal_study.run_manifest import RunManifest


class StrictChatResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatResultSource(StrictChatResultModel):
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    requested_pages: list[int]


class ChatAnkiCard(StrictChatResultModel):
    name: str = Field(min_length=1)
    scope: Literal["problem", "common_rule"]
    learning_type: Literal["A", "B", "C", "D", "E"]
    subject: str = Field(min_length=1)
    source: str = Field(min_length=1)
    pdf_page: str = Field(min_length=1)
    pdf_page_number: int = Field(ge=1)
    topic: str = Field(min_length=1)
    subtopic: str = ""
    level: Literal["L1", "L2", "L3", "L4"]
    anki_type: str = Field(min_length=1)
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)
    extra: str | None = None
    anki_tags: list[str] = Field(default_factory=list)
    anki_deck: str = Field(min_length=1)


class ChatObsidianNote(StrictChatResultModel):
    relative_path: str = Field(min_length=1)
    markdown: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_path(self) -> ChatObsidianNote:
        cleaned = self.relative_path.strip()
        path = Path(cleaned)
        if (
            not cleaned
            or path.is_absolute()
            or PureWindowsPath(cleaned).is_absolute()
            or ".." in path.parts
            or path.suffix.lower() != ".md"
        ):
            raise ValueError("Obsidian relative_path must be a safe vault-relative .md path")
        self.relative_path = path.as_posix()
        return self


class ChatStudyResult(StrictChatResultModel):
    schema_version: Literal["chat_study_result.v1"] = "chat_study_result.v1"
    source: ChatResultSource
    reviewed_pages: list[int]
    unresolved: list[str] = Field(default_factory=list)
    problem_card_extra: str | None = None
    anki_cards: list[ChatAnkiCard] = Field(default_factory=list)
    obsidian_note: ChatObsidianNote


class ChatResultValidationReport(StrictChatResultModel):
    valid: bool
    issues: list[str]
    subject: str
    question: str
    card_count: int
    problem_card_count: int
    common_rule_card_count: int
    reviewed_pages: list[int]


class ChatApplyReport(StrictChatResultModel):
    valid: bool
    expanded_result_path: str
    expanded_result_sha256: str
    obsidian_candidate_path: str
    obsidian_candidate_sha256: str
    inbox_path: str | None = None
    inbox_status: Literal["not_requested", "created", "updated", "identical"] = (
        "not_requested"
    )
    notion_mutated: Literal[False] = False


def validate_chat_result(
    *,
    result_path: Path,
    run_dir: Path,
) -> ChatResultValidationReport:
    result_file = result_path.expanduser().resolve()
    root = run_dir.expanduser().resolve()

    manifest = RunManifest.model_validate_json(
        (root / "run_manifest.json").read_text(encoding="utf-8")
    )
    result = ChatStudyResult.model_validate_json(
        result_file.read_text(encoding="utf-8")
    )

    issues: list[str] = []

    if result.source.subject != manifest.subject:
        issues.append("SOURCE_SUBJECT_MISMATCH")
    if result.source.question != manifest.question:
        issues.append("SOURCE_QUESTION_MISMATCH")
    if result.source.source_sha256 != manifest.source.sha256:
        issues.append("SOURCE_SHA256_MISMATCH")
    if result.source.requested_pages != manifest.requested_pages:
        issues.append("SOURCE_PAGES_MISMATCH")

    requested = list(manifest.requested_pages or [])
    if sorted(set(result.reviewed_pages)) != sorted(set(requested)):
        issues.append("VISUAL_REVIEW_INCOMPLETE")
    if result.unresolved:
        issues.append("UNRESOLVED_ITEMS_REMAIN")

    expected_subject = {
        "criminal": "刑法",
        "constitutional": "憲法",
        "administrative": "行政法",
    }.get(manifest.subject)
    expected_deck = {
        "criminal": "刑法 論文試験",
        "constitutional": "憲法 論文試験",
        "administrative": "行政法 論文試験",
    }.get(manifest.subject)

    for index, card in enumerate(result.anki_cards):
        if expected_subject and card.subject != expected_subject:
            issues.append(f"ANKI_SUBJECT_MISMATCH:{index}")
        if expected_deck and card.anki_deck != expected_deck:
            issues.append(f"ANKI_DECK_MISMATCH:{index}")
        for field_name, value in (
            ("front", card.front),
            ("back", card.back),
        ):
            if "<br>" not in value:
                issues.append(f"ANKI_HTML_BREAK_MISSING:{index}:{field_name}")
        if card.scope == "common_rule" and (
            not card.extra or "<br>" not in card.extra
        ):
            issues.append(f"ANKI_COMMON_EXTRA_INVALID:{index}")

    problem_cards = [card for card in result.anki_cards if card.scope == "problem"]
    common_cards = [card for card in result.anki_cards if card.scope == "common_rule"]

    if problem_cards and (
        not result.problem_card_extra or "<br>" not in result.problem_card_extra
    ):
        issues.append("PROBLEM_CARD_EXTRA_MISSING")

    if manifest.subject == "criminal":
        if not any(
            card.scope == "problem"
            and card.learning_type == "B"
            and card.level == "L4"
            for card in result.anki_cards
        ):
            issues.append("CRIMINAL_L4_B_CARD_MISSING")
        if not problem_cards:
            issues.append("CRIMINAL_PROBLEM_CARDS_MISSING")

    if not result.obsidian_note.markdown.strip():
        issues.append("OBSIDIAN_MARKDOWN_EMPTY")

    return ChatResultValidationReport(
        valid=not issues,
        issues=issues,
        subject=manifest.subject,
        question=manifest.question,
        card_count=len(result.anki_cards),
        problem_card_count=len(problem_cards),
        common_rule_card_count=len(common_cards),
        reviewed_pages=result.reviewed_pages,
    )


def expanded_anki_cards(result: ChatStudyResult) -> list[dict[str, object]]:
    """Expand the shared problem-card Extra for downstream registration/export."""

    expanded: list[dict[str, object]] = []
    for card in result.anki_cards:
        payload = card.model_dump(mode="json")
        if card.scope == "problem":
            payload["extra"] = result.problem_card_extra
        expanded.append(payload)
    return expanded


def load_chat_result(path: Path) -> ChatStudyResult:
    return ChatStudyResult.model_validate_json(
        path.expanduser().resolve().read_text(encoding="utf-8")
    )


def dump_expanded_result(result: ChatStudyResult) -> str:
    payload = result.model_dump(mode="json")
    payload["anki_cards"] = expanded_anki_cards(result)
    payload.pop("problem_card_extra", None)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def apply_chat_result(
    *,
    result_path: Path,
    run_dir: Path,
    obsidian_inbox: Path | None = None,
    update_existing: bool = False,
) -> ChatApplyReport:
    """Materialize a validated Chat result locally without touching Notion.

    The expanded JSON and an Obsidian candidate are always written inside the run.
    Inbox writing is opt-in and refuses a differing existing file unless the caller
    explicitly enables update_existing.
    """

    root = run_dir.expanduser().resolve()
    report = validate_chat_result(result_path=result_path, run_dir=root)
    if not report.valid:
        raise RuntimeError(
            "Chat result validation failed: " + ", ".join(report.issues)
        )

    result = load_chat_result(result_path)

    expanded_path = root / "chat_result_expanded.json"
    expanded_payload = result.model_dump(mode="json")
    expanded_payload["anki_cards"] = expanded_anki_cards(result)
    expanded_payload.pop("problem_card_extra", None)
    atomic_write_json(expanded_path, expanded_payload)

    candidate_path = root / "chat_result_obsidian.md"
    atomic_write_text(candidate_path, result.obsidian_note.markdown)
    candidate_text = candidate_path.read_text(encoding="utf-8")
    if candidate_text != result.obsidian_note.markdown:
        raise RuntimeError("Obsidian candidate post-write verification failed")

    inbox_path: str | None = None
    inbox_status: Literal["not_requested", "created", "updated", "identical"] = (
        "not_requested"
    )

    if obsidian_inbox is not None:
        inbox_root = obsidian_inbox.expanduser().resolve()
        if not inbox_root.is_dir():
            raise FileNotFoundError(
                f"Obsidian_Inbox root does not exist: {inbox_root}"
            )
        destination = inbox_root / result.obsidian_note.relative_path
        destination = destination.resolve()
        try:
            destination.relative_to(inbox_root)
        except ValueError as exc:
            raise ValueError("Obsidian destination escapes Inbox root") from exc

        if destination.exists():
            existing = destination.read_text(encoding="utf-8")
            if existing == result.obsidian_note.markdown:
                inbox_status = "identical"
            elif not update_existing:
                raise FileExistsError(
                    "A differing Obsidian file already exists. "
                    "Re-run with --update-existing only after confirming it is "
                    f"the intended current file: {destination}"
                )
            else:
                atomic_write_text(destination, result.obsidian_note.markdown)
                inbox_status = "updated"
        else:
            atomic_write_text(destination, result.obsidian_note.markdown)
            inbox_status = "created"

        if destination.read_text(encoding="utf-8") != result.obsidian_note.markdown:
            raise RuntimeError("Obsidian Inbox post-write verification failed")
        inbox_path = str(destination)

    return ChatApplyReport(
        valid=True,
        expanded_result_path=str(expanded_path),
        expanded_result_sha256=file_sha256(expanded_path),
        obsidian_candidate_path=str(candidate_path),
        obsidian_candidate_sha256=file_sha256(candidate_path),
        inbox_path=inbox_path,
        inbox_status=inbox_status,
        notion_mutated=False,
    )
