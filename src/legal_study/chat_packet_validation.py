from __future__ import annotations

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
    valid_title = any(
        not any(role in line for role in _AUXILIARY) for line in title_lines
    )
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
    text_pages = [
        page
        for page in requested
        if f"## PDF page {page}" in normalized
    ]
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
            expected_reviews = {
                f"review/page-{page:04d}-review.png" for page in requested
            }
            handoff = archive.read("handoff.md").decode("utf-8")
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
                "review_pages_match_manifest": expected_reviews
                == {name for name in names if name.startswith("review/")},
                "handoff_pages_match_manifest": all(
                    f"## PDF page {page}" in handoff for page in requested
                ),
                "review_count_matches": manifest.get("review_sheet_count")
                == len(expected_reviews),
            }
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"Chat packet structural validation failed: {target}") from exc
    result = {"valid": all(checks.values()), "checks": checks}
    if not result["valid"]:
        raise RuntimeError(f"Chat packet structural validation failed: {checks}")
    return result
