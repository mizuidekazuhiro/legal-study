from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from legal_study.io_utils import atomic_write_json, file_sha256
from legal_study.models import BBox
from legal_study.pdf.ocr.base import OcrBackendMetadata, OcrLine, OcrResult
from legal_study.settings import LocalSettings

OCR_VERSION = "PP-OCRv6"
MODEL_NAMES = {
    "text_detection": "PP-OCRv6_medium_det",
    "textline_orientation": "PP-LCNet_x1_0_textline_ori",
    "text_recognition": "PP-OCRv6_medium_rec",
}
MODEL_MANIFEST = "model_manifest.json"


def default_model_root() -> Path:
    return LocalSettings().models_dir / "paddleocr"


def _installed_version(distribution: str) -> str | None:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return None


def model_directory_hash(path: Path) -> str:
    """Hash model bytes and their relative names in a stable order."""
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"Model directory contains no files: {path}")
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_sha256(item).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _model_dir(model_root: Path, model_name: str) -> Path:
    return model_root / "official_models" / model_name


def inspect_paddle_installation(model_root: Path | None = None) -> dict[str, Any]:
    """Inspect packages and local model files without importing PaddleOCR."""
    root = (model_root or default_model_root()).expanduser().resolve()
    manifest_path = root / MODEL_MANIFEST
    problems: list[str] = []
    manifest: dict[str, Any] = {}
    if not manifest_path.is_file():
        problems.append(f"missing_manifest:{manifest_path}")
    else:
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError("manifest root is not an object")
            manifest = value
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            problems.append(f"invalid_manifest:{exc}")

    manifest_names = manifest.get("model_names", {})
    manifest_hashes = manifest.get("model_hashes", {})
    if manifest and manifest.get("model_version") != OCR_VERSION:
        problems.append(f"model_version_mismatch:{manifest.get('model_version')}")

    model_status: dict[str, dict[str, Any]] = {}
    for role, expected_name in MODEL_NAMES.items():
        path = _model_dir(root, expected_name)
        expected_hash = (
            str(manifest_hashes.get(expected_name, ""))
            if isinstance(manifest_hashes, dict)
            else ""
        )
        actual_hash: str | None = None
        if not path.is_dir():
            problems.append(f"missing_model:{expected_name}")
        else:
            try:
                actual_hash = model_directory_hash(path)
            except ValueError as exc:
                problems.append(f"invalid_model:{expected_name}:{exc}")
        if isinstance(manifest_names, dict) and manifest_names.get(role) != expected_name:
            problems.append(f"model_name_mismatch:{role}")
        if actual_hash is not None and not expected_hash:
            problems.append(f"missing_model_hash:{expected_name}")
        elif actual_hash is not None and actual_hash != expected_hash:
            problems.append(f"model_hash_mismatch:{expected_name}")
        model_status[role] = {
            "name": expected_name,
            "path": str(path),
            "exists": path.is_dir(),
            "expected_hash": expected_hash or None,
            "actual_hash": actual_hash,
        }

    paddleocr_version = _installed_version("paddleocr")
    paddle_version = _installed_version("paddlepaddle")
    if paddleocr_version is None:
        problems.append("missing_package:paddleocr")
    if paddle_version is None:
        problems.append("missing_package:paddlepaddle")
    return {
        "ready": not problems,
        "device": "cpu",
        "offline": True,
        "model_root": str(root),
        "manifest": str(manifest_path),
        "model_version": OCR_VERSION,
        "paddleocr_version": paddleocr_version,
        "paddlepaddle_version": paddle_version,
        "models": model_status,
        "problems": problems,
    }


def _paddle_options(root: Path) -> dict[str, Any]:
    return {
        "lang": "japan",
        "ocr_version": OCR_VERSION,
        "device": "cpu",
        "text_detection_model_name": MODEL_NAMES["text_detection"],
        "text_recognition_model_name": MODEL_NAMES["text_recognition"],
        "textline_orientation_model_name": MODEL_NAMES["textline_orientation"],
        "text_detection_model_dir": str(
            _model_dir(root, MODEL_NAMES["text_detection"])
        ),
        "text_recognition_model_dir": str(
            _model_dir(root, MODEL_NAMES["text_recognition"])
        ),
        "textline_orientation_model_dir": str(
            _model_dir(root, MODEL_NAMES["textline_orientation"])
        ),
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
    }


