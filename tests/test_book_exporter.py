from pathlib import Path

import pytest

from legal_study.book.models import (
    BookChapter,
    BookFigure,
    BookManifest,
    BookPage,
    BookSection,
)
from legal_study.book.obsidian_exporter import ObsidianExporter, safe_filename
from legal_study.book.text_verification import BookTextRecord, BookTextStatus
from legal_study.models import BBox


def _manifest() -> BookManifest:
    return BookManifest(
        source_sha256="a" * 64,
        original_filename="source.pdf",
        subject="刑法",
        book="基礎マスター 第1分冊",
        chapters=[
            BookChapter(
                chapter=5,
                title="構成要件",
                printed_start_page=19,
                pdf_start_page=29,
                pdf_end_page=34,
                sections=[
                    BookSection(
                        id="5-1",
                        title="構成要件の概念",
                        printed_page=19,
                        pdf_page=29,
                    )
                ],
            )
        ],
    )


def _page(asset: Path) -> BookPage:
    return BookPage(
        pdf_page=29,
        printed_page=19,
        stable_page_id="b" * 64,
        chapter=5,
        section_id="5-1",
        text_records=[
            BookTextRecord(
                id="p0029-line-001",
                raw_text="国家の政策半|｣断により",
                canonical_text="国家の政策判断により",
                pdf_page=29,
                printed_page=19,
                bbox=BBox(x0=100, y0=100, x1=300, y1=120),
                source_method="native_plus_surgical_ocr",
                ocr_evidence_id="ocr-p0029-001",
                status=BookTextStatus.VISUALLY_REPAIRED,
                reason="verified",
            )
        ],
        figures=[
            BookFigure(
                id="p0029-fig-001",
                pdf_page=29,
                bbox=BBox(x0=80, y0=300, x1=500, y1=600),
                asset_name="ch05_p0029_fig_001.png",
                source_image=asset,
            )
        ],
    )


def test_safe_filename_blocks_windows_reserved_and_path_characters() -> None:
    assert safe_filename('第05章: 構成要件/総論?') == "第05章_ 構成要件_総論_"
    assert safe_filename("CON") == "_CON"


def test_markdown_export_is_idempotent_and_preserves_page_and_figure(tmp_path: Path) -> None:
    asset = tmp_path / "figure.png"
    asset.write_bytes(b"figure-bytes")
    output = tmp_path / "vault"
    exporter = ObsidianExporter(output)

    first = exporter.export(_manifest(), [_page(asset)])
    second = exporter.export(_manifest(), [_page(asset)])

    note = (
        output
        / "刑法"
        / "基礎マスター 第1分冊"
        / "第05章_構成要件"
        / "01_構成要件の概念.md"
    )
    text = note.read_text(encoding="utf-8")
    assert "国家の政策判断により" in text
    assert "国家の政策半|｣断により" not in text
    assert "<!-- PDF_PAGE:29 / PRINTED_PAGE:19 -->" in text
    assert "![[assets/ch05_p0029_fig_001.png]]" in text
    assert first.written > 0
    assert second.written == 0
    assert second.reused == first.written


def test_export_never_overwrites_different_existing_vault_note(tmp_path: Path) -> None:
    asset = tmp_path / "figure.png"
    asset.write_bytes(b"figure-bytes")
    output = tmp_path / "vault"
    exporter = ObsidianExporter(output)
    exporter.export(_manifest(), [_page(asset)])
    note = (
        output
        / "刑法"
        / "基礎マスター 第1分冊"
        / "第05章_構成要件"
        / "01_構成要件の概念.md"
    )
    note.write_text("user-owned content", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        exporter.export(_manifest(), [_page(asset)])

    assert note.read_text(encoding="utf-8") == "user-owned content"
