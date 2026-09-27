from __future__ import annotations

from pathlib import Path

import pymupdf

from legal_study.book.pipeline import BookPipeline, BookPipelineConfig
from legal_study.book.text_verification import BookTextStatus
from legal_study.pdf.ocr.base import OcrBackendMetadata, OcrLine, OcrResult
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


class CountingOcr:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0
        self._metadata = OcrBackendMetadata(
            engine=self.name,
            library_version="test",
            model_version="test",
            device="cpu",
            offline=True,
        )

    @property
    def metadata(self) -> OcrBackendMetadata:
        return self._metadata

    def recognize(self, image_path: Path) -> OcrResult:
        self.calls += 1
        return OcrResult(
            engine=self.name,
            text="国家の政策判断により",
            confidence=0.99,
            lines=[
                OcrLine(
                    text="国家の政策判断により",
                    confidence=0.99,
                )
            ],
            backend=self.metadata,
        )


def _book_pdf(path: Path, *, extra: str = "") -> None:
    document = pymupdf.open()
    texts = [
        "目次\n第5章 構成要件 19\n5-1 構成要件の概念 19\n5-2 構成要件要素 21",
        f"第5章 構成要件\n〈構成要件の概念〉\n国家の政策半|｣断により{extra}",
        "第6章 実行行為\n〈実行行為の意義〉",
    ]
    printed = [None, 19, 25]
    for index, (text, printed_page) in enumerate(zip(texts, printed, strict=True)):
        page = document.new_page(width=513, height=730)
        page.insert_text((60, 70), text, fontname="japan", fontsize=10)
        if printed_page is not None:
            page.insert_text(
                (478 if index % 2 else 27, 705),
                str(printed_page),
                fontname="japan",
                fontsize=9,
            )
    document.save(path)
    document.close()


def test_pipeline_discovers_chapter_repairs_surgically_and_resumes(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    _book_pdf(source)
    settings = LocalSettings(home=tmp_path / "home")
    snapshot = snapshot_source(source, settings=settings)
    engine = CountingOcr()
    pipeline = BookPipeline(
        config=BookPipelineConfig(
            chapters=[5],
            render_dpi=72,
            surgical_dpi=150,
            detect_markup=False,
            detect_figures=False,
        ),
        ocr_engine=engine,
    )

    first = pipeline.run(
        snapshot,
        output_dir=tmp_path / "vault",
        settings=settings,
        subject="刑法",
        book="基礎マスター 第1分冊",
    )
    second = pipeline.run(
        snapshot,
        output_dir=tmp_path / "vault",
        settings=settings,
        subject="刑法",
        book="基礎マスター 第1分冊",
    )

    assert first.selected_pdf_pages == [2]
    assert first.ocr_executed == 1
    assert engine.calls == 1
    assert second.processed == 0
    assert second.reused == 1
    assert second.ocr_executed == 0
    assert any(
        record.status == BookTextStatus.VISUALLY_REPAIRED
        and record.raw_text == "国家の政策半|｣断により"
        and record.canonical_text == "国家の政策判断により"
        for record in first.pages[0].text_records
    )
    markdown = next((tmp_path / "vault").rglob("01_構成要件の概念.md"))
    content = markdown.read_text(encoding="utf-8")
    assert "国家の政策判断により" in content
    assert "国家の政策半|｣断により" not in content
    assert "PDF_PAGE:2 / PRINTED_PAGE:19" in content


def test_source_change_cannot_reuse_surgical_ocr_target(tmp_path: Path) -> None:
    settings = LocalSettings(home=tmp_path / "home")
    engine = CountingOcr()
    pipeline = BookPipeline(
        config=BookPipelineConfig(
            chapters=[5],
            render_dpi=72,
            surgical_dpi=150,
            detect_markup=False,
            detect_figures=False,
        ),
        ocr_engine=engine,
    )
    first_source = tmp_path / "first.pdf"
    second_source = tmp_path / "second.pdf"
    _book_pdf(first_source)
    _book_pdf(second_source, extra="別版")

    pipeline.run(
        snapshot_source(first_source, settings=settings),
        output_dir=tmp_path / "vault-1",
        settings=settings,
    )
    pipeline.run(
        snapshot_source(second_source, settings=settings),
        output_dir=tmp_path / "vault-2",
        settings=settings,
    )

    assert engine.calls == 2
