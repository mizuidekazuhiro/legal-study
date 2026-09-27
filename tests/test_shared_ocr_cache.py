from legal_study.pdf.ocr.cache import shared_cache_identity


def test_shared_cache_identity_includes_rendered_image_hash() -> None:
    target = {
        "kind": "full_page",
        "bbox": [0, 0, 100, 100],
        "dpi": 300,
        "crop_padding_points": 0,
        "preprocessing": {},
        "image_sha256": "a" * 64,
    }
    first, _identity = shared_cache_identity(
        stable_page_id="b" * 64,
        target=target,
        backend=None,
    )
    second, _identity = shared_cache_identity(
        stable_page_id="b" * 64,
        target={**target, "image_sha256": "c" * 64},
        backend=None,
    )

    assert first != second
