from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from legal_study.models import BBox


class OcrLine(BaseModel):
    text: str
    confidence: float
    bbox: BBox | None = None


class OcrResult(BaseModel):
    engine: str
    text: str
    confidence: float
    lines: list[OcrLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OcrEngine(Protocol):
    name: str

    def recognize(self, image_path: Path) -> OcrResult: ...
