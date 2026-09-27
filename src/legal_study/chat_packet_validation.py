from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from legal_study.io_utils import atomic_write_json
from legal_study.problem_packet import handoff_markdown_filename
from legal_study.run_manifest import RunManifest

_AUXILIARY = ("指針", "総括", "MEMO", "メモ", "答案例", "答案")
_PROBLEM_CUES = ("問題文", "設問", "次の", "以下", "罪責", "論ぜ", "答えよ")
_ANSWER_START = ("答案例", "講師答案")
_ANSWER_END = ("以上", "総括")


def validate_material_completeness(run_dir: Path) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    manifest = RunManifest.model_validate_json(
        (root / "run_manifest.json").read_text(encoding="utf-8")
    )
    handoff = root / handoff_markdown_filename(manifest.subject, manifest.question)
    if not handoff.is_file():
        raise FileNotFoundError(f"Handoff Markdown is missing: {handoff}")
    text = handoff.read_text(encoding="utf-8")
    normalized = text.replace("－", "-")
    question = re.escape(manifest.question)
    title_lines = [
        line.strip()
        for line in normalized.splitlines()
        if re.fullmatch(rf"第\s*{question}\s*問(?:\s+.*)?", line.strip())
    ]
    valid_title = any(not any(role in line for role in _AUXILIARY) for line in title_lines)
    booklet_start = bool(re.search(rf"(?<!\d){question}\s*-\s*1(?!\d)", normalized))
    problem_cue = any(cue in normalized for cue in _PROBLEM_CUES)
    answer_start = any(value in normalized for value in _ANSWER_START)
    answer_end = any(value in normalized for value in _ANSWER_END)
    title_offset = min(
        (
            normalized.find(line)
            for line in title_lines
            if not any(role in line for role in _AUXILIARY)
        ),
        default=-1,
    )
    problem_offset = min(
        (normalized.find(cue, title_offset) for cue in _PROBLEM_CUES if cue in normalized),
        default=-1,
    )
    answer_offset = min(
        (normalized.find(value) for value in _ANSWER_START if value in normalized),
        default=-1,
    )
    answer_end_offset = max(normalized.rfind(value) for value in _ANSWER_END)
    requested = list(manifest.requested_pages or [])
    text_pages = [page for page in requested if f"## PDF page {page}" in normalized]
    image_pages = [
        page
        for page in requested
        if (root / "handoff_review" / f"page-{page:04d}-review.png").is_file()
    ]
    checks = {
        "problem_start_identified": valid_title and (booklet_start or problem_cue),
        "problem_or_question_text_present": problem_cue,
        "answer_start_present": answer_start,
        "answer_end_present": answer_end,
        "material_sections_in_order": (
            -1 < title_offset <= problem_offset < answer_offset < answer_end_offset
        ),
        "requested_page_text_present": text_pages == requested,
        "requested_page_images_present": image_pages == requested,
    }
    missing = [name for name, passed in checks.items() if not passed]
    result = {
        "schema_version": 1,
        "status": "PASS" if not missing else "FAIL",
        "checks": checks,
        "missing": missing,
        "requested_pages": requested,
        "text_pages": text_pages,
        "image_pages": image_pages,
        "basis": "mechanical_prepublication_check",
    }
    atomic_write_json(root / "material_validation.json", result)
    return result


