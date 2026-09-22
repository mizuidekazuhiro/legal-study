from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import BaseModel, Field

from legal_study.io_utils import atomic_write_json
from legal_study.models import PageInspection
from legal_study.source_store import SourceSnapshot

PAGE_INDEX_SCHEMA_VERSION = 1
PAGE_ALIGNMENT_SCHEMA_VERSION = 1


class PageIdentityRecord(BaseModel):
    page_number: int
    stable_page_id: str
    base_content_hash: str
    text_fingerprint: str
    image_fingerprint: str
    vector_fingerprint: str
    annotation_fingerprint: str
    normalized_text: str
    width: float
    height: float
    rotation: int


class SourcePageIndex(BaseModel):
    schema_version: int = PAGE_INDEX_SCHEMA_VERSION
    source_sha256: str
    original_filename: str
    page_count: int
    generated_at: datetime
    pages: list[PageIdentityRecord] = Field(default_factory=list)


class PageAlignmentRecord(BaseModel):
    stable_page_id: str | None = None
    previous_page: int | None = None
    current_page: int | None = None
    classification: str
    markup_changed: bool = False
    similarity: float | None = None
    safe_for_base_ocr_reuse: bool = False


class PageAlignment(BaseModel):
    schema_version: int = PAGE_ALIGNMENT_SCHEMA_VERSION
    current_source_sha256: str
    previous_source_sha256: str | None = None
    logical_document_name: str
    records: list[PageAlignmentRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_page_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, pymupdf.Rect | pymupdf.IRect):
        return {"type": "rect", "values": [round(float(item), 4) for item in value]}
    if isinstance(value, pymupdf.Point):
        return {"type": "point", "values": [round(float(value.x), 4), round(float(value.y), 4)]}
    if isinstance(value, pymupdf.Quad):
        return {
            "type": "quad",
            "values": [
                [round(float(point.x), 4), round(float(point.y), 4)]
                for point in value
            ],
        }
    if isinstance(value, pymupdf.Matrix):
        return {"type": "matrix", "values": [round(float(item), 6) for item in value]}
    return repr(value)


