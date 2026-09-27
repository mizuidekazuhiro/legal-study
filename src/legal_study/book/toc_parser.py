from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel, Field

from legal_study.book.models import BookChapter, BookSection


@dataclass(frozen=True)
class PrintedPageAnchor:
    pdf_page: int
    printed_page: int


class PrintedPageSegment(BaseModel):
    printed_start: int
    printed_end: int
    pdf_start: int
    pdf_end: int
    offset: int


class PrintedPageMap(BaseModel):
    anchors: list[dict[str, int]] = Field(default_factory=list)
    segments: list[PrintedPageSegment] = Field(default_factory=list)

    def pdf_page_for_printed(self, printed_page: int) -> int | None:
        for segment in self.segments:
            if segment.printed_start <= printed_page <= segment.printed_end:
                return printed_page + segment.offset
        if not self.anchors:
            return None
        nearest = min(
            self.anchors,
            key=lambda item: abs(item["printed_page"] - printed_page),
        )
        return printed_page + nearest["pdf_page"] - nearest["printed_page"]


_CHAPTER_RE = re.compile(r"^第\s*(\d+)\s*章\s+(.+?)\s+(\d+)\s*([ABC](?:\+)?\s*Rank)?$")
_SECTION_RE = re.compile(r"^(\d+)\s*[-‐‑‒–—―ー]\s*(\d+)\s+(.+?)\s+(\d+)\s*([ABC](?:\+)?\s*Rank)?$")


def _clean_line(line: str) -> str:
    normalized = unicodedata.normalize("NFKC", line)
    normalized = re.sub(r"[.．・…·]{2,}", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _rank(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"\s+", " ", value).strip()
    return compact[0].upper() + compact[1:]


def parse_toc_text(text: str) -> list[BookChapter]:
    chapters: list[BookChapter] = []
    by_number: dict[int, BookChapter] = {}
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        chapter_match = _CHAPTER_RE.match(line)
        if chapter_match:
            number = int(chapter_match.group(1))
            chapter = BookChapter(
                chapter=number,
                title=chapter_match.group(2).strip(),
                printed_start_page=int(chapter_match.group(3)),
                rank_annotation=_rank(chapter_match.group(4)),
            )
            chapters.append(chapter)
            by_number[number] = chapter
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            chapter_number = int(section_match.group(1))
            chapter = by_number.get(chapter_number)
            if chapter is None:
                continue
            chapter.sections.append(
                BookSection(
                    id=f"{chapter_number}-{int(section_match.group(2))}",
                    title=section_match.group(3).strip(),
                    printed_page=int(section_match.group(4)),
                    rank_annotation=_rank(section_match.group(5)),
                )
            )
    return chapters


def build_printed_page_map(anchors: list[PrintedPageAnchor]) -> PrintedPageMap:
    ordered = sorted(anchors, key=lambda item: (item.pdf_page, item.printed_page))
    groups: list[list[PrintedPageAnchor]] = []
    for anchor in ordered:
        offset = anchor.pdf_page - anchor.printed_page
        if not groups or groups[-1][-1].pdf_page - groups[-1][-1].printed_page != offset:
            groups.append([anchor])
        else:
            groups[-1].append(anchor)
    segments = [
        PrintedPageSegment(
            printed_start=min(item.printed_page for item in group),
            printed_end=max(item.printed_page for item in group),
            pdf_start=min(item.pdf_page for item in group),
            pdf_end=max(item.pdf_page for item in group),
            offset=group[0].pdf_page - group[0].printed_page,
        )
        for group in groups
    ]
    return PrintedPageMap(
        anchors=[
            {"pdf_page": item.pdf_page, "printed_page": item.printed_page}
            for item in ordered
        ],
        segments=segments,
    )