def validate_chat_packet_structure(packet: Path) -> dict[str, Any]:
    target = packet.expanduser().resolve()
    try:
        with zipfile.ZipFile(target) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            safe = all(
                not PurePosixPath(name).is_absolute()
                and ".." not in PurePosixPath(name).parts
                and "\\" not in name
                for name in names
            )
            unique = len(names) == len(set(names))
            crc_ok = archive.testzip() is None
            manifest = json.loads(archive.read("packet_manifest.json"))
            requested = [int(page) for page in manifest.get("requested_pages") or []]
            expected_reviews = {f"review/page-{page:04d}-review.png" for page in requested}
            handoff = archive.read("handoff.md").decode("utf-8")
            marker_payload = json.loads(archive.read("marker_index.json"))
            logical_markers_value = marker_payload.get("logical_markers")
            logical_markers_are_list = isinstance(logical_markers_value, list)
            logical_markers = logical_markers_value if logical_markers_are_list else []
            packet_schema = manifest.get("schema_version")
            supported_packet_schema = packet_schema in {"chat_packet.v1", "chat_packet.v2"}
            marker_schema_v2 = packet_schema == "chat_packet.v2"
            declared_marker_schema = manifest.get("marker_index_schema_version")
            payload_marker_schema = marker_payload.get("schema_version")
            if marker_schema_v2:
                marker_schema_consistent = (
                    declared_marker_schema == "marker_index.v2"
                    and payload_marker_schema == "marker_index.v2"
                )
                range_semantics_valid = (
                    marker_payload.get("range_semantics")
                    == "page_unicode_codepoints_end_exclusive"
                )
            else:
                marker_schema_consistent = (
                    declared_marker_schema in {None, "marker_index.v1"}
                    and payload_marker_schema in {None, "marker_index.v1"}
                )
                range_semantics_valid = True

            page_text_present = "page_text.json" in names
            page_text_payload = (
                json.loads(archive.read("page_text.json"))
                if marker_schema_v2 and page_text_present
                else {"pages": []}
            )
            page_text_schema_valid = (
                not marker_schema_v2
                or page_text_payload.get("schema_version") == "page_text.v1"
            )
            raw_page_text_entries = page_text_payload.get("pages", [])
            page_text_entries_valid = isinstance(raw_page_text_entries, list) and all(
                isinstance(page, dict) and page.get("page_number") is not None
                for page in raw_page_text_entries
            )
            page_text_entries = raw_page_text_entries if page_text_entries_valid else []
            page_text_numbers = [int(page["page_number"]) for page in page_text_entries]
            page_text_pages_match_manifest = (
                not marker_schema_v2
                or (
                    len(page_text_numbers) == len(requested)
                    and len(page_text_numbers) == len(set(page_text_numbers))
                    and set(page_text_numbers) == set(requested)
                )
            )
            page_text_hashes_valid = (
                not marker_schema_v2
                or (
                    page_text_pages_match_manifest
                    and all(
                        isinstance(page.get("text"), str)
                        and isinstance(page.get("text_sha256"), str)
                        and page["text_sha256"]
                        == hashlib.sha256(page["text"].encode("utf-8")).hexdigest()
                        for page in page_text_entries
                    )
                )
            )
            page_texts = {
                int(page["page_number"]): page
                for page in page_text_entries
            }
            declared_marker_count = manifest.get("logical_marker_count")
            marker_count_matches_manifest = (
                not marker_schema_v2
                or (
                    type(declared_marker_count) is int
                    and logical_markers_are_list
                    and len(logical_markers) == declared_marker_count
                )
            )
            marker_refs_valid = (
                not marker_schema_v2
                or (
                    marker_schema_consistent
                    and page_text_present
                    and page_text_schema_valid
                    and marker_count_matches_manifest
                )
            )
            if marker_schema_v2:
                for marker in logical_markers:
                    page = page_texts.get(int(marker["page_number"]))
                    reference = marker.get("text_reference")
                    if page is None or not isinstance(reference, dict):
                        marker_refs_valid = False
                        continue
                    text = str(page.get("text") or "")
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    start = marker.get("canonical_start_char")
                    end = marker.get("canonical_end_char_exclusive")
                    marker_range_semantics_valid = (
                        marker.get("character_range_semantics")
                        == "page_unicode_codepoints_end_exclusive"
                    )
                    range_valid = start is None and end is None and marker.get("exact_text") is None
                    if isinstance(start, int) and isinstance(end, int):
                        range_valid = 0 <= start <= end <= len(text) and text[start:end] == marker.get(
                            "exact_text"
                        )
                    auto_verified = marker.get("review_status") == "AUTO_VERIFIED"
                    statuses_valid = (
                        marker.get("review_status")
                        in {"AUTO_VERIFIED", "NEEDS_REVIEW"}
                        and marker.get("position_status")
                        in {"VERIFIED", "NEEDS_REVIEW"}
                        and marker.get("text_accuracy_status")
                        in {"VERIFIED", "NEEDS_REVIEW"}
                    )
                    reference_source_valid = (
                        reference.get("field") == "canonical_text"
                        and reference.get("source") == page.get("source")
                    )
                    auto_verified_consistent = (
                        not auto_verified
                        or (
                            isinstance(start, int)
                            and isinstance(end, int)
                            and start < end
                            and isinstance(marker.get("exact_text"), str)
                            and bool(marker.get("exact_text"))
                            and range_valid
                            and marker.get("position_status") == "VERIFIED"
                            and marker.get("text_accuracy_status") == "VERIFIED"
                            and page.get("source") == "reconciled_text"
                        )
                    )
                    expected_evidence = (
                        f"review/page-{int(marker['page_number']):04d}-review.png"
                    )
                    marker_refs_valid = marker_refs_valid and (
                        digest == page.get("text_sha256") == reference.get("text_sha256")
                        and int(reference.get("page_number", -1)) == int(marker["page_number"])
                        and reference_source_valid
                        and range_semantics_valid
                        and marker_range_semantics_valid
                        and statuses_valid
                        and range_valid
                        and auto_verified_consistent
                        and (
                            auto_verified
                            or marker.get("evidence_image") == expected_evidence
                        )
                    )
            checks = {
                "paths_safe": safe,
                "paths_unique": unique,
                "crc_valid": crc_ok,
                "required_files_present": {
                    "packet_manifest.json",
                    "handoff.md",
                    "marker_index.json",
                    "CHAT_INSTRUCTIONS.md",
                }.issubset(names),
                "packet_schema_supported": supported_packet_schema,
                "marker_schema_matches_manifest": marker_schema_consistent,
                "range_semantics_valid_for_v2": range_semantics_valid,
                "page_text_present_for_v2": not marker_schema_v2 or page_text_present,
                "page_text_schema_valid_for_v2": page_text_schema_valid,
                "page_text_entries_valid_for_v2": not marker_schema_v2 or page_text_entries_valid,
                "page_text_pages_match_manifest": page_text_pages_match_manifest,
                "page_text_hashes_valid": page_text_hashes_valid,
                "marker_count_matches_manifest": marker_count_matches_manifest,
                "review_pages_match_manifest": expected_reviews
                == {name for name in names if name.startswith("review/")},
                "handoff_pages_match_manifest": all(
                    f"## PDF page {page}" in handoff for page in requested
                ),
                "review_count_matches": manifest.get("review_sheet_count") == len(expected_reviews),
                "marker_text_references_valid": marker_refs_valid,
            }
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"Chat packet structural validation failed: {target}") from exc
    result = {"valid": all(checks.values()), "checks": checks}
    if not result["valid"]:
        raise RuntimeError(f"Chat packet structural validation failed: {checks}")
    return result
