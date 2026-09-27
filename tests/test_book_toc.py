from legal_study.book.toc_parser import (
    PrintedPageAnchor,
    build_printed_page_map,
    parse_toc_text,
)


def test_toc_hierarchy_parses_chapters_sections_and_rank() -> None:
    toc = """
第5章 構成要件 19 B Rank
5-1 構成要件の概念 19
5-2 構成要件要素 21
第6章 実行行為 25 A Rank
6-1 実行行為の意義 25
"""

    chapters = parse_toc_text(toc)

    assert [(item.chapter, item.title, item.printed_start_page) for item in chapters] == [
        (5, "構成要件", 19),
        (6, "実行行為", 25),
    ]
    assert [section.id for section in chapters[0].sections] == ["5-1", "5-2"]
    assert chapters[0].sections[1].printed_page == 21
    assert chapters[0].rank_annotation == "B Rank"


def test_printed_page_mapping_supports_offset_changes() -> None:
    mapping = build_printed_page_map(
        [
            PrintedPageAnchor(pdf_page=10, printed_page=1),
            PrintedPageAnchor(pdf_page=11, printed_page=2),
            PrintedPageAnchor(pdf_page=20, printed_page=15),
            PrintedPageAnchor(pdf_page=21, printed_page=16),
        ]
    )

    assert mapping.pdf_page_for_printed(2) == 11
    assert mapping.pdf_page_for_printed(15) == 20
    assert len(mapping.segments) == 2
    assert [segment.offset for segment in mapping.segments] == [9, 5]

