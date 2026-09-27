from legal_study.notion_retrieval import retrieve_notion_statutes


class FakeDataSources:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def query(self, data_source_id, **kwargs):
        self.calls.append((data_source_id, kwargs))
        article = kwargs["filter"]["and"][1]["rich_text"]["equals"]
        return self.responses[article]


class FakeClient:
    def __init__(self, responses):
        self.data_sources = FakeDataSources(responses)


def _page(article="199", text="人を殺した者は、死刑又は無期若しくは五年以上の拘禁刑に処する。"):
    return {
        "id": "3cddec27-c9aa-8157-889d-ddad14f17a80",
        "url": "https://app.notion.com/p/3cddec27c9aa8157889dddad14f17a80",
        "last_edited_time": "2026-08-31T14:36:00.476Z",
        "properties": {
            "条文名": {
                "title": [{"plain_text": "刑法 第百九十九条"}],
            },
            "法令名": {
                "select": {"name": "刑法"},
            },
            "条文番号": {
                "rich_text": [{"plain_text": article}],
            },
            "条文本文": {
                "rich_text": [{"plain_text": text}],
            },
            "公式URL": {
                "url": "https://laws.e-gov.go.jp/law/140AC0000000045",
            },
        },
    }


def test_exact_statute_lookup_maps_notion_row() -> None:
    client = FakeClient(
        {
            "199": {
                "results": [_page()],
                "has_more": False,
            }
        }
    )

    records, attempts = retrieve_notion_statutes(
        client=client,
        data_source_id="collection://e116464c-6e1a-467e-b2d4-4bef337c0d07",
        law_name="刑法",
        articles=["199条"],
    )

    assert len(records) == 1
    record = records[0]
    assert record.record_id == "3cddec27-c9aa-8157-889d-ddad14f17a80"
    assert record.title == "刑法 第百九十九条"
    assert record.law_name == "刑法"
    assert record.article == "199"
    assert record.text.startswith("人を殺した者は")
    assert record.official_url == "https://laws.e-gov.go.jp/law/140AC0000000045"
    assert attempts[0].query == "刑法199条"
    assert attempts[0].matched_ids == [record.record_id]

    data_source_id, kwargs = client.data_sources.calls[0]
    assert data_source_id == "e116464c-6e1a-467e-b2d4-4bef337c0d07"
    assert kwargs["filter"]["and"][0] == {
        "property": "法令名",
        "select": {"equals": "刑法"},
    }
    assert kwargs["filter"]["and"][1] == {
        "property": "条文番号",
        "rich_text": {"equals": "199"},
    }


def test_missing_statute_is_rejected() -> None:
    client = FakeClient({"238": {"results": [], "has_more": False}})

    try:
        retrieve_notion_statutes(
            client=client,
            data_source_id="ds",
            law_name="刑法",
            articles=["238"],
        )
    except LookupError as exc:
        assert "刑法 238条" in str(exc)
    else:
        raise AssertionError("Expected missing statute to be rejected")


def test_ambiguous_statute_is_rejected() -> None:
    client = FakeClient(
        {
            "199": {
                "results": [_page(), {**_page(), "id": "duplicate"}],
                "has_more": False,
            }
        }
    )

    try:
        retrieve_notion_statutes(
            client=client,
            data_source_id="ds",
            law_name="刑法",
            articles=["199"],
        )
    except ValueError as exc:
        assert "Ambiguous Notion statute rows" in str(exc)
    else:
        raise AssertionError("Expected ambiguous statute rows to be rejected")


def test_article_normalization_is_conservative() -> None:
    client = FakeClient({})

    for invalid in ("第199条", "199条1項", "百九十九"):
        try:
            retrieve_notion_statutes(
                client=client,
                data_source_id="ds",
                law_name="刑法",
                articles=[invalid],
            )
        except ValueError as exc:
            assert "ASCII article number" in str(exc)
        else:
            raise AssertionError(f"Expected invalid article to be rejected: {invalid}")
