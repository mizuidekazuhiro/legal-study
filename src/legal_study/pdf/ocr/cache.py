from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from legal_study.io_utils import atomic_write_json
from legal_study.models import DocumentInspection
from legal_study.page_identity import page_identity_from_inspection
from legal_study.pdf.ocr.base import OcrBackendMetadata, OcrResult
from legal_study.run_manifest import RunManifest

SHARED_OCR_CACHE_SCHEMA_VERSION = 1


class SharedOcrCacheEntry(BaseModel):
    schema_version: int = SHARED_OCR_CACHE_SCHEMA_VERSION
    cache_key: str
    stable_page_id: str
    identity: dict[str, Any]
    result: dict[str, Any]
    provenance: dict[str, Any]


def _rounded_bbox(value: object) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    return [round(float(item), 4) for item in value]


def canonical_target_signature(target: dict[str, object]) -> dict[str, object]:
    signature: dict[str, object] = {
        "kind": str(target.get("kind", "")),
        "bbox": _rounded_bbox(target.get("bbox")),
        "dpi": int(target.get("dpi", 0)),
        "crop_padding_points": round(float(target.get("crop_padding_points", 0.0)), 4),
        "preprocessing": dict(target.get("preprocessing", {})),  # type: ignore[arg-type]
    }
    if target.get("native_candidate") is not None:
        signature["native_candidate"] = str(target["native_candidate"])
    if target.get("native_bbox") is not None:
        signature["native_bbox"] = _rounded_bbox(target.get("native_bbox"))
    if target.get("source_image_digest") is not None:
        signature["source_image_digest"] = str(target["source_image_digest"])
    return signature


def shared_cache_identity(
    *,
    stable_page_id: str,
    target: dict[str, object],
    backend: OcrBackendMetadata | dict[str, object] | None,
) -> tuple[str, dict[str, object]]:
    backend_payload: dict[str, object] | None
    if isinstance(backend, OcrBackendMetadata):
        backend_payload = backend.model_dump(mode="json")
    elif isinstance(backend, dict):
        backend_payload = dict(backend)
    else:
        backend_payload = None
    identity: dict[str, object] = {
        "schema_version": SHARED_OCR_CACHE_SCHEMA_VERSION,
        "stable_page_id": stable_page_id,
        "target": canonical_target_signature(target),
        "backend": backend_payload,
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), identity


class SharedOcrCache:
    def __init__(self, cache_dir: Path) -> None:
        self.root = (cache_dir / "ocr_pages").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, stable_page_id: str, cache_key: str) -> Path:
        for value in (stable_page_id, cache_key):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("Invalid shared OCR cache key")
        path = (self.root / stable_page_id / f"{cache_key}.json").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Shared OCR cache path escapes cache root")
        return path

    def load(
        self,
        *,
        stable_page_id: str,
        target: dict[str, object],
        backend: OcrBackendMetadata | dict[str, object] | None,
    ) -> SharedOcrCacheEntry | None:
        cache_key, identity = shared_cache_identity(
            stable_page_id=stable_page_id,
            target=target,
            backend=backend,
        )
        path = self._path(stable_page_id, cache_key)
        if not path.is_file():
            return None
        try:
            entry = SharedOcrCacheEntry.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if entry.cache_key != cache_key or entry.identity != identity:
                return None
            OcrResult.model_validate(entry.result)
            return entry
        except (OSError, ValueError, TypeError):
            return None

    def store(
        self,
        *,
        stable_page_id: str,
        target: dict[str, object],
        backend: OcrBackendMetadata | dict[str, object] | None,
        result: OcrResult | dict[str, object],
        provenance: dict[str, object],
    ) -> Path:
        cache_key, identity = shared_cache_identity(
            stable_page_id=stable_page_id,
            target=target,
            backend=backend,
        )
        path = self._path(stable_page_id, cache_key)
        result_payload = (
            result.model_dump(mode="json")
            if isinstance(result, OcrResult)
            else dict(result)
        )
        entry = SharedOcrCacheEntry(
            cache_key=cache_key,
            stable_page_id=stable_page_id,
            identity=identity,
            result=result_payload,
            provenance=provenance,
        )
        atomic_write_json(path, entry.model_dump(mode="json"))
        return path


def seed_shared_ocr_cache_from_run(
    run_dir: Path,
    cache_dir: Path,
) -> dict[str, int]:
    run_dir = run_dir.expanduser().resolve()
    inspection_path = run_dir / "inspection.json"
    ocr_path = run_dir / "ocr.json"
    manifest_path = run_dir / "run_manifest.json"
    for path in (inspection_path, ocr_path, manifest_path):
        if not path.is_file():
            raise FileNotFoundError(f"Required run artifact is missing: {path}")

    inspection = DocumentInspection.model_validate_json(
        inspection_path.read_text(encoding="utf-8")
    )
    manifest = RunManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    payload = json.loads(ocr_path.read_text(encoding="utf-8"))
    backend = payload.get("backend")
    if backend is not None and not isinstance(backend, dict):
        raise ValueError("ocr.json backend metadata is invalid")

    page_ids: dict[int, str] = {}
    for page in inspection.pages:
        stable_page_id = page.stable_page_id
        if stable_page_id is None:
            stable_page_id = page_identity_from_inspection(page).stable_page_id
        page_ids[page.page_number] = stable_page_id

    cache = SharedOcrCache(cache_dir)
    seeded = 0
    skipped = 0
    pages = payload.get("pages", {})
    if not isinstance(pages, dict):
        raise TypeError("ocr.json pages payload is invalid")
    for page_key, page_payload in pages.items():
        if not isinstance(page_payload, dict):
            skipped += 1
            continue
        page_number = int(page_key)
        stable_page_id = page_ids.get(page_number)
        if stable_page_id is None:
            skipped += 1
            continue
        candidates: list[dict[str, object]] = []
        full_page = page_payload.get("full_page")
        if isinstance(full_page, dict):
            candidates.append(full_page)
        regions = page_payload.get("regions")
        if isinstance(regions, list):
            candidates.extend(item for item in regions if isinstance(item, dict))

        for evidence in candidates:
            target = evidence.get("target")
            result = evidence.get("result")
            if (
                evidence.get("status") != "completed"
                or not isinstance(target, dict)
                or not isinstance(result, dict)
            ):
                skipped += 1
                continue
            target_copy = dict(target)
            target_copy["stable_page_id"] = stable_page_id
            OcrResult.model_validate(result)
            cache.store(
                stable_page_id=stable_page_id,
                target=target_copy,
                backend=backend,
                result=result,
                provenance={
                    "source_sha256": manifest.source.sha256,
                    "source_page": page_number,
                    "run_id": manifest.run_id,
                    "seeded_from_existing_run": True,
                },
            )
            seeded += 1
    return {"seeded": seeded, "skipped": skipped}
