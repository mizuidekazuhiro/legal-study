from __future__ import annotations

import os
from typing import Any, Protocol

from legal_study.supplemental_retrieval import (
    NotionStatuteRecord,
    SupplementalSearchAttempt,
)


class _DataSourcesEndpoint(Protocol):
    def query(self, data_source_id: str, **kwargs: Any) -> Any: ...


class _NotionClient(Protocol):
    data_sources: _DataSourcesEndpoint


def retrieve_notion_statutes(
    *,
    client: _NotionClient,
    data_source_id: str,
    law_name: str,
    articles: list[str],
) -> tuple[list[NotionStatuteRecord], list[SupplementalSearchAttempt]]:
    """Retrieve exact statute rows from the configured Notion data source.

    Each requested article must resolve to exactly one live row. Missing or
    ambiguous rows stop the build rather than allowing the model to guess.
    """

    cleaned_data_source_id = _normalize_data_source_id(data_source_id)
    cleaned_law_name = law_name.strip()
    if not cleaned_law_name:
        raise ValueError("law_name must not be empty")

    normalized_articles = [_normalize_article(article) for article in articles]
    if not normalized_articles:
        raise ValueError("At least one statute article is required")

    records: list[NotionStatuteRecord] = []
    attempts: list[SupplementalSearchAttempt] = []

    for article in normalized_articles:
        response = client.data_sources.query(
            data_source_id=cleaned_data_source_id,
            filter={
                "and": [
                    {
                        "property": "法令名",
                        "select": {"equals": cleaned_law_name},
                    },
                    {
                        "property": "条文番号",
                        "rich_text": {"equals": article},
                    },
                ]
            },
            page_size=10,
        )
        if not isinstance(response, dict):
            raise TypeError("Notion data source query must return an object")

        results = response.get("results")
        if not isinstance(results, list):
            raise TypeError("Notion data source query has no results list")
        if response.get("has_more") is True:
            raise ValueError(
                f"Notion statute lookup is unexpectedly paginated: {cleaned_law_name} {article}"
            )

        pages = [item for item in results if isinstance(item, dict)]
        attempts.append(
            SupplementalSearchAttempt(
                source="notion_statute",
                query=f"{cleaned_law_name}{article}条",
                matched_ids=[_page_id(page) for page in pages],
            )
        )

        if not pages:
            raise LookupError(f"Notion statute not found: {cleaned_law_name} {article}条")
        if len(pages) != 1:
            raise ValueError(
                f"Ambiguous Notion statute rows: {cleaned_law_name} {article}条 -> {len(pages)}"
            )

        record = _page_to_record(
            page=pages[0],
            expected_law_name=cleaned_law_name,
            expected_article=article,
        )
        records.append(record)

    return records, attempts


def retrieve_notion_statutes_from_env(
    *,
    law_name: str,
    articles: list[str],
) -> tuple[list[NotionStatuteRecord], list[SupplementalSearchAttempt]]:
    """Read Notion credentials/config from environment and perform a read-only lookup."""

    token = os.getenv("NOTION_TOKEN", "").strip()
    data_source_id = os.getenv("LEGAL_STUDY_NOTION_STATUTE_DATA_SOURCE_ID", "").strip()
    if not token:
        raise RuntimeError("NOTION_TOKEN is not set")
    if not data_source_id:
        raise RuntimeError("LEGAL_STUDY_NOTION_STATUTE_DATA_SOURCE_ID is not set")

    try:
        from notion_client import Client
    except ImportError as exc:
        raise RuntimeError(
            "Notion client is not installed; install legal-study[notion]"
        ) from exc

    client = Client(auth=token)
    return retrieve_notion_statutes(
        client=client,
        data_source_id=data_source_id,
        law_name=law_name,
        articles=articles,
    )


def _page_to_record(
    *,
    page: dict[str, Any],
    expected_law_name: str,
    expected_article: str,
) -> NotionStatuteRecord:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("Notion statute page has no properties object")

    actual_law_name = _select_name(properties, "法令名")
    actual_article = _rich_text(properties, "条文番号")
    text = _rich_text(properties, "条文本文")
    title = _title_text(properties, "条文名")
    official_url = _url_value(properties, "公式URL")

    if actual_law_name != expected_law_name:
        raise ValueError(
            f"Notion statute law mismatch: expected={expected_law_name!r}, "
            f"actual={actual_law_name!r}"
        )
    if actual_article != expected_article:
        raise ValueError(
            f"Notion statute article mismatch: expected={expected_article!r}, "
            f"actual={actual_article!r}"
        )
    if not text:
        raise ValueError(
            f"Notion statute text is empty: {expected_law_name} {expected_article}条"
        )

    notion_url = str(page.get("url") or "").strip()
    if not notion_url:
        raise ValueError("Notion statute page URL is missing")

    return NotionStatuteRecord(
        record_id=_page_id(page),
        title=title or None,
        law_name=actual_law_name,
        article=actual_article,
        text=text,
        notion_url=notion_url,
        official_url=official_url or None,
        last_edited_time=_optional_text(page.get("last_edited_time")),
    )


def _normalize_article(article: str) -> str:
    cleaned = article.strip()
    if cleaned.endswith("条"):
        cleaned = cleaned[:-1].strip()
    if not cleaned or not cleaned.isascii() or not cleaned.isdigit():
        raise ValueError(
            "Statute article must be an ASCII article number such as '199' or '199条'"
        )
    return str(int(cleaned))


def _normalize_data_source_id(value: str) -> str:
    cleaned = value.strip()
    prefix = "collection://"
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    if not cleaned:
        raise ValueError("Notion data source id must not be empty")
    return cleaned


def _page_id(page: dict[str, Any]) -> str:
    value = str(page.get("id") or "").strip()
    if not value:
        raise ValueError("Notion statute page id is missing")
    return value


def _select_name(properties: dict[str, Any], name: str) -> str:
    prop = _property(properties, name)
    selected = prop.get("select")
    if not isinstance(selected, dict):
        return ""
    return str(selected.get("name") or "").strip()


def _rich_text(properties: dict[str, Any], name: str) -> str:
    prop = _property(properties, name)
    items = prop.get("rich_text")
    if not isinstance(items, list):
        return ""
    return "".join(
        str(item.get("plain_text") or "")
        for item in items
        if isinstance(item, dict)
    ).strip()


def _title_text(properties: dict[str, Any], name: str) -> str:
    prop = _property(properties, name)
    items = prop.get("title")
    if not isinstance(items, list):
        return ""
    return "".join(
        str(item.get("plain_text") or "")
        for item in items
        if isinstance(item, dict)
    ).strip()


def _url_value(properties: dict[str, Any], name: str) -> str:
    prop = _property(properties, name)
    value = prop.get("url")
    return str(value).strip() if value is not None else ""


def _property(properties: dict[str, Any], name: str) -> dict[str, Any]:
    prop = properties.get(name)
    if not isinstance(prop, dict):
        raise ValueError(f"Notion statute property is missing: {name}")
    return prop


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
