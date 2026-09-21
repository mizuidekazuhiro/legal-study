from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PageMode(StrEnum):
    NATIVE = "native"
    HYBRID = "hybrid"
    OCR_REQUIRED = "ocr_required"


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class NativeSpan(BaseModel):
    text: str
    bbox: BBox
    font: str | None = None
    size: float | None = None
    color: int | None = None


class PdfAnnotation(BaseModel):
    type_name: str
    rect: BBox
    colors: dict[str, Any] = Field(default_factory=dict)
    opacity: float | None = None
    content: str | None = None
    vertices: list[tuple[float, float]] = Field(default_factory=list)


class VectorMark(BaseModel):
    kind: str
    color_name: str | None = None
    color_rgb: tuple[float, float, float] | None = None
    rect: BBox
    width: float | None = None
    opacity: float | None = None
    extracted_text: str | None = None
    confidence: float = 0.0


class SuspectRegion(BaseModel):
    text: str
    bbox: BBox
    reason: str


class PageInspection(BaseModel):
    page_number: int
    width: float
    height: float
    native_text: str
    native_char_count: int
    native_quality_score: float
    suspicious_char_count: int = 0
    suspect_native_regions: list[SuspectRegion] = Field(default_factory=list)
    image_coverage: float
    largest_image_coverage: float
    drawing_count: int
    annotation_count: int
    mode: PageMode
    ocr_recommended: bool
    vision_review_recommended: bool
    spans: list[NativeSpan] = Field(default_factory=list)
    annotations: list[PdfAnnotation] = Field(default_factory=list)
    vector_marks: list[VectorMark] = Field(default_factory=list)
    rendered_image: Path | None = None
    reasons: list[str] = Field(default_factory=list)


class DocumentInspection(BaseModel):
    source_path: Path
    sha256: str
    page_count: int
    pages: list[PageInspection]
