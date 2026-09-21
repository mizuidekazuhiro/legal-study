from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import fitz

from legal_study.io_utils import atomic_output_path
from legal_study.models import (
    BBox,
    DocumentInspection,
    NativeSpan,
    PageInspection,
    PageMode,
    PdfAnnotation,
    RawImageRegion,
    RawVectorDrawing,
    SuspectRegion,
)
from legal_study.pdf.quality import (
    native_text_quality,
    suspicious_char_count,
    useful_char_count,
)
from legal_study.pdf.vector_marks import extract_vector_marks


class PdfInspector:
    def __init__(
        self,
        *,
        min_native_chars: int = 120,
        min_native_quality: float = 0.82,
        render_dpi: int = 300,
    ) -> None:
        self.min_native_chars = min_native_chars
        self.min_native_quality = min_native_quality
        self.render_dpi = render_dpi

    def inspect(
        self,
        source: str | Path,
        *,
        pages: Iterable[int] | None = None,
        render_dir: str | Path | None = None,
        artifact_root: str | Path | None = None,
    ) -> DocumentInspection:
        source_path = Path(source).expanduser().resolve()
        page_filter = set(pages) if pages is not None else None
        output_dir = Path(render_dir).resolve() if render_dir else None
        artifact_root_path = Path(artifact_root).resolve() if artifact_root else None
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
        if (
            output_dir is not None
            and artifact_root_path is not None
            and not output_dir.is_relative_to(artifact_root_path)
        ):
            raise ValueError("render_dir must be inside artifact_root")

        sha = self._sha256(source_path)
        document = fitz.open(source_path)
        inspected: list[PageInspection] = []
        page_count = document.page_count
        try:
            for index in range(document.page_count):
                page_number = index + 1
                if page_filter is not None and page_number not in page_filter:
                    continue
                inspected.append(
                    self._inspect_page(
                        document[index], page_number, output_dir, artifact_root_path
                    )
                )
        finally:
            document.close()

        return DocumentInspection(
            source_path=source_path,
            sha256=sha,
            page_count=page_count,
            pages=inspected,
        )

    def _inspect_page(
        self,
        page: fitz.Page,
        page_number: int,
        output_dir: Path | None,
        artifact_root: Path | None,
    ) -> PageInspection:
        native_text = page.get_text("text", sort=True)
        raw_native = self._raw_native(page)
        native_count = useful_char_count(native_text)
        quality = native_text_quality(native_text)
        image_info = page.get_image_info(hashes=True, xrefs=True)
        raw_image_regions = self._raw_image_regions(image_info)
        image_coverage, largest_image_coverage = self._image_coverage(page, image_info)
        spans = self._spans(page)
        suspect_regions = self._suspect_regions(spans)
        suspicious_count = suspicious_char_count(native_text)
        annotations = self._annotations(page)
        drawings = page.get_drawings()
        raw_vector_drawings = self._raw_vector_drawings(drawings)
        marks = extract_vector_marks(page, drawings=drawings)

        reasons: list[str] = []
        if native_count < self.min_native_chars:
            reasons.append(f"native_chars<{self.min_native_chars}")
        if quality < self.min_native_quality:
            reasons.append(f"native_quality<{self.min_native_quality}")
        if suspicious_count:
            reasons.append(f"suspicious_native_glyphs={suspicious_count}")
        if largest_image_coverage >= 0.80:
            reasons.append("large_raster_background")

        ocr_required = native_count < self.min_native_chars or quality < self.min_native_quality
        if ocr_required:
            mode = PageMode.OCR_REQUIRED
        elif largest_image_coverage >= 0.20 or len(page.get_images(full=True)) > 0:
            mode = PageMode.HYBRID
        else:
            mode = PageMode.NATIVE

        # Current study PDFs often contain flattened vector highlights/pen marks,
        # not PDF Annotation objects. Any recognized vector mark therefore causes
        # an image/Vision review recommendation even when native text is excellent.
        vision_review = (
            bool(marks)
            or bool(annotations)
            or largest_image_coverage >= 0.20
            or bool(suspect_regions)
        )
        if marks and not annotations:
            reasons.append("flattened_vector_marks_detected")

        rendered: str | None = None
        if output_dir is not None:
            rendered_path = output_dir / f"page-{page_number:04d}.png"
            pix = page.get_pixmap(dpi=self.render_dpi, alpha=False)
            with atomic_output_path(rendered_path) as temporary:
                pix.save(temporary)
            rendered = (
                rendered_path.relative_to(artifact_root).as_posix()
                if artifact_root is not None
                else str(rendered_path)
            )

        return PageInspection(
            page_number=page_number,
            width=page.rect.width,
            height=page.rect.height,
            native_text=native_text,
            native_char_count=native_count,
            native_quality_score=quality,
            suspicious_char_count=suspicious_count,
            suspect_native_regions=suspect_regions,
            image_coverage=image_coverage,
            largest_image_coverage=largest_image_coverage,
            drawing_count=len(drawings),
            annotation_count=len(annotations),
            mode=mode,
            ocr_recommended=ocr_required,
            vision_review_recommended=vision_review,
            spans=spans,
            raw_native=raw_native,
            annotations=annotations,
            raw_vector_drawings=raw_vector_drawings,
            raw_image_regions=raw_image_regions,
            vector_marks=marks,
            rendered_image=rendered,
            reasons=reasons,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _image_coverage(
        page: fitz.Page, image_info: list[dict[str, Any]]
    ) -> tuple[float, float]:
        page_area = max(page.rect.get_area(), 1.0)
        areas: list[float] = []
        seen: set[tuple[float, float, float, float]] = set()
        for image in image_info:
            rect = fitz.Rect(image["bbox"])
            key = tuple(round(v, 2) for v in rect)
            if key in seen:
                continue
            seen.add(key)
            areas.append(max(0.0, rect.get_area() / page_area))
        return min(1.0, sum(areas)), min(1.0, max(areas, default=0.0))

    @classmethod
    def _raw_native(cls, page: fitz.Page) -> dict[str, Any]:
        data = page.get_text("rawdict", sort=False)
        # Image bytes are deliberately represented by get_image_info metadata
        # instead. Text blocks retain the PDF extraction order and per-character
        # coordinates, which are needed to audit later normalized text.
        text_only = {
            **{key: value for key, value in data.items() if key != "blocks"},
            "blocks": [block for block in data.get("blocks", []) if block.get("type") == 0],
        }
        return cls._json_safe(text_only)

    @classmethod
    def _raw_vector_drawings(
        cls, drawings: list[dict[str, Any]]
    ) -> list[RawVectorDrawing]:
        output: list[RawVectorDrawing] = []
        for index, drawing in enumerate(drawings):
            rect = fitz.Rect(drawing["rect"])
            output.append(
                RawVectorDrawing(
                    drawing_index=index,
                    rect=BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
                    raw=cls._json_safe(drawing),
                )
            )
        return output

    @classmethod
    def _raw_image_regions(
        cls, image_info: list[dict[str, Any]]
    ) -> list[RawImageRegion]:
        output: list[RawImageRegion] = []
        for index, image in enumerate(image_info):
            rect = fitz.Rect(image["bbox"])
            digest = image.get("digest")
            output.append(
                RawImageRegion(
                    image_index=index,
                    bbox=BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
                    xref=int(image["xref"]) if image.get("xref") else None,
                    digest=digest.hex() if isinstance(digest, bytes) else None,
                    raw=cls._json_safe(image),
                )
            )
        return output

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, bytes):
            return {"type": "bytes", "hex": value.hex()}
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [cls._json_safe(item) for item in value]
        if isinstance(value, fitz.Rect | fitz.IRect):
            return {"type": "rect", "values": [float(item) for item in value]}
        if isinstance(value, fitz.Point):
            return {"type": "point", "values": [float(value.x), float(value.y)]}
        if isinstance(value, fitz.Quad):
            return {
                "type": "quad",
                "values": [[float(point.x), float(point.y)] for point in value],
            }
        if isinstance(value, fitz.Matrix):
            return {"type": "matrix", "values": [float(item) for item in value]}
        raise TypeError(f"Unsupported PyMuPDF evidence value: {type(value).__name__}")

    @staticmethod
    def _spans(page: fitz.Page) -> list[NativeSpan]:
        output: list[NativeSpan] = []
        data = page.get_text("dict", sort=True)
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = str(span.get("text", ""))
                    if not text:
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    output.append(
                        NativeSpan(
                            text=text,
                            bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                            font=span.get("font"),
                            size=span.get("size"),
                            color=span.get("color"),
                        )
                    )
        return output

    @staticmethod
    def _suspect_regions(spans: list[NativeSpan]) -> list[SuspectRegion]:
        regions: list[SuspectRegion] = []
        for span in spans:
            count = suspicious_char_count(span.text)
            if count:
                regions.append(
                    SuspectRegion(
                        text=span.text,
                        bbox=span.bbox,
                        reason=f"suspicious_glyphs={count}",
                    )
                )
        return regions

    @staticmethod
    def _annotations(page: fitz.Page) -> list[PdfAnnotation]:
        out: list[PdfAnnotation] = []
        annot = page.first_annot
        while annot is not None:
            vertices = []
            annotation_vertices = annot.vertices
            if annotation_vertices:
                vertices = [
                    (float(point.x), float(point.y))
                    for point in (fitz.Point(value) for value in annotation_vertices)
                ]
            rect = annot.rect
            type_code, type_name = annot.type
            raw = {
                "xref": annot.xref,
                "type": {"code": type_code, "name": type_name},
                "rect": PdfInspector._json_safe(rect),
                "colors": PdfInspector._json_safe(annot.colors or {}),
                "opacity": annot.opacity,
                "info": PdfInspector._json_safe(annot.info or {}),
                "vertices": PdfInspector._json_safe(annotation_vertices or []),
                "flags": annot.flags,
                "border": PdfInspector._json_safe(annot.border or {}),
                "blend_mode": annot.blendmode,
                "line_ends": PdfInspector._json_safe(annot.line_ends),
                "popup_xref": annot.popup_xref,
            }
            out.append(
                PdfAnnotation(
                    xref=int(annot.xref),
                    type_code=int(type_code),
                    type_name=str(type_name),
                    rect=BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
                    colors=dict(annot.colors or {}),
                    opacity=annot.opacity,
                    content=(annot.info or {}).get("content"),
                    vertices=vertices,
                    raw=raw,
                )
            )
            annot = annot.next
        return out
