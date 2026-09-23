import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from legal_study.notion_registration import LegalQuestionBankRegistrar


def _select_property(*names: str) -> dict[str, Any]:
    return {
        "type": "select",
        "select": {"options": [{"name": name} for name in names]},
    }


def _schema() -> dict[str, Any]:
    return {
        "Subject": _select_property("刑法"),
        "Anki Deck": _select_property("刑法 論文試験"),
        "Level": _select_property(
            "L1 穴埋め",
            "L2 一問一答",
            "L3 小型論述",
            "L4 フルサイズ",
        ),
        "Anki Type": _select_property("基本"),
        "Topic": _select_property("答案構成"),
        "Status": _select_property("Ready"),
    }


def _with_plain_text(properties: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, prop in properties.items():
        copied = dict(prop)
        for key in ("title", "rich_text"):
            if key in copied:
                copied[key] = [
                    {
                        **item,
                        "plain_text": (item.get("text") or {}).get("content", ""),
                    }
                    for item in copied[key]
                ]
        result[name] = copied
    return result


class FakeDataSources:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate

    def retrieve(self, *, data_source_id: str) -> dict[str, Any]:
        assert data_source_id
        return {"properties": _schema()}

    def query(self, *, data_source_id: str, **kwargs: Any) -> dict[str, Any]:
        assert data_source_id
        assert "filter" in kwargs
        return {"results": [{"id": "existing"}] if self.duplicate else []}


class FakePages:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}

    def create(self, *, parent: dict[str, str], properties: dict[str, Any]) -> dict[str, Any]:
        assert parent["data_source_id"]
        page_id = f"page-{len(self.created) + 1}"
        self.created.append(properties)
        self.by_id[page_id] = {
            "id": page_id,
            "url": f"https://www.notion.so/{page_id}",
            "properties": _with_plain_text(properties),
        }
        return {"id": page_id, "url": f"https://www.notion.so/{page_id}"}

    def retrieve(self, *, page_id: str) -> dict[str, Any]:
        return self.by_id[page_id]


class FakeClient:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.data_sources = FakeDataSources(duplicate=duplicate)
        self.pages = FakePages()


def _write_run_and_result(tmp_path: Path) -> tuple[Path, Path]:
    run = tmp_path / "run"
    run.mkdir()
    source_sha = "a" * 64
    manifest = {
        "schema_version": "1",
        "run_id": "r" * 64,
        "input_hash": "i" * 64,
        "created_at": "2026-09-23T00:00:00Z",
        "updated_at": "2026-09-23T00:00:00Z",
        "subject": "criminal",
        "question": "16",
        "requested_pages": [200],
        "source": {
            "original_path": "C:/input/source.pdf",
            "original_filename": "論文マスター_刑法.pdf",
            "source_size": 10,
            "source_mtime_ns": 1,
            "sha256": source_sha,
            "snapshot_path": "C:/store/source.pdf",
            "snapshot_created_at": "2026-09-23T00:00:00Z",
        },
        "output_dir": ".",
        "page_count": 286,
        "pipeline_config": {},
        "app_version": "0.1.0",
        "python_version": "3.13.7",
        "platform": "Windows",
        "pymupdf_version": "1.28.2",
    }
    (run / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    result = {
        "schema_version": "chat_study_result.v1",
        "source": {
            "subject": "criminal",
            "question": "16",
            "source_sha256": source_sha,
            "requested_pages": [200],
        },
        "reviewed_pages": [200],
        "unresolved": [],
        "problem_card_extra": "【講師答案全文】<br><br>全文",
        "anki_cards": [
            {
                "name": "刑法第16問-01",
                "scope": "problem",
                "learning_type": "B",
                "subject": "刑法",
                "source": "論文マスター_刑法.pdf",
                "pdf_page": "PDF p.200",
                "pdf_page_number": 200,
                "topic": "答案構成",
                "subtopic": "処理順序",
                "level": "L4",
                "anki_type": "基本",
                "front": "【L4 答案構成】<br><br>問",
                "back": "【回答】<br><br>答",
                "extra": None,
                "anki_tags": ["刑法", "論文"],
                "anki_deck": "刑法 論文試験",
            }
        ],
        "obsidian_note": {
            "relative_path": "30_論文マスター/刑法/刑法_第16問.md",
            "markdown": "---\ntitle: 第16問\n---\n\n本文\n",
        },
    }
    result_path = tmp_path / "study_result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )
    return run, result_path


def test_registrar_creates_ready_card_and_verifies_it(tmp_path: Path) -> None:
    run, result_path = _write_run_and_result(tmp_path)
    client = FakeClient()

    registrar = LegalQuestionBankRegistrar(
        token="test-token",
        client_factory=lambda _token: client,
    )
    report = registrar.register(result_path=result_path, run_dir=run)

    assert report.status == "REGISTERED_AND_VERIFIED"
    assert report.created == 1
    assert report.verified == 1
    properties = client.pages.created[0]
    assert properties["Status"]["select"]["name"] == "Ready"
    assert properties["Export to Anki"]["checkbox"] is False
    assert properties["Anki Deck"]["select"]["name"] == "刑法 論文試験"
    assert properties["PDF Page"]["number"] == 200
    assert "Note ID" not in properties


def test_duplicate_name_aborts_before_any_create(tmp_path: Path) -> None:
    run, result_path = _write_run_and_result(tmp_path)
    client = FakeClient(duplicate=True)
    registrar = LegalQuestionBankRegistrar(
        token="test-token",
        client_factory=lambda _token: client,
    )

    with pytest.raises(RuntimeError, match="duplicate card names"):
        registrar.register(result_path=result_path, run_dir=run)

    assert client.pages.created == []
