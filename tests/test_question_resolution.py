from pathlib import Path

import pymupdf

from legal_study.completion.done_marker import done_stamp_png_bytes
from legal_study.completion.question_resolution import (
    apply_done_markers,
    resolve_question_for_done_page,
)
from legal_study.pdf.ocr.base import OcrLine, OcrResult
from legal_study.settings import LocalSettings
from legal_study.state import QuestionStateStore, QuestionStatus


class HeaderOcr:
    name = "fake-header"

    def __init__(self, mapping: dict[int, str]) -> None:
        self.mapping = mapping
        self.calls: list[int] = []

    def recognize(self, image_path: Path) -> OcrResult:
        page_number = int(image_path.name.split("-p", 1)[1].split("-", 1)[0])
        self.calls.append(page_number)
        text = self.mapping.get(page_number, "ordinary page")
        return OcrResult(
            engine=self.name,
            text=text,
            confidence=0.99,
            lines=[OcrLine(text=text, confidence=0.99)],
        )


def _question_pdf(path: Path, *, done_page: int = 5) -> None:
    document = pymupdf.open()
    for page_number in range(1, 7):
        page = document.new_page(width=400, height=550)
        page.insert_text((40, 80), f"page {page_number}")
        if page_number == done_page:
            page.insert_image(
                pymupdf.Rect(250, 470, 370, 514),
                stream=done_stamp_png_bytes(),
            )
    document.save(path)
    document.close()


def test_resolver_uses_nearest_preceding_exact_question_header(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    engine = HeaderOcr({2: "第21問", 4: "第22問"})

    resolved = resolve_question_for_done_page(
        pdf,
        5,
        ocr_engine=engine,
        temp_dir=tmp_path / "tmp",
        max_backtrack=5,
    )

    assert resolved.resolved is True
    assert resolved.question == "22"
    assert resolved.start_page == 4
    assert resolved.scanned_pages == [5, 4]
    assert engine.calls == [5, 4]


def test_apply_done_marker_sets_question_done_detected(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    settings = LocalSettings(home=tmp_path / "home")
    engine = HeaderOcr({2: "第22問"})

    results = apply_done_markers(
        pdf,
        subject="criminal",
        ocr_engine=engine,
        settings=settings,
        pages=[5],
        max_backtrack=4,
    )

    assert len(results) == 1
    result = results[0]
    assert result.done_detection.detected is True
    assert result.resolution.question == "22"
    assert result.resolution.start_page == 2
    assert result.current_status == QuestionStatus.DONE_DETECTED
    assert result.state_updated is True

    stored = QuestionStateStore(settings.state_db).get("criminal", "22")
    assert stored is not None
    assert stored.status == QuestionStatus.DONE_DETECTED
    assert stored.latest_source_sha256 == result.source_sha256
    assert len(stored.stable_page_ids) == 4


def test_apply_done_marker_is_idempotent_for_same_source(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    settings = LocalSettings(home=tmp_path / "home")
    engine = HeaderOcr({2: "第22問"})

    first = apply_done_markers(
        pdf,
        subject="criminal",
        ocr_engine=engine,
        settings=settings,
        pages=[5],
        max_backtrack=4,
    )[0]
    second = apply_done_markers(
        pdf,
        subject="criminal",
        ocr_engine=engine,
        settings=settings,
        pages=[5],
        max_backtrack=4,
    )[0]

    assert first.state_updated is True
    assert second.previous_status == QuestionStatus.DONE_DETECTED
    assert second.current_status == QuestionStatus.DONE_DETECTED
    assert second.state_updated is False


def test_unresolved_header_never_changes_question_state(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    settings = LocalSettings(home=tmp_path / "home")
    engine = HeaderOcr({})

    results = apply_done_markers(
        pdf,
        subject="criminal",
        ocr_engine=engine,
        settings=settings,
        pages=[5],
        max_backtrack=4,
    )

    assert len(results) == 1
    assert results[0].resolution.resolved is False
    assert results[0].current_status is None
    assert QuestionStateStore(settings.state_db).get("criminal", "22") is None


def test_ambiguous_nearer_heading_never_falls_back_to_previous_question(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    engine = HeaderOcr({2: "第15問", 4: "第I6問"})

    resolution = resolve_question_for_done_page(
        pdf,
        5,
        ocr_engine=engine,
        temp_dir=tmp_path / "tmp",
        max_backtrack=5,
    )

    assert resolution.resolved is False
    assert resolution.question is None
    assert resolution.scanned_pages == [5, 4]
    assert engine.calls == [5, 4]


def test_two_distinct_headings_on_one_page_are_unresolved(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    engine = HeaderOcr({2: "第15問", 4: "第16問\n第17問"})

    resolution = resolve_question_for_done_page(
        pdf,
        5,
        ocr_engine=engine,
        temp_dir=tmp_path / "tmp",
        max_backtrack=5,
    )

    assert resolution.resolved is False
    assert resolution.question is None
    assert resolution.scanned_pages == [5, 4]


def test_auxiliary_heading_is_not_used_as_question_start(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    engine = HeaderOcr(
        {
            2: "第26問\n26-1\n甲の罪責を論ぜよ。",
            3: "第26問 指針\n26-4\n検討の指針",
        }
    )

    resolution = resolve_question_for_done_page(
        pdf,
        5,
        ocr_engine=engine,
        temp_dir=tmp_path / "tmp",
        max_backtrack=5,
    )

    assert resolution.resolved is True
    assert resolution.question == "26"
    assert resolution.start_page == 2
    assert any(
        item.page_number == 3 and item.decision == "REJECT_AUXILIARY_HEADING"
        for item in resolution.candidates
    )


def test_problem_and_guidance_on_same_page_is_valid_start(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    engine = HeaderOcr(
        {
            2: "第27問\n27-1\n次の事例について甲の罪責を論ぜよ。\n指針",
        }
    )

    resolution = resolve_question_for_done_page(
        pdf,
        5,
        ocr_engine=engine,
        temp_dir=tmp_path / "tmp",
        max_backtrack=5,
    )

    assert resolution.resolved is True
    assert resolution.question == "27"
    assert resolution.start_page == 2
    assert resolution.candidates[-1].decision == "ACCEPT_PROBLEM_START"


def test_contents_and_cross_reference_are_not_problem_starts(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    _question_pdf(pdf)
    engine = HeaderOcr(
        {
            1: "第28問\n28-1\n次の事例について論ぜよ。",
            2: "目次\n第28問 200頁\n第29問 204頁",
            4: "第28問を参照して検討する。",
        }
    )

    resolution = resolve_question_for_done_page(
        pdf,
        5,
        ocr_engine=engine,
        temp_dir=tmp_path / "tmp",
        max_backtrack=5,
    )

    assert resolution.resolved is True
    assert resolution.start_page == 1
    assert {item.decision for item in resolution.candidates} >= {
        "REJECT_CROSS_REFERENCE",
        "REJECT_TABLE_OF_CONTENTS",
        "ACCEPT_PROBLEM_START",
    }