def _image_payload(image_info: list[dict[str, Any]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for image in image_info:
        digest = image.get("digest")
        rect = pymupdf.Rect(image["bbox"])
        output.append(
            {
                "digest": digest.hex() if isinstance(digest, bytes) else None,
                "bbox": [round(float(value), 3) for value in rect],
                "width": image.get("width"),
                "height": image.get("height"),
            }
        )
    return sorted(
        output,
        key=lambda item: (
            str(item.get("digest")),
            str(item.get("bbox")),
            str(item.get("width")),
            str(item.get("height")),
        ),
    )


def _annotation_payload(page: pymupdf.Page) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    annot = page.first_annot
    while annot is not None:
        info = annot.info or {}
        output.append(
            {
                "type": list(annot.type) if annot.type else None,
                "rect": [round(float(value), 3) for value in annot.rect],
                "colors": _json_safe(annot.colors),
                "opacity": annot.opacity,
                "content": info.get("content"),
                "vertices": _json_safe(annot.vertices or []),
            }
        )
        annot = annot.next
    return output


def page_identity_from_components(
    *,
    page_number: int,
    width: float,
    height: float,
    rotation: int,
    native_text: str,
    image_payload: object,
    vector_payload: object,
    annotation_payload: object,
) -> PageIdentityRecord:
    normalized_text = normalize_page_text(native_text)
    text_fingerprint = _hash_payload(normalized_text)
    if isinstance(image_payload, list):
        image_payload = sorted(
            image_payload,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    image_fingerprint = _hash_payload(image_payload)
    vector_fingerprint = _hash_payload(vector_payload)
    annotation_fingerprint = _hash_payload(annotation_payload)
    base_content_hash = _hash_payload(
        {
            "width": round(width, 3),
            "height": round(height, 3),
            "rotation": rotation,
            "text_fingerprint": text_fingerprint,
            "image_fingerprint": image_fingerprint,
        }
    )
    return PageIdentityRecord(
        page_number=page_number,
        stable_page_id=base_content_hash,
        base_content_hash=base_content_hash,
        text_fingerprint=text_fingerprint,
        image_fingerprint=image_fingerprint,
        vector_fingerprint=vector_fingerprint,
        annotation_fingerprint=annotation_fingerprint,
        normalized_text=normalized_text,
        width=width,
        height=height,
        rotation=rotation,
    )


def page_identity_from_inspection(page: PageInspection) -> PageIdentityRecord:
    image_payload = [
        {
            "digest": region.digest,
            "bbox": [
                round(region.bbox.x0, 3),
                round(region.bbox.y0, 3),
                round(region.bbox.x1, 3),
                round(region.bbox.y1, 3),
            ],
            "width": region.raw.get("width"),
            "height": region.raw.get("height"),
        }
        for region in page.raw_image_regions
    ]
    vector_payload = [
        {
            "rect": [
                round(drawing.rect.x0, 3),
                round(drawing.rect.y0, 3),
                round(drawing.rect.x1, 3),
                round(drawing.rect.y1, 3),
            ],
            "raw": drawing.raw,
        }
        for drawing in page.raw_vector_drawings
    ]
    annotation_payload = [annotation.model_dump(mode="json") for annotation in page.annotations]
    return page_identity_from_components(
        page_number=page.page_number,
        width=page.width,
        height=page.height,
        rotation=page.rotation,
        native_text=page.native_text,
        image_payload=image_payload,
        vector_payload=vector_payload,
        annotation_payload=annotation_payload,
    )


def build_source_page_index(snapshot: SourceSnapshot) -> SourcePageIndex:
    document = pymupdf.open(snapshot.snapshot_path)
    pages: list[PageIdentityRecord] = []
    try:
        for index in range(document.page_count):
            page = document[index]
            image_info = page.get_image_info(hashes=True, xrefs=True)
            drawings = page.get_drawings()
            pages.append(
                page_identity_from_components(
                    page_number=index + 1,
                    width=page.rect.width,
                    height=page.rect.height,
                    rotation=page.rotation,
                    native_text=page.get_text("text", sort=True),
                    image_payload=_image_payload(image_info),
                    vector_payload=_json_safe(drawings),
                    annotation_payload=_annotation_payload(page),
                )
            )
    finally:
        document.close()
    return SourcePageIndex(
        source_sha256=snapshot.sha256,
        original_filename=snapshot.original_filename,
        page_count=len(pages),
        generated_at=datetime.now(UTC),
        pages=pages,
    )


def ensure_source_page_index(snapshot: SourceSnapshot, cache_dir: Path) -> SourcePageIndex:
    directory = cache_dir / "page_indexes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.sha256}.json"
    if path.is_file():
        try:
            existing = SourcePageIndex.model_validate_json(path.read_text(encoding="utf-8"))
            if (
                existing.source_sha256 == snapshot.sha256
                and existing.original_filename == snapshot.original_filename
            ):
                return existing
        except (OSError, ValueError):
            pass
    index = build_source_page_index(snapshot)
    atomic_write_json(path, index.model_dump(mode="json"))
    return index


def find_previous_page_index(
    current: SourcePageIndex,
    cache_dir: Path,
) -> SourcePageIndex | None:
    directory = cache_dir / "page_indexes"
    if not directory.is_dir():
        return None
    candidates: list[SourcePageIndex] = []
    for path in directory.glob("*.json"):
        if path.stem == current.source_sha256:
            continue
        try:
            candidate = SourcePageIndex.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if candidate.original_filename == current.original_filename:
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.generated_at)


def _exact_alignment(
    previous: SourcePageIndex,
    current: SourcePageIndex,
) -> tuple[list[PageAlignmentRecord], set[int], set[int]]:
    previous_by_id: dict[str, list[PageIdentityRecord]] = {}
    current_by_id: dict[str, list[PageIdentityRecord]] = {}
    for page in previous.pages:
        previous_by_id.setdefault(page.stable_page_id, []).append(page)
    for page in current.pages:
        current_by_id.setdefault(page.stable_page_id, []).append(page)

    records: list[PageAlignmentRecord] = []
    matched_previous: set[int] = set()
    matched_current: set[int] = set()
    for stable_page_id in sorted(set(previous_by_id) & set(current_by_id)):
        old_pages = sorted(previous_by_id[stable_page_id], key=lambda item: item.page_number)
        new_pages = sorted(current_by_id[stable_page_id], key=lambda item: item.page_number)
        for old, new in zip(old_pages, new_pages, strict=False):
            markup_changed = (
                old.vector_fingerprint != new.vector_fingerprint
                or old.annotation_fingerprint != new.annotation_fingerprint
            )
            moved = old.page_number != new.page_number
            if moved and markup_changed:
                classification = "MOVED_MARKUP_CHANGED"
            elif moved:
                classification = "MOVED"
            elif markup_changed:
                classification = "MARKUP_CHANGED"
            else:
                classification = "SAME"
            records.append(
                PageAlignmentRecord(
                    stable_page_id=stable_page_id,
                    previous_page=old.page_number,
                    current_page=new.page_number,
                    classification=classification,
                    markup_changed=markup_changed,
                    similarity=1.0,
                    safe_for_base_ocr_reuse=True,
                )
            )
            matched_previous.add(old.page_number)
            matched_current.add(new.page_number)
    return records, matched_previous, matched_current


def align_page_indexes(
    previous: SourcePageIndex | None,
    current: SourcePageIndex,
) -> PageAlignment:
    if previous is None:
        records = [
            PageAlignmentRecord(
                stable_page_id=page.stable_page_id,
                current_page=page.page_number,
                classification="NEW",
            )
            for page in current.pages
        ]
    else:
        records, matched_previous, matched_current = _exact_alignment(previous, current)
        old_unmatched = [
            page for page in previous.pages if page.page_number not in matched_previous
        ]
        new_unmatched = [
            page for page in current.pages if page.page_number not in matched_current
        ]

        candidates: list[tuple[float, PageIdentityRecord, PageIdentityRecord]] = []
        for old in old_unmatched:
            if len(old.normalized_text) < 40:
                continue
            for new in new_unmatched:
                if len(new.normalized_text) < 40:
                    continue
                if abs(old.width - new.width) > 1.0 or abs(old.height - new.height) > 1.0:
                    continue
                if old.image_fingerprint != new.image_fingerprint:
                    continue
                similarity = SequenceMatcher(
                    None, old.normalized_text, new.normalized_text
                ).ratio()
                if similarity >= 0.985:
                    candidates.append((similarity, old, new))

        used_old: set[int] = set()
        used_new: set[int] = set()
        for similarity, old, new in sorted(candidates, key=lambda item: item[0], reverse=True):
            if old.page_number in used_old or new.page_number in used_new:
                continue
            records.append(
                PageAlignmentRecord(
                    previous_page=old.page_number,
                    current_page=new.page_number,
                    classification=(
                        "MOVED_CONTENT_CHANGED"
                        if old.page_number != new.page_number
                        else "CONTENT_CHANGED"
                    ),
                    markup_changed=(
                        old.vector_fingerprint != new.vector_fingerprint
                        or old.annotation_fingerprint != new.annotation_fingerprint
                    ),
                    similarity=round(similarity, 6),
                    safe_for_base_ocr_reuse=False,
                )
            )
            used_old.add(old.page_number)
            used_new.add(new.page_number)

        for old in old_unmatched:
            if old.page_number not in used_old:
                records.append(
                    PageAlignmentRecord(
                        stable_page_id=old.stable_page_id,
                        previous_page=old.page_number,
                        classification="DELETED",
                    )
                )
        for new in new_unmatched:
            if new.page_number not in used_new:
                records.append(
                    PageAlignmentRecord(
                        stable_page_id=new.stable_page_id,
                        current_page=new.page_number,
                        classification="NEW",
                    )
                )

    counts: dict[str, int] = {}
    for record in records:
        counts[record.classification] = counts.get(record.classification, 0) + 1
    return PageAlignment(
        current_source_sha256=current.source_sha256,
        previous_source_sha256=previous.source_sha256 if previous is not None else None,
        logical_document_name=current.original_filename,
        records=sorted(
            records,
            key=lambda item: (
                item.current_page if item.current_page is not None else 10**9,
                item.previous_page if item.previous_page is not None else 10**9,
            ),
        ),
        counts=counts,
    )
