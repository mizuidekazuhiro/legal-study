from legal_study.pdf.quality import native_text_quality, useful_char_count


def test_japanese_text_scores_high() -> None:
    text = "甲はVを殺害しようと考え、劇薬をワインに入れた。"
    assert native_text_quality(text) > 0.9
    assert useful_char_count(text) > 10


def test_replacement_garbage_scores_low() -> None:
    text = "\ufffd\ufffd\ufffd\ufffd\ufffd\ue000\ue001"
    assert native_text_quality(text) < 0.2
