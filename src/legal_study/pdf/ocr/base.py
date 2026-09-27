from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from legal_study.models import BBox


class OcrBackendMetadata(BaseModel):
    engine: str
    library_version: str | None = None
    runtime: str | None = None
    runtime_version: str | None = None
    model_version: str | None = None
    model_names: list[str] = Field(default_factory=list)
    model_hashes: dict[str, str] = Field(default_factory=dict)
    device: str
    offline: bool
    settings: dict[str, Any] = Field(default_factory=dict)


class CoordinateTransform(BaseModel):
    """Affine mapping from OCR image pixels to PDF page coordinates.

    The six values follow ``x_pdf = a*x + c*y + e`` and
    ``y_pdf = b*x + d*y + f``. Keeping the transform with the OCR evidence
    makes a detected pixel box traceable even when a crop was padded.
    """

    pixel_to_pdf: tuple[float, float, float, float, float, float]
    image_width_px: int
    image_height_px: int
    pdf_bbox: BBox
    page_rotation: int = 0

    def map_bbox(self, bbox: BBox) -> BBox:
        a, b, c, d, e, f = self.pixel_to_pdf
        points = (
            (a * bbox.x0 + c * bbox.y0 + e, b * bbox.x0 + d * bbox.y0 + f),
            (a * bbox.x1 + c * bbox.y0 + e, b * bbox.x1 + d * bbox.y0 + f),
            (a * bbox.x0 + c * bbox.y1 + e, b * bbox.x0 + d * bbox.y1 + f),
            (a * bbox.x1 + c * bbox.y1 + e, b * bbox.x1 + d * bbox.y1 + f),
        )
        return BBox(
            x0=min(point[0] for point in points),
            y0=min(point[1] for point in points),
            x1=max(point[0] for point in points),
            y1=max(point[1] for point in points),
        )


class OcrInputMetadata(BaseModel):
    source_kind: str
    page_number: int
    image: str
    image_sha256: str
    dpi: int
    crop_bbox: BBox
    crop_padding_points: float
    preprocessing: dict[str, Any] = Field(default_factory=dict)
    coordinate_transform: CoordinateTransform


class OcrLine(BaseModel):
    text: str
    confidence: float
    bbox: BBox | None = None
    polygon: list[tuple[float, float]] = Field(default_factory=list)
    pdf_bbox: BBox | None = None


class OcrResult(BaseModel):
    engine: str
    text: str
    confidence: float
    lines: list[OcrLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    backend: OcrBackendMetadata | None = None
    input: OcrInputMetadata | None = None
    executed_at: datetime | None = None


class OcrEngine(Protocol):
    name: str

    @property
    def metadata(self) -> OcrBackendMetadata: ...

    def recognize(self, image_path: Path) -> OcrResult: ...
