from legal_study.pdf.vector_marks import nearest_color


def test_known_goodnotes_colors() -> None:
    assert nearest_color((1.0, 1.0, 0.5137)) == "yellow"
    assert nearest_color((0.3451, 0.6941, 1.0)) == "blue"
    assert nearest_color((1.0, 0.7255, 0.3294)) == "orange"
    assert nearest_color((1.0, 0.1647, 0.1333)) == "red"
