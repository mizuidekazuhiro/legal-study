import pytest

from legal_study.obsidian_retrieval import search_obsidian_argument_patterns


_PATTERN = """---
title: 刑3　間接正犯の実行行為性
aliases:
  - 間接正犯の実行行為性
  - 刑3
subject: 刑法
type: 論証パターン
source: 論文ナビゲートテキスト 刑法
source_page: 10
pdf_pages: "PDF p.23"
pattern_id: 刑3
topic: 実行行為・間接正犯
---

# 刑3　間接正犯の実行行為性

## 論証パターン

> [!abstract] 論証
> 他人を利用する行為に実行行為性が認められるかが問題となる。
>
> この点について、実行行為とは構成要件的結果発生の現実的危険を有する行為であるから、他人を利用する場合でも、利用者が①正犯意思を有し、②他人の行為を道具として一方的に支配・利用している場合には、実行行為性が認められると解する。

## 掲載判例

判例本文
"""


def test_search_reads_registered_pattern_without_opening_source_pdf(tmp_path) -> None:
    vault = tmp_path / "vault"
    note = vault / "10_論証パターン/03_刑法/刑003_間接正犯の実行行為性.md"
    note.parent.mkdir(parents=True)
    note.write_text(_PATTERN, encoding="utf-8")

    patterns, attempts = search_obsidian_argument_patterns(
        vault_dir=vault,
        queries=["間接正犯", "刑3"],
        subject="刑法",
    )

    assert [pattern.pattern_id for pattern in patterns] == ["刑3"]
    pattern = patterns[0]
    assert pattern.aliases == ["間接正犯の実行行為性", "刑3"]
    assert pattern.source == "論文ナビゲートテキスト 刑法"
    assert pattern.source_page == 10
    assert pattern.pdf_pages == "PDF p.23"
    assert pattern.source_path == "10_論証パターン/03_刑法/刑003_間接正犯の実行行為性.md"
    assert "他人を利用する行為に実行行為性" in pattern.body
    assert "判例本文" not in pattern.body
    assert [attempt.query for attempt in attempts] == ["間接正犯", "刑3"]
    assert attempts[0].matched_ids == ["刑3"]


def test_search_ignores_non_pattern_and_other_subject(tmp_path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "problem.md").write_text(
        "---\ntitle: problem\nsubject: 刑法\ntype: 問題別\n---\n間接正犯",
        encoding="utf-8",
    )
    (vault / "constitutional.md").write_text(
        _PATTERN.replace("subject: 刑法", "subject: 憲法"),
        encoding="utf-8",
    )

    patterns, attempts = search_obsidian_argument_patterns(
        vault_dir=vault,
        queries=["間接正犯"],
        subject="刑法",
    )

    assert patterns == []
    assert attempts[0].matched_ids == []


def test_conflicting_duplicate_pattern_id_is_rejected(tmp_path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(_PATTERN, encoding="utf-8")
    (vault / "b.md").write_text(
        _PATTERN.replace("現実的危険を有する行為", "別内容"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Conflicting duplicate Obsidian pattern_id"):
        search_obsidian_argument_patterns(
            vault_dir=vault,
            queries=["間接正犯"],
            subject="刑法",
        )
