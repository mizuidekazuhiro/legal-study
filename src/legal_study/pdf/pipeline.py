from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import pymupdf

from legal_study.io_utils import atomic_output_path, atomic_write_json, file_sha256
from legal_study.models import BBox, DocumentInspection
from legal_study.page_identity import (
    PageAlignment,
    align_page_indexes,
    ensure_source_page_index,
    find_previous_page_index,
)
from legal_study.pdf.inspector import PdfInspector
from legal_study.pdf.ocr.base import (
    CoordinateTransform,
    OcrBackendMetadata,
    OcrEngine,
    OcrInputMetadata,
    OcrResult,
)
from legal_study.pdf.ocr.cache import SharedOcrCache
from legal_study.pdf.ocr.routing import OcrRoutingConfig, routed_image_regions
from legal_study.pdf.vector_marks import cluster_red_vector_evidence
from legal_study.problem_packet import (
    handoff_markdown_filename,
    problem_markdown_filename,
    write_problem_packet,
)
from legal_study.reconciliation import ReconciliationResult, build_reconciliation
from legal_study.repair import RepairResult, build_repairs
from legal_study.run_manifest import PreparedRun, update_manifest_page_count
from legal_study.source_store import SourceSnapshot, verify_snapshot
from legal_study.state import RunStateMissingError, RunStateStore

_STEP_VERSION = "2"
_PAGE_IDENTITY_STEP_VERSION = "1"
_OCR_STEP_VERSION = "4"
_OCR_CHECKPOINT_SCHEMA_VERSION = 1
_RECONCILIATION_STEP_VERSION = "3"
_REPAIR_STEP_VERSION = "2"
_PROBLEM_PACKET_STEP_VERSION = "8"