def warmup_paddle_models(model_root: Path | None = None) -> dict[str, Any]:
    """Explicitly download the configured CPU models and record immutable hashes."""
    root = (model_root or default_model_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root)
    os.environ.pop("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", None)
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "PaddleOCR/PaddlePaddle is not installed; install the `ocr` extra first"
        ) from exc

    options = _paddle_options(root)
    # Omitting explicit directories is intentional only in this network-enabled
    # warmup path: PaddleX then populates PADDLE_PDX_CACHE_HOME/official_models.
    download_options = {
        key: value for key, value in options.items() if not key.endswith("_model_dir")
    }
    PaddleOCR(**download_options)

    hashes: dict[str, str] = {}
    for name in MODEL_NAMES.values():
        path = _model_dir(root, name)
        if not path.is_dir():
            raise RuntimeError(f"Warmup did not create expected model directory: {path}")
        hashes[name] = model_directory_hash(path)

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "engine": "paddleocr",
        "library_version": _installed_version("paddleocr"),
        "runtime": "paddlepaddle",
        "runtime_version": _installed_version("paddlepaddle"),
        "model_version": OCR_VERSION,
        "model_names": MODEL_NAMES,
        "model_hashes": hashes,
        "device": "cpu",
    }
    atomic_write_json(root / MODEL_MANIFEST, manifest)
    return inspect_paddle_installation(root)


class PaddleOcrEngine:
    """Offline-only PaddleOCR 3.7 adapter using pre-warmed local CPU models."""

    name = "paddleocr"

    def __init__(self, *, model_root: Path | None = None, device: str = "cpu") -> None:
        if device != "cpu":
            raise ValueError("P1-B guarantees CPU operation; device must be 'cpu'")
        self.model_root = (model_root or default_model_root()).expanduser().resolve()
        status = inspect_paddle_installation(self.model_root)
        if not status["ready"]:
            details = ", ".join(status["problems"])
            raise RuntimeError(
                "Offline PaddleOCR is not ready. Run `legal-study doctor --ocr`, then "
                f"`legal-study warmup-ocr` while online. Problems: {details}"
            )

        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.model_root)
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "1"
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover - checked above, defensive
            raise RuntimeError("PaddleOCR is not installed; install the `ocr` extra") from exc

        self._pipeline = PaddleOCR(**_paddle_options(self.model_root))
        models = status["models"]
        self._metadata = OcrBackendMetadata(
            engine=self.name,
            library_version=status["paddleocr_version"],
            runtime="paddlepaddle",
            runtime_version=status["paddlepaddle_version"],
            model_version=OCR_VERSION,
            model_names=list(MODEL_NAMES.values()),
            model_hashes={
                details["name"]: details["actual_hash"] for details in models.values()
            },
            device="cpu",
            offline=True,
            settings={
                "lang": "japan",
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": True,
                "model_root": str(self.model_root),
            },
        )

    @property
    def metadata(self) -> OcrBackendMetadata:
        return self._metadata.model_copy(deep=True)

    def recognize(self, image_path: Path) -> OcrResult:
        result = self._pipeline.predict(str(image_path))
        lines: list[OcrLine] = []
        scores: list[float] = []

        for res in result:
            data = self._as_mapping(res)
            texts = list(data.get("rec_texts", []))
            confs = list(data.get("rec_scores", []))
            boxes = list(data.get("rec_boxes", []))
            polygons = list(data.get("rec_polys", []))
            for index, text in enumerate(texts):
                cleaned = str(text).strip()
                if not cleaned:
                    continue
                confidence = float(confs[index]) if index < len(confs) else 0.0
                polygon = self._polygon(polygons[index]) if index < len(polygons) else []
                bbox = None
                if index < len(boxes):
                    x0, y0, x1, y1 = [float(value) for value in boxes[index]]
                    bbox = BBox(x0=x0, y0=y0, x1=x1, y1=y1)
                elif polygon:
                    bbox = BBox(
                        x0=min(point[0] for point in polygon),
                        y0=min(point[1] for point in polygon),
                        x1=max(point[0] for point in polygon),
                        y1=max(point[1] for point in polygon),
                    )
                lines.append(
                    OcrLine(
                        text=cleaned,
                        confidence=confidence,
                        bbox=bbox,
                        polygon=polygon,
                    )
                )
                scores.append(confidence)

        return OcrResult(
            engine=self.name,
            text="\n".join(line.text for line in lines),
            confidence=(sum(scores) / len(scores)) if scores else 0.0,
            lines=lines,
            warnings=[] if lines else ["no_text_detected"],
            backend=self.metadata,
            executed_at=datetime.now(UTC),
        )

    @staticmethod
    def _polygon(value: Any) -> list[tuple[float, float]]:
        try:
            return [(float(point[0]), float(point[1])) for point in value]
        except (IndexError, TypeError, ValueError):
            return []

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
