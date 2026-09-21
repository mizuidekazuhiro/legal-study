from __future__ import annotations

from pathlib import Path
from typing import Any

from legal_study.models import BBox
from legal_study.pdf.ocr.base import OcrLine, OcrResult


class PaddleOcrEngine:
    """Optional PaddleOCR 3.x adapter.

    PaddleOCR is not a hard dependency. Install with `pip install -e .[ocr]` and
    install a compatible Paddle inference runtime for your machine.
    """

    name = "paddleocr"

    def __init__(self, *, lang: str = "japan", ocr_version: str = "PP-OCRv6") -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("PaddleOCR is not installed; use the `ocr` extra") from exc

        self._pipeline = PaddleOCR(
            lang=lang,
            ocr_version=ocr_version,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
        )

    def recognize(self, image_path: Path) -> OcrResult:
        result = self._pipeline.predict(str(image_path))
        lines: list[OcrLine] = []
        scores: list[float] = []

        for res in result:
            data = self._as_mapping(res)
            texts = list(data.get("rec_texts", []))
            confs = list(data.get("rec_scores", []))
            boxes = list(data.get("rec_boxes", []))
            for index, text in enumerate(texts):
                cleaned = str(text).strip()
                if not cleaned:
                    continue
                confidence = float(confs[index]) if index < len(confs) else 0.0
                bbox = None
                if index < len(boxes):
                    x0, y0, x1, y1 = [float(v) for v in boxes[index]]
                    bbox = BBox(x0=x0, y0=y0, x1=x1, y1=y1)
                lines.append(OcrLine(text=cleaned, confidence=confidence, bbox=bbox))
                scores.append(confidence)

        return OcrResult(
            engine=self.name,
            text="\n".join(line.text for line in lines),
            confidence=(sum(scores) / len(scores)) if scores else 0.0,
            lines=lines,
            warnings=[] if lines else ["no_text_detected"],
        )

    @staticmethod
    def _as_mapping(res: Any) -> dict[str, Any]:
        if isinstance(res, dict):
            if "res" in res and isinstance(res["res"], dict):
                return res["res"]
            return res
        json_value = getattr(res, "json", None)
        if isinstance(json_value, dict):
            value = json_value.get("res", json_value)
            if isinstance(value, dict):
                return value
        try:
            return dict(res)
        except Exception as exc:  # pragma: no cover - depends on Paddle version
            raise TypeError(f"Unsupported PaddleOCR result type: {type(res)!r}") from exc
