from legal_study.pdf.quality import is_suspicious_char, native_text_quality, useful_char_count


def test_japanese_text_scores_high() -> None:
    text = "甲はVを殺害しようと考え、劇薬をワインに入れた。"
    assert native_text_quality(text) > 0.9
    assert useful_char_count(text) > 10


def test_replacement_garbage_scores_low() -> None:
    text = "\ufffd\ufffd\ufffd\ufffd\ufffd\ue000\ue001"
    assert native_text_quality(text) < 0.2


def test_japanese_iteration_and_closing_marks_are_not_suspicious() -> None:
    text = "人々、各々、〆切、〇、〻"

    assert native_text_quality(text) > 0.9
    assert all(not is_suspicious_char(character) for character in "々〆〇〻")
