# ruff: noqa: I001
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from legal_study import chat_bridge_worker, chat_result
from legal_study.run_manifest import RunManifest


DEFAULT_LEGAL_QUESTION_BANK_DATA_SOURCE_ID = (
    "1d69ccff-48a8-4889-bb83-45f87c0e714b"
)

LEVEL_TO_NOTION = {
    "L1": "L1 穴埋め",
    "L2": "L2 一問一答",
    "L3": "L3 小型論述",
    "L4": "L4 フルサイズ",
}


class StrictNotionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotionPreflight(StrictNotionModel):
    data_source_id: str
    card_count: int
    duplicate_names: list[str] = Field(default_factory=list)
    invalid_options: list[str] = Field(default_factory=list)


class NotionCardReceipt(StrictNotionModel):
    name: str
    page_id: str
    url: str | None = None
    verified: bool


class LegalQuestionBankRegistrar:
    """Register approved Chat bridge Anki cards into Legal Question Bank.

    This performs a full preflight before the first create call, never updates an
    existing page, always creates cards as Ready, keeps Export to Anki false, and
    omits Note ID. Each created page is re-fetched and verified.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        data_source_id: str | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.token = (
            token
            or os.environ.get("NOTION_TOKEN")
            or os.environ.get("NOTION_API_KEY")
        )
        if not self.token:
            raise RuntimeError(
                "Notion registration requires NOTION_TOKEN or NOTION_API_KEY"
            )
        self.data_source_id = (
            data_source_id
            or os.environ.get("LEGAL_STUDY_NOTION_DATA_SOURCE_ID")
            or DEFAULT_LEGAL_QUESTION_BANK_DATA_SOURCE_ID
        )
        self._client_factory = client_factory

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self.token or "")
        try:
            from notion_client import Client
        except ImportError as exc:
            raise RuntimeError(
                "Install the optional Notion dependency: pip install -e .[notion]"
            ) from exc
        return Client(auth=self.token)

    def register(
        self,
        *,
        result_path: Path,
        run_dir: Path,
    ) -> chat_bridge_worker.NotionRegistrationResult:
        result = chat_result.load_chat_result(result_path)
        run_root = run_dir.expanduser().resolve()
        manifest = RunManifest.model_validate_json(
            (run_root / "run_manifest.json").read_text(encoding="utf-8")
        )
        if (
            result.source.subject != manifest.subject
            or result.source.question != manifest.question
            or result.source.source_sha256 != manifest.source.sha256
        ):
            raise RuntimeError("Chat result identity does not match local run")

        cards = chat_result.expanded_anki_cards(result)
        client = self._client()
        preflight = self._preflight(client=client, cards=cards)
        if preflight.duplicate_names:
            raise RuntimeError(
                "Notion duplicate card names already exist: "
                + ", ".join(preflight.duplicate_names)
            )
        if preflight.invalid_options:
            raise RuntimeError(
                "Notion option preflight failed: "
                + "; ".join(preflight.invalid_options)
            )

        created: list[NotionCardReceipt] = []
        for raw in cards:
            card = chat_result.ChatAnkiCard.model_validate(raw)
            properties = self._properties(card)
            page = client.pages.create(
                parent={"data_source_id": self.data_source_id},
                properties=properties,
            )
            page_id = str(page["id"])
            fetched = client.pages.retrieve(page_id=page_id)
            self._verify_page(fetched, card)
            created.append(
                NotionCardReceipt(
                    name=card.name,
                    page_id=page_id,
                    url=page.get("url"),
                    verified=True,
                )
            )

        return chat_bridge_worker.NotionRegistrationResult(
            status="REGISTERED_AND_VERIFIED",
            created=len(created),
            verified=sum(1 for item in created if item.verified),
        )

    def _preflight(
        self,
        *,
        client: Any,
        cards: list[dict[str, object]],
    ) -> NotionPreflight:
        data_source = client.data_sources.retrieve(
            data_source_id=self.data_source_id
        )
        schema = data_source.get("properties", {})

        invalid_options: list[str] = []
        for raw in cards:
            card = chat_result.ChatAnkiCard.model_validate(raw)
            for property_name, value in (
                ("Subject", card.subject),
                ("Anki Deck", card.anki_deck),
                ("Level", LEVEL_TO_NOTION[card.level]),
                ("Anki Type", card.anki_type),
                ("Topic", card.topic),
                ("Status", "Ready"),
            ):
                options = _option_names(schema.get(property_name))
                if value not in options:
                    invalid_options.append(
                        f"{card.name}: {property_name}={value!r} not in {sorted(options)!r}"
                    )

        duplicate_names: list[str] = []
        for raw in cards:
            card = chat_result.ChatAnkiCard.model_validate(raw)
            response = client.data_sources.query(
                data_source_id=self.data_source_id,
                filter={
                    "property": "Name",
                    "title": {"equals": card.name},
                },
                page_size=2,
            )
            if response.get("results"):
                duplicate_names.append(card.name)

        return NotionPreflight(
            data_source_id=self.data_source_id,
            card_count=len(cards),
            duplicate_names=sorted(set(duplicate_names)),
            invalid_options=invalid_options,
        )

    def _properties(self, card: chat_result.ChatAnkiCard) -> dict[str, Any]:
        if card.extra is None:
            raise RuntimeError(f"Expanded card is missing Extra: {card.name}")
        return {
            "Name": {"title": _rich_text(card.name)},
            "Subject": {"select": {"name": card.subject}},
            "Status": {"select": {"name": "Ready"}},
            "Export to Anki": {"checkbox": False},
            "Anki Deck": {"select": {"name": card.anki_deck}},
            "Level": {"select": {"name": LEVEL_TO_NOTION[card.level]}},
            "Anki Type": {"select": {"name": card.anki_type}},
            "Anki Tags": {"rich_text": _rich_text(" ".join(card.anki_tags))},
            "Front": {"rich_text": _rich_text(card.front)},
            "Back": {"rich_text": _rich_text(card.back)},
            "Extra": {"rich_text": _rich_text(card.extra)},
            "Topic": {"select": {"name": card.topic}},
            "Subtopic": {"rich_text": _rich_text(card.subtopic)},
            "Source": {"rich_text": _rich_text(card.source)},
            "PDF Page": {"number": card.pdf_page_number},
        }

    def _verify_page(self, page: dict[str, Any], card: chat_result.ChatAnkiCard) -> None:
        properties = page.get("properties", {})
        expected_text = {
            "Name": card.name,
            "Anki Tags": " ".join(card.anki_tags),
            "Front": card.front,
            "Back": card.back,
            "Extra": card.extra or "",
            "Subtopic": card.subtopic,
            "Source": card.source,
        }
        for name, expected in expected_text.items():
            actual = _plain_property_text(properties.get(name))
            if actual != expected:
                raise RuntimeError(
                    f"Notion verification mismatch for {card.name} {name}: "
                    f"{actual!r} != {expected!r}"
                )

        expected_selects = {
            "Subject": card.subject,
            "Status": "Ready",
            "Anki Deck": card.anki_deck,
            "Level": LEVEL_TO_NOTION[card.level],
            "Anki Type": card.anki_type,
            "Topic": card.topic,
        }
        for name, expected in expected_selects.items():
            prop = properties.get(name) or {}
            actual = (prop.get("select") or {}).get("name")
            if actual != expected:
                raise RuntimeError(
                    f"Notion verification mismatch for {card.name} {name}"
                )

        if (properties.get("Export to Anki") or {}).get("checkbox") is not False:
            raise RuntimeError(
                f"Notion verification failed: Export to Anki is not false for {card.name}"
            )
        if (properties.get("PDF Page") or {}).get("number") != card.pdf_page_number:
            raise RuntimeError(
                f"Notion verification failed: PDF Page mismatch for {card.name}"
            )

        note_id = _plain_property_text(properties.get("Note ID"))
        if note_id:
            raise RuntimeError(
                f"Notion verification failed: Note ID unexpectedly set for {card.name}"
            )


def _option_names(property_schema: Any) -> set[str]:
    if not isinstance(property_schema, dict):
        return set()
    property_type = property_schema.get("type")
    if property_type != "select":
        return set()
    options = (property_schema.get("select") or {}).get("options") or []
    return {
        str(option.get("name"))
        for option in options
        if isinstance(option, dict) and option.get("name")
    }


def _rich_text(value: str, *, chunk_size: int = 1800) -> list[dict[str, Any]]:
    if not value:
        return []
    return [
        {
            "type": "text",
            "text": {"content": value[index : index + chunk_size]},
        }
        for index in range(0, len(value), chunk_size)
    ]


def _plain_property_text(property_value: Any) -> str:
    if not isinstance(property_value, dict):
        return ""
    items = property_value.get("title")
    if items is None:
        items = property_value.get("rich_text")
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plain = item.get("plain_text")
        if plain is not None:
            parts.append(str(plain))
            continue
        text = item.get("text")
        if isinstance(text, dict) and text.get("content") is not None:
            parts.append(str(text["content"]))
    return "".join(parts)