class PdfIngestPipeline:
    """Evidence-first PDF ingest with selective / surgical OCR targets."""

    def __init__(
        self,
        inspector: PdfInspector | None = None,
        ocr_engine: OcrEngine | None = None,
        *,
        review_crop_dpi: int = 450,
        ocr_crop_dpi: int = 450,
        image_region_ocr_dpi: int = 300,
        routing_config: OcrRoutingConfig | None = None,
    ):
        config = routing_config or OcrRoutingConfig(
            surgical_dpi=ocr_crop_dpi,
            image_region_dpi=image_region_ocr_dpi,
        )
        self.inspector = inspector or PdfInspector(render_dpi=config.full_page_dpi)
        self.ocr_engine = ocr_engine
        self.review_crop_dpi = review_crop_dpi
        self.routing_config = config

    def input_config(self) -> dict[str, object]:
        return {
            "pipeline_version": "3",
            "min_native_chars": self.inspector.min_native_chars,
            "min_native_quality": self.inspector.min_native_quality,
            "render_dpi": self.inspector.render_dpi,
            "review_crop_dpi": self.review_crop_dpi,
            "ocr_routing": self.routing_config.as_dict(),
            "ocr_engine": self.ocr_engine.name if self.ocr_engine is not None else "none",
            "ocr_backend": self._backend_metadata(),
        }

    def _backend_metadata(self) -> dict[str, object] | None:
        if self.ocr_engine is None:
            return None
        metadata = getattr(self.ocr_engine, "metadata", None)
        if isinstance(metadata, OcrBackendMetadata):
            return metadata.model_dump(mode="json")
        return None

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
            _alignment, page_identity_hash = self._page_identity_step(
                state, prepared, source, out
            )
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
            reconciliation, reconciliation_hash = self._reconciliation_step(
                state,
                prepared,
                inspection,
                out,
                upstream_hashes=[inspection_hash, ocr_hash, review_hash],
            )
            repair, repair_hash = self._repair_step(
                state,
                prepared,
                inspection,
                reconciliation,
                out,
                upstream_hashes=[reconciliation_hash, ocr_hash],
            )
            packet_hash = self._problem_packet_step(
                state,
                prepared,
                inspection,
                reconciliation,
                repair,
                out,
                upstream_hashes=[reconciliation_hash, repair_hash, review_hash],
            )
            final_hash = self._hash_values(
                inspection_hash,
                page_identity_hash,
                crops_hash,
                ocr_hash,
                review_hash,
                reconciliation_hash,
                repair_hash,
                packet_hash,
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
        for path in out.rglob("*"):
            if not path.is_file() or path.name == "run_manifest.json":
                continue
            relative = path.relative_to(out)
            if relative.parts and relative.parts[0] == "orphans":
                continue
            return True
        return False

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
        elif step_name == "PAGE_IDENTITY_INDEXED":
            fixed_files = [out / "page_alignment.json"]
        elif step_name == "EVIDENCE_CROPS_RENDERED":
            fixed_files = [out / "evidence_crops.json"]
            generated_files.extend((out / "review_crops").glob("*.png"))
            generated_files.extend((out / "ocr_crops").glob("*.png"))
        elif step_name == "OCR_COMPLETE":
            fixed_files = [out / "ocr.json"]
        elif step_name == "REVIEW_MANIFEST_WRITTEN":
            fixed_files = [out / "review_manifest.json"]
        elif step_name == "RECONCILIATION_WRITTEN":
            fixed_files = [out / "reconciliation.json"]
        elif step_name == "REPAIR_WRITTEN":
            fixed_files = [out / "repair.json"]
        elif step_name == "PROBLEM_PACKET_WRITTEN":
            fixed_files = [out / "canonical_source.json", out / "problem_validation.json"]
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
        self, out: Path, artifact: Path, inspection: DocumentInspection
    ) -> str | None:
        try:
            render_paths = [
                self._resolve_artifact(out, page.rendered_image)
                for page in inspection.pages
                if page.rendered_image is not None
            ]
        except ValueError:
            return None
        if len(render_paths) != len(inspection.pages) or any(
            not path.is_file() for path in render_paths
        ):
            return None
        return self._hash_values(
            file_sha256(artifact),
            *(file_sha256(path) for path in render_paths),
        )

    def _crop_bundle_hash(
        self, out: Path, artifact: Path, payload: dict[str, object]
    ) -> str | None:
        if not self._crop_images_exist(out, payload):
            return None
        paths: list[Path] = []
        for group_name in ("review_crops", "ocr_targets"):
            group = payload.get(group_name, {})
            assert isinstance(group, dict)
            for items in group.values():
                assert isinstance(items, list)
                paths.extend(
                    self._resolve_artifact(out, str(item["image"])) for item in items
                )
        return self._hash_values(
            file_sha256(artifact),
            *(file_sha256(path) for path in sorted(paths, key=str)),
        )

    def _problem_packet_bundle_hash(
        self,
        out: Path,
        packet_paths: list[Path],
        validation_path: Path,
    ) -> str | None:
        if any(not path.is_file() for path in packet_paths):
            return None
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            upload_files = validation.get("upload_files", [])
            if not isinstance(upload_files, list):
                return None
            review_refs = sorted(
                {
                    str(reference)
                    for reference in upload_files
                    if isinstance(reference, str)
                    and reference.startswith("handoff_review/")
                }
            )
            review_paths: list[tuple[str, Path]] = []
            for reference in review_refs:
                path = self._resolve_artifact(out, reference)
                if not path.is_file():
                    return None
                review_paths.append((reference, path))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

        values = [
            f"{path.relative_to(out).as_posix()}:{file_sha256(path)}"
            for path in packet_paths
        ]
        values.extend(
            f"{reference}:{file_sha256(path)}"
            for reference, path in review_paths
        )
        return self._hash_values(*values)

    @staticmethod
    def _resolve_artifact(out: Path, reference: str) -> Path:
        relative = Path(reference)
        if relative.is_absolute():
            raise ValueError(f"Artifact path must be run-relative: {reference}")
        resolved_out = out.resolve()
        resolved = (resolved_out / relative).resolve()
        if not resolved.is_relative_to(resolved_out):
            raise ValueError(f"Artifact path escapes the run directory: {reference}")
        return resolved

    @staticmethod
    def _artifact_reference(out: Path, path: Path) -> str:
        resolved_out = out.resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_out):
            raise ValueError(f"Artifact is outside the run directory: {path}")
        return resolved_path.relative_to(resolved_out).as_posix()

    @staticmethod
    def _begin(
        state: RunStateStore,
        prepared: PreparedRun,
        step_name: str,
        input_hash: str,
        *,
        version: str = _STEP_VERSION,
    ) -> None:
        state.begin_step(
            prepared.manifest.run_id,
            step_name,
            input_hash=input_hash,
            version=version,
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
                bundle_hash = self._inspection_bundle_hash(
                    prepared.output_dir, artifact, inspection
                )
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
                snapshot_path,
                pages=pages,
                render_dir=render_dir,
                artifact_root=prepared.output_dir,
            )
            atomic_write_json(artifact, inspection.model_dump(mode="json"))
            output_hash = self._inspection_bundle_hash(
                prepared.output_dir, artifact, inspection
            )
            assert output_hash is not None
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return inspection, output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _page_identity_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        source: SourceSnapshot,
        out: Path,
    ) -> tuple[PageAlignment, str]:
        step_name = "PAGE_IDENTITY_INDEXED"
        artifact = out / "page_alignment.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash,
            step_name,
            source.sha256,
            source.original_filename,
        )
        if artifact.is_file():
            try:
                alignment = PageAlignment.model_validate_json(
                    artifact.read_text(encoding="utf-8")
                )
                if state.can_resume(
                    prepared.manifest.run_id,
                    step_name,
                    input_hash=input_hash,
                    version=_PAGE_IDENTITY_STEP_VERSION,
                    output_hash=file_sha256(artifact),
                ):
                    return alignment, file_sha256(artifact)
            except (OSError, ValueError):
                pass

        self._archive_step_artifacts(out, step_name)
        self._begin(
            state,
            prepared,
            step_name,
            input_hash,
            version=_PAGE_IDENTITY_STEP_VERSION,
        )
        try:
            cache_dir = prepared.state_db.parent / "cache"
            current_index = ensure_source_page_index(source, cache_dir)
            previous_index = find_previous_page_index(current_index, cache_dir)
            alignment = align_page_indexes(previous_index, current_index)
            atomic_write_json(artifact, alignment.model_dump(mode="json"))
            output_hash = file_sha256(artifact)
            state.complete_step(
                prepared.manifest.run_id,
                step_name,
                output_hash=output_hash,
            )
            return alignment, output_hash
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
                bundle_hash = self._crop_bundle_hash(out, artifact, payload)
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
                artifact_root=out,
                dpi=self.review_crop_dpi,
            )
            ocr_targets = self._render_ocr_targets(
                snapshot_path,
                inspection,
                out / "ocr_crops",
                artifact_root=out,
            )
            payload = {
                "routing_config": self.routing_config.as_dict(),
                "review_crops": review_crops,
                "ocr_targets": ocr_targets,
            }
            atomic_write_json(artifact, payload)
            output_hash = self._crop_bundle_hash(out, artifact, payload)
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
    def _crop_images_exist(out: Path, payload: dict[str, object]) -> bool:
        groups = (payload.get("review_crops", {}), payload.get("ocr_targets", {}))
        for group in groups:
            if not isinstance(group, dict):
                return False
            for items in group.values():
                if not isinstance(items, list):
                    return False
                for item in items:
                    if not isinstance(item, dict):
                        return False
                    try:
                        path = PdfIngestPipeline._resolve_artifact(
                            out, str(item.get("image", ""))
                        )
                    except ValueError:
                        return False
                    if not path.is_file():
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
        if artifact.is_file() and state.can_resume(
            prepared.manifest.run_id,
            step_name,
            input_hash=input_hash,
            version=_OCR_STEP_VERSION,
            output_hash=file_sha256(artifact),
        ):
            return file_sha256(artifact)

        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(
            state,
            prepared,
            step_name,
            input_hash,
            version=_OCR_STEP_VERSION,
        )
        try:
            page_targets: dict[int, tuple[dict[str, object] | None, list[dict[str, object]]]] = {}
            total_targets = 0
            for page in inspection.pages:
                full_page_target = (
                    self._full_page_target(page, out)
                    if page.ocr_recommended and page.rendered_image
                    else None
                )
                region_targets = list(ocr_targets.get(page.page_number, []))
                page_targets[page.page_number] = (full_page_target, region_targets)
                total_targets += (1 if full_page_target is not None else 0) + len(
                    region_targets
                )

            ocr_results: dict[str, object] = {
                "schema_version": 5,
                "native_text_replaced": False,
                "backend": self._backend_metadata(),
                "routing_config": self.routing_config.as_dict(),
                "checkpoint": {
                    "schema_version": _OCR_CHECKPOINT_SCHEMA_VERSION,
                    "total_targets": total_targets,
                    "reused_targets": 0,
                    "executed_targets": 0,
                },
                "pages": {},
            }
            page_results: dict[str, object] = {}
            current_target = 0
            reused_targets = 0
            executed_targets = 0

            for page in inspection.pages:
                full_page_target, region_targets = page_targets[page.page_number]
                page_result: dict[str, object] = {
                    "routing": {
                        "native_text_preserved": True,
                        "text_layer_trust": page.text_layer_trust.value,
                        "text_layer_origin": page.text_layer_origin.value,
                        "full_page_ocr": page.ocr_recommended,
                        "surgical_region_count": sum(
                            target.get("kind") == "suspect_native_text"
                            for target in region_targets
                        ),
                        "image_region_count": sum(
                            target.get("kind") == "image_region"
                            for target in region_targets
                        ),
                    }
                }

                if full_page_target is not None:
                    current_target += 1
                    evidence, reused = self._execute_ocr_target_checkpointed(
                        prepared,
                        full_page_target,
                        out,
                        current=current_target,
                        total=total_targets,
                    )
                    page_result["full_page"] = evidence
                    reused_targets += int(reused)
                    executed_targets += int(not reused)

                region_results: list[dict[str, object]] = []
                for target in region_targets:
                    current_target += 1
                    evidence, reused = self._execute_ocr_target_checkpointed(
                        prepared,
                        target,
                        out,
                        current=current_target,
                        total=total_targets,
                    )
                    region_results.append(evidence)
                    reused_targets += int(reused)
                    executed_targets += int(not reused)
                if region_results:
                    page_result["regions"] = region_results
                page_results[str(page.page_number)] = page_result

            checkpoint_summary = ocr_results["checkpoint"]
            assert isinstance(checkpoint_summary, dict)
            checkpoint_summary["reused_targets"] = reused_targets
            checkpoint_summary["executed_targets"] = executed_targets
            ocr_results["pages"] = page_results
            atomic_write_json(artifact, ocr_results)
            output_hash = file_sha256(artifact)
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _execute_ocr_target(
        self, target: dict[str, object], out: Path
    ) -> dict[str, object]:
        evidence: dict[str, object] = {"target": target}
        if self.ocr_engine is None:
            evidence["status"] = "not_executed_no_backend"
            return evidence
        image_path = self._resolve_artifact(out, str(target["image"]))
        result = self.ocr_engine.recognize(image_path)
        enriched = self._attach_input_metadata(result, target)
        evidence["status"] = "completed"
        evidence["result"] = enriched.model_dump(mode="json")
        return evidence

    def _ocr_checkpoint_identity(
        self,
        prepared: PreparedRun,
        target: dict[str, object],
        out: Path,
    ) -> tuple[str, dict[str, object]]:
        image_reference = str(target.get("image", ""))
        image_path = self._resolve_artifact(out, image_reference)
        if not image_path.is_file():
            raise FileNotFoundError(f"OCR target image is missing: {image_reference}")
        actual_image_sha = file_sha256(image_path)
        declared_image_sha = str(target.get("image_sha256", ""))
        if not declared_image_sha or actual_image_sha != declared_image_sha:
            raise RuntimeError(
                "OCR target image SHA mismatch: "
                f"{image_reference} declared={declared_image_sha} actual={actual_image_sha}"
            )

        backend = self._backend_metadata()
        identity: dict[str, object] = {
            "source_sha256": prepared.manifest.source.sha256,
            "target": target,
            "ocr_engine": self.ocr_engine.name if self.ocr_engine is not None else "none",
            "backend": backend,
        }
        encoded = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), identity

    def _ocr_checkpoint_path(self, out: Path, checkpoint_hash: str) -> Path:
        if len(checkpoint_hash) != 64 or any(
            char not in "0123456789abcdef" for char in checkpoint_hash
        ):
            raise ValueError("Invalid OCR checkpoint hash")
        checkpoint_dir = (out / "ocr_checkpoints").resolve()
        resolved_out = out.resolve()
        if not checkpoint_dir.is_relative_to(resolved_out):
            raise ValueError("OCR checkpoint directory escapes the run")
        return checkpoint_dir / f"{checkpoint_hash}.json"

    def _load_ocr_checkpoint(
        self,
        *,
        out: Path,
        checkpoint_hash: str,
        identity: dict[str, object],
        target: dict[str, object],
    ) -> dict[str, object] | None:
        path = self._ocr_checkpoint_path(out, checkpoint_hash)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if payload.get("schema_version") != _OCR_CHECKPOINT_SCHEMA_VERSION:
                return None
            if payload.get("checkpoint_hash") != checkpoint_hash:
                return None
            if payload.get("identity") != identity:
                return None
            evidence = payload.get("evidence")
            if not isinstance(evidence, dict) or evidence.get("target") != target:
                return None
            status = evidence.get("status")
            if status == "completed":
                result = evidence.get("result")
                if not isinstance(result, dict):
                    return None
                OcrResult.model_validate(result)
            elif status != "not_executed_no_backend":
                return None
            return evidence
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_ocr_checkpoint(
        self,
        *,
        out: Path,
        checkpoint_hash: str,
        identity: dict[str, object],
        evidence: dict[str, object],
    ) -> None:
        path = self._ocr_checkpoint_path(out, checkpoint_hash)
        atomic_write_json(
            path,
            {
                "schema_version": _OCR_CHECKPOINT_SCHEMA_VERSION,
                "checkpoint_hash": checkpoint_hash,
                "identity": identity,
                "evidence": evidence,
            },
        )

    @staticmethod
    def _ocr_target_label(target: dict[str, object]) -> str:
        page_number = int(target.get("page_number", 0))
        kind = str(target.get("kind", "unknown"))
        image = Path(str(target.get("image", "target"))).stem
        return f"p{page_number:04d}-{kind}-{image}"

    def _execute_ocr_target_checkpointed(
        self,
        prepared: PreparedRun,
        target: dict[str, object],
        out: Path,
        *,
        current: int,
        total: int,
    ) -> tuple[dict[str, object], bool]:
        started = perf_counter()
        checkpoint_hash, identity = self._ocr_checkpoint_identity(
            prepared, target, out
        )
        cached = self._load_ocr_checkpoint(
            out=out,
            checkpoint_hash=checkpoint_hash,
            identity=identity,
            target=target,
        )
        label = self._ocr_target_label(target)
        if cached is not None:
            elapsed = perf_counter() - started
            print(
                f"OCR {current}/{total} REUSED {label} {elapsed:.2f}s",
                flush=True,
            )
            return cached, True

        stable_page_id = target.get("stable_page_id")
        shared_cache = SharedOcrCache(prepared.state_db.parent / "cache")
        backend = self._backend_metadata()
        if isinstance(stable_page_id, str) and len(stable_page_id) == 64:
            shared = shared_cache.load(
                stable_page_id=stable_page_id,
                target=target,
                backend=backend,
            )
            if shared is not None:
                cached_result = OcrResult.model_validate(shared.result)
                enriched = self._attach_input_metadata(cached_result, target)
                evidence: dict[str, object] = {
                    "target": target,
                    "status": "completed",
                    "result": enriched.model_dump(mode="json"),
                    "cache": {
                        "scope": "shared_page",
                        "reused": True,
                        "stable_page_id": stable_page_id,
                        "provenance": shared.provenance,
                    },
                }
                self._write_ocr_checkpoint(
                    out=out,
                    checkpoint_hash=checkpoint_hash,
                    identity=identity,
                    evidence=evidence,
                )
                elapsed = perf_counter() - started
                print(
                    f"OCR {current}/{total} PAGE-CACHE {label} {elapsed:.2f}s",
                    flush=True,
                )
                return evidence, True

        evidence = self._execute_ocr_target(target, out)
        if (
            evidence.get("status") == "completed"
            and isinstance(stable_page_id, str)
            and len(stable_page_id) == 64
            and isinstance(evidence.get("result"), dict)
        ):
            shared_cache.store(
                stable_page_id=stable_page_id,
                target=target,
                backend=backend,
                result=evidence["result"],  # type: ignore[arg-type]
                provenance={
                    "source_sha256": prepared.manifest.source.sha256,
                    "source_page": int(target["page_number"]),
                    "run_id": prepared.manifest.run_id,
                },
            )
        self._write_ocr_checkpoint(
            out=out,
            checkpoint_hash=checkpoint_hash,
            identity=identity,
            evidence=evidence,
        )
        elapsed = perf_counter() - started
        print(
            f"OCR {current}/{total} DONE   {label} {elapsed:.2f}s",
            flush=True,
        )
        return evidence, False

    def _full_page_target(self, page, out: Path) -> dict[str, object]:
        assert page.rendered_image is not None
        image_path = self._resolve_artifact(out, page.rendered_image)
        pixmap = pymupdf.Pixmap(str(image_path))
        bbox = BBox(x0=0.0, y0=0.0, x1=page.width, y1=page.height)
        return self._target_metadata(
            kind="full_page",
            page_number=page.page_number,
            image_reference=page.rendered_image,
            image_path=image_path,
            image_width=pixmap.width,
            image_height=pixmap.height,
            bbox=bbox,
            dpi=self.inspector.render_dpi,
            padding=0.0,
            page_rotation=page.rotation,
            reason=(
                "low_trust_embedded_text_layer"
                if page.text_layer_trust.value == "low"
                else "insufficient_or_low_quality_native_text"
            ),
            extra={"stable_page_id": page.stable_page_id},
        )

    def _attach_input_metadata(
        self, result: OcrResult, target: dict[str, object]
    ) -> OcrResult:
        transform = CoordinateTransform.model_validate(target["coordinate_transform"])
        bbox_values = target["bbox"]
        assert isinstance(bbox_values, list)
        input_metadata = OcrInputMetadata(
            source_kind=str(target["kind"]),
            page_number=int(target["page_number"]),
            image=str(target["image"]),
            image_sha256=str(target["image_sha256"]),
            dpi=int(target["dpi"]),
            crop_bbox=BBox(
                x0=float(bbox_values[0]),
                y0=float(bbox_values[1]),
                x1=float(bbox_values[2]),
                y1=float(bbox_values[3]),
            ),
            crop_padding_points=float(target["crop_padding_points"]),
            preprocessing=dict(target["preprocessing"]),  # type: ignore[arg-type]
            coordinate_transform=transform,
        )
        lines = [
            line.model_copy(
                update={"pdf_bbox": transform.map_bbox(line.bbox) if line.bbox else None}
            )
            for line in result.lines
        ]
        backend = result.backend
        if backend is None:
            metadata = getattr(self.ocr_engine, "metadata", None)
            backend = metadata if isinstance(metadata, OcrBackendMetadata) else None
        return result.model_copy(
            update={
                "backend": backend,
                "input": input_metadata,
                "lines": lines,
                "executed_at": result.executed_at or datetime.now(UTC),
            }
        )

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

    def _reconciliation_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        inspection: DocumentInspection,
        out: Path,
        *,
        upstream_hashes: list[str],
    ) -> tuple[ReconciliationResult, str]:
        step_name = "RECONCILIATION_WRITTEN"
        artifact = out / "reconciliation.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash, step_name, *upstream_hashes
        )
        if artifact.is_file():
            try:
                result = ReconciliationResult.model_validate_json(
                    artifact.read_text(encoding="utf-8")
                )
                if state.can_resume(
                    prepared.manifest.run_id,
                    step_name,
                    input_hash=input_hash,
                    version=_RECONCILIATION_STEP_VERSION,
                    output_hash=file_sha256(artifact),
                ):
                    return result, file_sha256(artifact)
            except (OSError, ValueError):
                pass

        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(
            state,
            prepared,
            step_name,
            input_hash,
            version=_RECONCILIATION_STEP_VERSION,
        )
        try:
            ocr_payload = json.loads((out / "ocr.json").read_text(encoding="utf-8"))
            review_payload = json.loads(
                (out / "review_manifest.json").read_text(encoding="utf-8")
            )
            result = build_reconciliation(inspection, ocr_payload, review_payload)
            atomic_write_json(artifact, result.model_dump(mode="json"))
            output_hash = file_sha256(artifact)
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return result, output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _repair_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        inspection: DocumentInspection,
        reconciliation: ReconciliationResult,
        out: Path,
        *,
        upstream_hashes: list[str],
    ) -> tuple[RepairResult, str]:
        step_name = "REPAIR_WRITTEN"
        artifact = out / "repair.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash, step_name, *upstream_hashes
        )
        if artifact.is_file():
            try:
                result = RepairResult.model_validate_json(
                    artifact.read_text(encoding="utf-8")
                )
                if state.can_resume(
                    prepared.manifest.run_id,
                    step_name,
                    input_hash=input_hash,
                    version=_REPAIR_STEP_VERSION,
                    output_hash=file_sha256(artifact),
                ):
                    return result, file_sha256(artifact)
            except (OSError, ValueError):
                pass

        self._archive_step_artifacts(prepared.output_dir, step_name)
        self._begin(
            state,
            prepared,
            step_name,
            input_hash,
            version=_REPAIR_STEP_VERSION,
        )
        try:
            result = build_repairs(inspection, reconciliation)
            atomic_write_json(artifact, result.model_dump(mode="json"))
            output_hash = file_sha256(artifact)
            state.complete_step(
                prepared.manifest.run_id, step_name, output_hash=output_hash
            )
            return result, output_hash
        except Exception as exc:
            self._fail(state, prepared, step_name, exc)
            raise

    def _problem_packet_step(
        self,
        state: RunStateStore,
        prepared: PreparedRun,
        inspection: DocumentInspection,
        reconciliation: ReconciliationResult,
        repair: RepairResult,
        out: Path,
        *,
        upstream_hashes: list[str],
    ) -> str:
        step_name = "PROBLEM_PACKET_WRITTEN"
        canonical_path = out / "canonical_source.json"
        markdown_path = out / problem_markdown_filename(
            prepared.manifest.subject, prepared.manifest.question
        )
        handoff_path = out / handoff_markdown_filename(
            prepared.manifest.subject, prepared.manifest.question
        )
        validation_path = out / "problem_validation.json"
        input_hash = self._hash_values(
            prepared.manifest.input_hash, step_name, *upstream_hashes
        )
        packet_paths = [canonical_path, markdown_path, handoff_path, validation_path]
        output_hash = self._problem_packet_bundle_hash(
            out, packet_paths, validation_path
        )
        if output_hash is not None and state.can_resume(
            prepared.manifest.run_id,
            step_name,
            input_hash=input_hash,
            version=_PROBLEM_PACKET_STEP_VERSION,
            output_hash=output_hash,
        ):
            return output_hash

        for path in packet_paths:
            self._archive_file(out, step_name, path)
        for path in (out / "handoff_review").glob("*.png"):
            self._archive_file(out, step_name, path)
        self._begin(
            state,
            prepared,
            step_name,
            input_hash,
            version=_PROBLEM_PACKET_STEP_VERSION,
        )
        try:
            ocr_payload = json.loads((out / "ocr.json").read_text(encoding="utf-8"))
            write_problem_packet(
                run_dir=out,
                manifest=prepared.manifest,
                inspection=inspection,
                reconciliation=reconciliation,
                repair=repair,
                ocr_payload=ocr_payload,
            )
            output_hash = self._problem_packet_bundle_hash(
                out, packet_paths, validation_path
            )
            if output_hash is None:
                raise RuntimeError(
                    "Problem packet is incomplete: review handoff artifacts are missing"
                )
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
        return (
            pymupdf.Rect(
                box.x0 - padding,
                box.y0 - padding,
                box.x1 + padding,
                box.y1 + padding,
            )
            & page_rect
        )

    @classmethod
    def _render_review_crops(
        cls,
        source: str | Path,
        inspection: DocumentInspection,
        crop_dir: Path,
        *,
        artifact_root: Path,
        dpi: int = 450,
    ) -> dict[int, list[dict[str, object]]]:
        crop_dir.mkdir(parents=True, exist_ok=True)
        document = pymupdf.open(source)
        manifest: dict[int, list[dict[str, object]]] = {}
        try:
            for inspected_page in inspection.pages:
                clusters = cluster_red_vector_evidence(inspected_page.vector_marks)
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
                            "kind": "red_vector_cluster",
                            "bbox": [clip.x0, clip.y0, clip.x1, clip.y1],
                            "image": cls._artifact_reference(artifact_root, path),
                        }
                    )
                manifest[inspected_page.page_number] = page_items
        finally:
            document.close()
        return manifest

    @staticmethod
    def _target_metadata(
        *,
        kind: str,
        page_number: int,
        image_reference: str,
        image_path: Path,
        image_width: int,
        image_height: int,
        bbox: BBox,
        dpi: int,
        padding: float,
        page_rotation: int,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        scale_x = (bbox.x1 - bbox.x0) / image_width
        scale_y = (bbox.y1 - bbox.y0) / image_height
        transform = CoordinateTransform(
            pixel_to_pdf=(scale_x, 0.0, 0.0, scale_y, bbox.x0, bbox.y0),
            image_width_px=image_width,
            image_height_px=image_height,
            pdf_bbox=bbox,
            page_rotation=page_rotation,
        )
        target: dict[str, object] = {
            "kind": kind,
            "page_number": page_number,
            "reason": reason,
            "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
            "image": image_reference,
            "image_sha256": file_sha256(image_path),
            "dpi": dpi,
            "crop_padding_points": padding,
            "preprocessing": {
                "renderer": "PyMuPDF",
                "colorspace": "rgb",
                "alpha": False,
                "deskew": False,
                "binarization": False,
                "contrast_adjustment": False,
            },
            "coordinate_transform": transform.model_dump(mode="json"),
        }
        if extra:
            target.update(extra)
        return target

    def _render_ocr_targets(
        self,
        source: str | Path,
        inspection: DocumentInspection,
        crop_dir: Path,
        *,
        artifact_root: Path,
    ) -> dict[int, list[dict[str, object]]]:
        """Render surgical native-text and independently routed image regions.

        This is the surgical OCR path. It avoids replacing an otherwise-good PDF
        text layer merely because a few glyphs are corrupted. Substantive image
        regions route independently so native page text cannot hide image text.
        """
        crop_dir.mkdir(parents=True, exist_ok=True)
        document = pymupdf.open(source)
        manifest: dict[int, list[dict[str, object]]] = {}
        try:
            for inspected_page in inspection.pages:
                page = document[inspected_page.page_number - 1]
                items: list[dict[str, object]] = []
                for index, region in enumerate(inspected_page.suspect_native_regions, start=1):
                    padding = self.routing_config.surgical_padding_points
                    dpi = self.routing_config.surgical_dpi
                    clip = self._clip_box(page.rect, region.bbox, padding=padding)
                    path = crop_dir / (
                        f"page-{inspected_page.page_number:04d}-suspect-{index:03d}.png"
                    )
                    pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
                    with atomic_output_path(path) as temporary:
                        pixmap.save(temporary)
                    items.append(
                        self._target_metadata(
                            kind="suspect_native_text",
                            page_number=inspected_page.page_number,
                            image_reference=self._artifact_reference(artifact_root, path),
                            image_path=path,
                            image_width=pixmap.width,
                            image_height=pixmap.height,
                            bbox=BBox(x0=clip.x0, y0=clip.y0, x1=clip.x1, y1=clip.y1),
                            dpi=dpi,
                            padding=padding,
                            page_rotation=inspected_page.rotation,
                            reason=region.reason,
                            extra={
                                "stable_page_id": inspected_page.stable_page_id,
                                "native_candidate": region.text,
                                "native_bbox": [
                                    region.bbox.x0,
                                    region.bbox.y0,
                                    region.bbox.x1,
                                    region.bbox.y1,
                                ],
                            },
                        )
                    )

                for image_region, reason in routed_image_regions(
                    inspected_page, self.routing_config
                ):
                    padding = self.routing_config.image_padding_points
                    dpi = self.routing_config.image_region_dpi
                    clip = self._clip_box(page.rect, image_region.bbox, padding=padding)
                    path = crop_dir / (
                        f"page-{inspected_page.page_number:04d}-image-"
                        f"{image_region.image_index:03d}.png"
                    )
                    pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
                    with atomic_output_path(path) as temporary:
                        pixmap.save(temporary)
                    items.append(
                        self._target_metadata(
                            kind="image_region",
                            page_number=inspected_page.page_number,
                            image_reference=self._artifact_reference(artifact_root, path),
                            image_path=path,
                            image_width=pixmap.width,
                            image_height=pixmap.height,
                            bbox=BBox(x0=clip.x0, y0=clip.y0, x1=clip.x1, y1=clip.y1),
                            dpi=dpi,
                            padding=padding,
                            page_rotation=inspected_page.rotation,
                            reason=reason,
                            extra={
                                "stable_page_id": inspected_page.stable_page_id,
                                "image_index": image_region.image_index,
                                "image_xref": image_region.xref,
                                "source_image_digest": image_region.digest,
                            },
                        )
                    )
                if items:
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
                    "marker_text_candidates": [
                        {
                            "color": m.color_name,
                            "text": m.extracted_text,
                            "confidence": m.confidence,
                            "bbox": m.rect.model_dump(),
                        }
                        for m in p.vector_marks
                        if m.kind == "marker_candidate"
                    ],
                    "suspect_native_regions": [r.model_dump() for r in p.suspect_native_regions],
                    "ocr_targets": ocr_targets.get(p.page_number, []),
                    "review_crops": review_crops.get(p.page_number, []),
                    "red_vector_regions": [
                        m.rect.model_dump()
                        for m in p.vector_marks
                        if m.kind == "red_vector_evidence"
                    ],
                }
                for p in inspection.pages
            ],
        }
        atomic_write_json(path, payload)
