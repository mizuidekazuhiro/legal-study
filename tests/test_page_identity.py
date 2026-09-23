from pathlib import Path

import pymupdf

from legal_study.page_identity import (
    align_page_indexes,
    build_source_page_index,
)
from legal_study.settings import LocalSettings
from legal_study.source_store import snapshot_source


def _write_version(
    path: Path,
    *,
    insert_front: bool,
    mark_base_page: bool,
) -> None:
    document = pymupdf.open()
    if insert_front:
        inserted = document.new_page(width=300, height=200)
        inserted.insert_text((30, 50), "inserted page")
    first = document.new_page(width=300, height=200)
    first.insert_text((30, 50), "stable base page A")
    second = document.new_page(width=300, height=200)
    second.insert_text((30, 50), "stable base page B")
    if mark_base_page:
        shape = second.new_shape()
        shape.draw_line((30, 58), (180, 58))
        shape.finish(color=(1.0, 1.0, 0.514), width=8)
        shape.commit()
    document.save(path)
    document.close()


def test_page_alignment_survives_insertion_move_and_markup_change(tmp_path: Path) -> None:
    first_dir = tmp_path / "v1"
    second_dir = tmp_path / "v2"
    first_dir.mkdir()
    second_dir.mkdir()
    first_path = first_dir / "source.pdf"
    second_path = second_dir / "source.pdf"
    _write_version(first_path, insert_front=False, mark_base_page=False)
    _write_version(second_path, insert_front=True, mark_base_page=True)

    settings = LocalSettings(home=tmp_path / "home")
    first_snapshot = snapshot_source(first_path, settings=settings)
    second_snapshot = snapshot_source(second_path, settings=settings)
    first = build_source_page_index(first_snapshot)
    second = build_source_page_index(second_snapshot)

    assert first.pages[0].stable_page_id == second.pages[1].stable_page_id
    assert first.pages[1].stable_page_id == second.pages[2].stable_page_id
    assert first.pages[1].vector_fingerprint != second.pages[2].vector_fingerprint

    alignment = align_page_indexes(first, second)
    by_current = {
        record.current_page: record
        for record in alignment.records
        if record.current_page is not None
    }
    assert by_current[1].classification == "NEW"
    assert by_current[2].classification == "MOVED"
    assert by_current[2].safe_for_base_ocr_reuse is True
    assert by_current[3].classification == "MOVED_MARKUP_CHANGED"
    assert by_current[3].markup_changed is True
    assert by_current[3].safe_for_base_ocr_reuse is True
