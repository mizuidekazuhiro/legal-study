from __future__ import annotations

import json
from pathlib import Path

from legal_study.models import BBox, DocumentInspection
from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.base import OcrEngine
from legal_study.pdf.vector_marks import cluster_red_pen_marks


class PdfIngestPipeline:
    """Evidence-first PDF ingest with selective / surgical OCR targets."""

    def __init__(self, inspector: PdfInspector | None = None, ocr_engine: OcrEngine | None = None):
        self.inspector = inspector or PdfInspector()
        self.ocr_engine = ocr_engine

    def run(
        self,
        source: str | Path,
        output_dir: str | Path,
        *,
        pages: list[int] | None = None,
    ) -> DocumentInspection:
        out = Path(output_dir).resolve()
        render_dir = out / "renders"
        out.mkdir(parents=True, exist_ok=True)

        inspection = self.inspector.inspect(source, pages=pages, render_dir=render_dir)
        review_crops = self._render_review_crops(source, inspection, out / "review_crops")
        ocr_targets = self._render_ocr_targets(source, inspection, out / "ocr_crops")

        ocr_results: dict[str, object] = {"pages": {}}
        if self.ocr_engine is not None:
            for page in inspection.pages:
                page_result: dict[str, object] = {}
                if page.ocr_recommended and page.rendered_image:
                    page_result["full_page"] = self.ocr_engine.recognize(
                        page.rendered_image
                    ).model_dump(mode="json")

                region_results: list[dict[str, object]] = []
                for target in ocr_targets.get(page.page_number, []):
                    crop_path = Path(str(target["image"]))
                    result = self.ocr_engine.recognize(crop_path)
                    region_results.append({**target, "result": result.model_dump(mode="json")})
                if region_results:
                    page_result["regions"] = region_results
                if page_result:
                    ocr_results["pages"][str(page.page_number)] = page_result

        (out / "inspection.json").write_text(
            inspection.model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "ocr.json").write_text(
            json.dumps(ocr_results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._write_review_manifest(
            inspection,
            out / "review_manifest.json",
            review_crops=review_crops,
            ocr_targets=ocr_targets,
        )
        return inspection

    @staticmethod
    def _clip_box(page_rect, box: BBox, padding: float):
        import fitz

        return (
            fitz.Rect(box.x0 - padding, box.y0 - padding, box.x1 + padding, box.y1 + padding)
            & page_rect
        )

    @classmethod
    def _render_review_crops(
        cls, source: str | Path, inspection: DocumentInspection, crop_dir: Path, dpi: int = 450
    ) -> dict[int, list[dict[str, object]]]:
        import fitz

        crop_dir.mkdir(parents=True, exist_ok=True)
        document = fitz.open(source)
        manifest: dict[int, list[dict[str, object]]] = {}
        try:
            for inspected_page in inspection.pages:
                clusters = cluster_red_pen_marks(inspected_page.vector_marks)
                if not clusters:
                    continue
                page = document[inspected_page.page_number - 1]
                page_items: list[dict[str, object]] = []
                for index, box in enumerate(clusters, start=1):
                    clip = cls._clip_box(page.rect, box, padding=8)
                    path = crop_dir / f"page-{inspected_page.page_number:04d}-red-{index:03d}.png"
                    page.get_pixmap(dpi=dpi, clip=clip, alpha=False).save(path)
                    page_items.append(
                        {
                            "kind": "red_pen_cluster",
                            "bbox": [clip.x0, clip.y0, clip.x1, clip.y1],
                            "image": str(path),
                        }
                    )
                manifest[inspected_page.page_number] = page_items
        finally:
            document.close()
        return manifest

    @classmethod
    def _render_ocr_targets(
        cls, source: str | Path, inspection: DocumentInspection, crop_dir: Path, dpi: int = 450
    ) -> dict[int, list[dict[str, object]]]:
        """Render only native-text regions that show broken glyph mappings.

        This is the surgical OCR path. It avoids replacing an otherwise-good PDF
        text layer merely because a few glyphs are corrupted.
        """
        import fitz

        crop_dir.mkdir(parents=True, exist_ok=True)
        document = fitz.open(source)
        manifest: dict[int, list[dict[str, object]]] = {}
        try:
            for inspected_page in inspection.pages:
                if not inspected_page.suspect_native_regions:
                    continue
                page = document[inspected_page.page_number - 1]
                items: list[dict[str, object]] = []
                for index, region in enumerate(inspected_page.suspect_native_regions, start=1):
                    clip = cls._clip_box(page.rect, region.bbox, padding=6)
                    path = crop_dir / (
                        f"page-{inspected_page.page_number:04d}-suspect-{index:03d}.png"
                    )
                    page.get_pixmap(dpi=dpi, clip=clip, alpha=False).save(path)
                    items.append(
                        {
                            "kind": "suspect_native_text",
                            "source_text": region.text,
                            "reason": region.reason,
                            "bbox": [clip.x0, clip.y0, clip.x1, clip.y1],
                            "image": str(path),
                        }
                    )
                manifest[inspected_page.page_number] = items
        finally:
            document.close()
        return manifest

    @staticmethod
    def _write_review_manifest(
        inspection: DocumentInspection,
        path: Path,
        *,
        review_crops: dict[int, list[dict[str, object]]],
        ocr_targets: dict[int, list[dict[str, object]]],
    ) -> None:
        payload = {
            "source_sha256": inspection.sha256,
            "pages": [
                {
                    "page_number": p.page_number,
                    "mode": p.mode,
                    "ocr_recommended": p.ocr_recommended,
                    "vision_review_recommended": p.vision_review_recommended,
                    "reasons": p.reasons,
                    "rendered_image": str(p.rendered_image) if p.rendered_image else None,
                    "vector_mark_count": len(p.vector_marks),
                    "highlight_text_candidates": [
                        {
                            "color": m.color_name,
                            "text": m.extracted_text,
                            "confidence": m.confidence,
                            "bbox": m.rect.model_dump(),
                        }
                        for m in p.vector_marks
                        if m.kind == "highlight_stroke"
                    ],
                    "suspect_native_regions": [r.model_dump() for r in p.suspect_native_regions],
                    "ocr_targets": ocr_targets.get(p.page_number, []),
                    "review_crops": review_crops.get(p.page_number, []),
                    "red_pen_regions": [
                        m.rect.model_dump()
                        for m in p.vector_marks
                        if m.kind == "red_pen_stroke"
                    ],
                }
                for p in inspection.pages
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
