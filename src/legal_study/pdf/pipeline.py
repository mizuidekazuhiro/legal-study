from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from legal_study.io_utils import atomic_output_path, atomic_write_json, file_sha256
from legal_study.models import BBox, DocumentInspection
from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.base import OcrEngine
from legal_study.pdf.vector_marks import cluster_red_pen_marks
from legal_study.run_manifest import PreparedRun, update_manifest_page_count
from legal_study.source_store import SourceSnapshot, verify_snapshot
from legal_study.state import RunStateMissingError, RunStateStore

_STEP_VERSION = "1"


class PdfIngestPipeline:
    """Evidence-first PDF ingest with selective / surgical OCR targets."""

    def __init__(
        self,
        inspector: PdfInspector | None = None,
        ocr_engine: OcrEngine | None = None,
        *,
        review_crop_dpi: int = 450,
        ocr_crop_dpi: int = 450,
    ):
        self.inspector = inspector or PdfInspector()
        self.ocr_engine = ocr_engine
        self.review_crop_dpi = review_crop_dpi
        self.ocr_crop_dpi = ocr_crop_dpi

    def input_config(self) -> dict[str, object]:
        return {
            "pipeline_version": "1",
            "min_native_chars": self.inspector.min_native_chars,
            "min_native_quality": self.inspector.min_native_quality,
            "render_dpi": self.inspector.render_dpi,
            "review_crop_dpi": self.review_crop_dpi,
            "ocr_crop_dpi": self.ocr_crop_dpi,
            "ocr_engine": self.ocr_engine.name if self.ocr_engine is not None else "none",
        }

    def run(
        self,
        source: SourceSnapshot,
        prepared: PreparedRun,
        *,
        pages: list[int] | None = None,
    ) -> DocumentInspection:
        verify_snapshot(source)
        snapshot_path = source.snapshot_path
        out = prepared.output_dir
        render_dir = out / "renders"
        out.mkdir(parents=True, exist_ok=True)
        state = RunStateStore(prepared.state_db)
        if not state.has_run(prepared.manifest.run_id) and self._has_existing_artifacts(out):
            raise RunStateMissingError(
                "Run artifacts exist but their SQLite state is missing; "
                f"refusing to overwrite {out}"
            )
        state.register_run(prepared.manifest, prepared.manifest_path)
        try:
            self._record_source_step(state, prepared, source)
            inspection, inspection_hash = self._inspection_step(
                state, prepared, snapshot_path, render_dir, pages
            )
            update_manifest_page_count(prepared, inspection.page_count)
            review_crops, ocr_targets, crops_hash = self._crops_step(
                state, prepared, snapshot_path, inspection, out
            )
            ocr_hash = self._ocr_step(state, prepared, inspection, ocr_targets, out)
            review_hash = self._review_manifest_step(
                state,
                prepared,
                inspection,
                review_crops,
                ocr_targets,
                out,
                upstream_hashes=[inspection_hash, crops_hash, ocr_hash],
            )
            final_hash = self._hash_values(
                inspection_hash, crops_hash, ocr_hash, review_hash
            )
            self._complete_final_step(state, prepared, final_hash)
            state.set_run_status(prepared.manifest.run_id, "COMPLETED")
            return inspection
        except Exception as exc:
            state.set_run_status(prepared.manifest.run_id, "FAILED", error=repr(exc))
            raise

    @staticmethod
    def _hash_values(*values: str) -> str:
        return hashlib.sha256("\0".join(values).encode()).hexdigest()

    @staticmethod
    def _has_existing_artifacts(out: Path) -> bool:
        return any(
            path.is_file() and path.name != "run_manifest.json"
            for path in out.rglob("*")
            if "orphans" not in path.parts
        )

    @staticmethod
    def _archive_file(out: Path, step_name: str, path: Path) -> None:
        if not path.is_file():
            return
        resolved_out = out.resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_out):
            raise RuntimeError(f"Refusing to archive an artifact outside the run: {path}")
        digest = file_sha256(path)
        destination_dir = out / "orphans" / step_name
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{path.name}.{digest[:16]}.orphan"
        if destination.exists():
            if file_sha256(destination) != digest:
                raise RuntimeError(f"Orphan artifact hash collision: {destination}")
            path.unlink()
            return
        os.replace(path, destination)

    @classmethod
    def _archive_step_artifacts(cls, out: Path, step_name: str) -> None:
        fixed_files: list[Path]
        generated_files: list[Path] = []
        if step_name == "PDF_INSPECTED":
            fixed_files = [out / "inspection.json"]
            generated_files.extend((out / "renders").glob("*.png"))
        elif step_name == "EVIDENCE_CROPS_RENDERED":
            fixed_files = [out / "evidence_crops.json"]
            generated_files.extend((out / "review_crops").glob("*.png"))
            generated_files.extend((out / "ocr_crops").glob("*.png"))
        elif step_name == "OCR_COMPLETE":
            fixed_files = [out / "ocr.json"]
        elif step_name == "REVIEW_MANIFEST_WRITTEN":
            fixed_files = [out / "review_manifest.json"]
        else:
            fixed_files = []
        for path in [*fixed_files, *generated_files]:
            cls._archive_file(out, step_name, path)

    @staticmethod
    def _artifact_is_resumable(
        state: RunStateStore,
        prepared: PreparedRun,
        step_name: str,
        input_hash: str,
        artifact: Path,
    ) -> bool:
        if not artifact.is_file():
            return False
        return state.can_resume(
            prepared.manifest.run_id,
            step_name,
            input_hash=input_hash,
            version=_STEP_VERSION,
            output_hash=file_sha256(artifact),
        )

    def _inspection_bundle_hash(
        self, artifact: Path, inspection: DocumentInspection
    ) -> str | None:
        render_paths = [page.rendered_image for page in inspection.pages]
        if any(path is None or not path.is_file() for path in render_paths):
            return None
        return self._hash_values(
            file_sha256(artifact),
            *(file_sha256(path) for path in render_paths if path is not None),
        )

    def _crop_bundle_hash(self, artifact: Path, payload: dict[str, object]) -> str | None:
        if not self._crop_images_exist(payload):
            return None
        paths: list[Path] = []
        for group_name in ("review_crops", "ocr_targets"):
            group = payload.get(group_name, {})
            assert isinstance(group, dict)
            for items in group.values():
                assert isinstance(items, list)
                paths.extend(Path(str(item["image"])) for item in items)
        return self._hash_values(
            file_sha256(artifact),
            *(file_sha256(path) for path in sorted(paths, key=str)),
        )

    @staticmethod
    def _begin(state: RunStateStore, prepared: PreparedRun, step_name: str, input_hash: str) -> None:
        state.begin_step(
            prepared.manifest.run_id,
            step_name,
            input_hash=input_hash,
            version=_STEP_VERSION,
        )

    @staticmethod
    def _fail(
        state: RunStateStore, prepared: PreparedRun, step_name: str, exc: Exception
    ) -> None:
        state.fail_step(prepared.manifest.run_id, step_name, error=repr(exc))

    def _record_source_step(
        self, state: RunStateStore, prepared: PreparedRun, source: SourceSnapshot
    ) -> None:
        step_name = "SOURCE_RESOLVED"
        if state.can_resume(
            prepared.manifest.run_id,
            step_name,
            input_hash=source.sha256,
            version=_STEP_VERSION,
            output_hash=source.sha256,
        ):
            return
        self._begin(state, prepared, step_name, source.sha256)
        state.complete_step(
            prepared.manifest.run_id, step_name, output_hash=source.sha256
        )

    def _inspection_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        snapshot_path: Path,
        render_dir: Path,
        pages: list[int] | None,
    ) -> tuple[DocumentInspection, str]:
        step_name = "PDF_INSPECTED"
        artifact = prepared.output_dir / "inspection.json"
        input_hash = self._hash_values(prepared.manifest.input_hash, step_name)
        if artifact.is_file():
            try:
                inspection = DocumentInspection.model_validate_json(
                    artifact.read_text(encoding="utf-8")
                )
                bundle_hash = self._inspection_bundle_hash(artifact, inspection)
                if bundle_hash is not None and state.can_resume(
                    prepared.manifest.run_id,
                    step_name,
                    input_hash=input_hash,
                    version=_STEP_VERSION,
                    output_hash=bundle_hash,
                ):
                    return inspection, bundle_hash
            except (OSError, ValueError):
                pass

        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(state, prepared, step_name, input_hash)
        try:
            inspection = self.inspector.inspect(
                snapshot_path, pages=pages, render_dir=render_dir
            )
            atomic_write_json(artifact, inspection.model_dump(mode="json"))
            output_hash = self._inspection_bundle_hash(artifact, inspection)
            assert output_hash is not None
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return inspection, output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _crops_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        snapshot_path: Path,
        inspection: DocumentInspection,
        out: Path,
    ) -> tuple[
        dict[int, list[dict[str, object]]],
        dict[int, list[dict[str, object]]],
        str,
    ]:
        step_name = "EVIDENCE_CROPS_RENDERED"
        artifact = out / "evidence_crops.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash,
            step_name,
            file_sha256(out / "inspection.json"),
        )
        if artifact.is_file():
            try:
                payload = json.loads(artifact.read_text(encoding="utf-8"))
                bundle_hash = self._crop_bundle_hash(artifact, payload)
                if bundle_hash is not None and state.can_resume(
                    prepared.manifest.run_id,
                    step_name,
                    input_hash=input_hash,
                    version=_STEP_VERSION,
                    output_hash=bundle_hash,
                ):
                    return (
                        self._int_keys(payload["review_crops"]),
                        self._int_keys(payload["ocr_targets"]),
                        bundle_hash,
                    )
            except (OSError, ValueError):
                pass

        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(state, prepared, step_name, input_hash)
        try:
            review_crops = self._render_review_crops(
                snapshot_path,
                inspection,
                out / "review_crops",
                dpi=self.review_crop_dpi,
            )
            ocr_targets = self._render_ocr_targets(
                snapshot_path,
                inspection,
                out / "ocr_crops",
                dpi=self.ocr_crop_dpi,
            )
            payload = {"review_crops": review_crops, "ocr_targets": ocr_targets}
            atomic_write_json(artifact, payload)
            output_hash = self._crop_bundle_hash(artifact, payload)
            assert output_hash is not None
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return review_crops, ocr_targets, output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    @staticmethod
    def _int_keys(value: dict[str, object]) -> dict[int, list[dict[str, object]]]:
        return {int(key): list(items) for key, items in value.items()}  # type: ignore[arg-type]

    @staticmethod
    def _crop_images_exist(payload: dict[str, object]) -> bool:
        groups = (payload.get("review_crops", {}), payload.get("ocr_targets", {}))
        for group in groups:
            if not isinstance(group, dict):
                return False
            for items in group.values():
                if not isinstance(items, list):
                    return False
                for item in items:
                    if not isinstance(item, dict) or not Path(str(item.get("image", ""))).is_file():
                        return False
        return True

    def _ocr_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        inspection: DocumentInspection,
        ocr_targets: dict[int, list[dict[str, object]]],
        out: Path,
    ) -> str:
        step_name = "OCR_COMPLETE"
        artifact = out / "ocr.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash,
            step_name,
            file_sha256(out / "evidence_crops.json"),
        )
        if self._artifact_is_resumable(state, prepared, step_name, input_hash, artifact):
            return file_sha256(artifact)

        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(state, prepared, step_name, input_hash)
        try:
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
                        region_results.append(
                            {**target, "result": result.model_dump(mode="json")}
                        )
                    if region_results:
                        page_result["regions"] = region_results
                    if page_result:
                        ocr_results["pages"][str(page.page_number)] = page_result
            atomic_write_json(artifact, ocr_results)
            output_hash = file_sha256(artifact)
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _review_manifest_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        inspection: DocumentInspection,
        review_crops: dict[int, list[dict[str, object]]],
        ocr_targets: dict[int, list[dict[str, object]]],
        out: Path,
        *,
        upstream_hashes: list[str],
    ) -> str:
        step_name = "REVIEW_MANIFEST_WRITTEN"
        artifact = out / "review_manifest.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash, step_name, *upstream_hashes
        )
        if self._artifact_is_resumable(state, prepared, step_name, input_hash, artifact):
            return file_sha256(artifact)
        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(state, prepared, step_name, input_hash)
        try:
            self._write_review_manifest(
                inspection,
                artifact,
                review_crops=review_crops,
                ocr_targets=ocr_targets,
            )
            output_hash = file_sha256(artifact)
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _complete_final_step(
        self, state: RunStateStore, prepared: PreparedRun, final_hash: str
    ) -> None:
        step_name = "INGEST_COMPLETE"
        input_hash = self._hash_values(prepared.manifest.input_hash, step_name, final_hash)
        if state.can_resume(
            prepared.manifest.run_id,
            step_name,
            input_hash=input_hash,
            version=_STEP_VERSION,
            output_hash=final_hash,
        ):
            return
        self._begin(state, prepared, step_name, input_hash)
        state.complete_step(
            prepared.manifest.run_id, step_name, output_hash=final_hash
        )

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
                    pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
                    with atomic_output_path(path) as temporary:
                        pixmap.save(temporary)
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
                    pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
                    with atomic_output_path(path) as temporary:
                        pixmap.save(temporary)
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
        atomic_write_json(path, payload)
