from legal_study.book.markup import WordBox, extract_rank_annotation, link_words_to_bbox
from legal_study.book.text_verification import BookTextStatus
from legal_study.models import BBox


def test_highlight_bbox_links_only_intersecting_words_in_reading_order() -> None:
    words = [
        WordBox(text="構成要件", bbox=BBox(x0=100, y0=100, x1=170, y1=120), order=2),
        WordBox(text="罪刑法定主義機能", bbox=BBox(x0=180, y0=100, x1=320, y1=120), order=3),
        WordBox(text="欄外", bbox=BBox(x0=500, y0=100, x1=550, y1=120), order=1),
    ]

    linked = link_words_to_bbox(
        words,
        BBox(x0=95, y0=96, x1=330, y1=124),
    )

    assert linked == "構成要件 罪刑法定主義機能"


def test_rank_uses_crop_ocr_when_native_mapping_is_ambiguous() -> None:
    rank = extract_rank_annotation(
        raw_text="BtRank",
        ocr_text="B+ Rank",
        ocr_confidence=0.97,
        pdf_page=31,
        bbox=BBox(x0=500, y0=500, x1=580, y1=550),
        linked_heading="2 客観的構成要件要素",
    )

    assert rank.raw_text == "BtRank"
    assert rank.text == "B+ Rank"
    assert rank.status == BookTextStatus.OCR_VERIFIED
    assert rank.linked_heading == "2 客観的構成要件要素"


def test_uncertain_rank_is_not_guessed() -> None:
    rank = extract_rank_annotation(
        raw_text="BtRank",
        ocr_text=None,
        ocr_confidence=None,
        pdf_page=31,
        bbox=BBox(x0=500, y0=500, x1=580, y1=550),
    )

    assert rank.text is None
    assert rank.status == BookTextStatus.NEEDS_REVIEW

