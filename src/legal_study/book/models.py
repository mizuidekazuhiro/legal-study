from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from legal_study.book.markup import AnnotationEvidence, MarkupEvidence
from legal_study.book.text_verification import BookTextRecord
from legal_study.models import BBox


class BookSection(BaseModel):
    id: str
    title: str
    printed_page: int
    pdf_page: int | None = None
    rank_annotation: str | None = None


class BookChapter(BaseModel):
    chapter: int
    title: str
    printed_start_page: int
    pdf_start_page: int | None = None
    pdf_end_page: int | None = None
    rank_annotation: str | None = None
    sections: list[BookSection] = Field(default_factory=list)


class PrintedPageMapping(BaseModel):
    pdf_page: int
    printed_page: int
    source: str = "printed_footer"
    status: str = "VISUALLY_VERIFIED"


class BookManifest(BaseModel):
    schema_version: int = 1
    source_sha256: str
    original_filename: str
    subject: str
    book: str
    structure_source: str = "toc"
    chapters: list[BookChapter] = Field(default_factory=list)
    page_mappings: list[PrintedPageMapping] = Field(default_factory=list)


class BookFigure(BaseModel):
    id: str
    pdf_page: int
    bbox: BBox
    asset_name: str
    source_image: Path
    search_ocr_text: str | None = None
    status: str = "VISUAL_PRESERVED"


class BookPage(BaseModel):
    pdf_page: int
    printed_page: int | None = None
    stable_page_id: str
    chapter: int
    section_id: str
    text_records: list[BookTextRecord] = Field(default_factory=list)
    figures: list[BookFigure] = Field(default_factory=list)
    annotations: list[AnnotationEvidence] = Field(default_factory=list)
    highlights: list[MarkupEvidence] = Field(default_factory=list)
