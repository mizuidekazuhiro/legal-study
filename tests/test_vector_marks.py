from legal_study.pdf.vector_marks import (
    classify_color_family,
    cluster_page_palette,
    nearest_color,
)


def test_known_goodnotes_colors() -> None:
    assert nearest_color((1.0, 1.0, 0.5137)) == "yellow"
    assert nearest_color((0.3451, 0.6941, 1.0)) == "blue"
    assert nearest_color((1.0, 0.7255, 0.3294)) == "orange"
    assert nearest_color((1.0, 0.1647, 0.1333)) == "red"


def test_saturated_palette_classification_includes_green() -> None:
    assert classify_color_family((0.28, 0.92, 0.34)) == "green"
    assert classify_color_family((0.98, 0.93, 0.24)) == "yellow"
    assert classify_color_family((0.95, 0.20, 0.18)) == "red"
    assert classify_color_family((0.91, 0.91, 0.91)) is None


def test_page_palette_clusters_nearby_colors_without_assigning_meaning() -> None:
    clusters = cluster_page_palette(
        [
            (0.24, 0.90, 0.31),
            (0.29, 0.95, 0.37),
            (0.98, 0.92, 0.20),
        ]
    )

    assert [cluster.color_family for cluster in clusters] == ["green", "yellow"]
    assert clusters[0].member_count == 2
