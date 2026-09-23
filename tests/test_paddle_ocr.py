import json
import sys
import types
from pathlib import Path

import pytest

from legal_study.pdf.ocr import paddle


def _write_model_store(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in paddle.MODEL_NAMES.values():
        model_dir = root / "official_models" / name
        model_dir.mkdir(parents=True)
        (model_dir / "model.bin").write_bytes(f"model:{name}".encode())
        hashes[name] = paddle.model_directory_hash(model_dir)
    manifest = {
        "library_version": "3.7.0",
        "runtime_version": "3.3.1",
        "model_version": paddle.OCR_VERSION,
        "model_names": paddle.MODEL_NAMES,
        "model_hashes": hashes,
    }
    (root / paddle.MODEL_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return hashes


def _fake_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        paddle,
        "_installed_version",
        lambda name: {"paddleocr": "3.7.0", "paddlepaddle": "3.3.1"}.get(name),
    )


def test_offline_engine_refuses_missing_models_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_versions(monkeypatch)
    monkeypatch.setitem(sys.modules, "paddleocr", None)

    with pytest.raises(RuntimeError, match="warmup-ocr"):
        paddle.PaddleOcrEngine(model_root=tmp_path)


def test_offline_engine_uses_hashed_local_models_and_preserves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_hashes = _write_model_store(tmp_path)
    _fake_versions(monkeypatch)
    calls: list[dict[str, object]] = []

    class FakePaddleOcr:
        def __init__(self, **options: object) -> None:
            calls.append(options)

        def predict(self, _image: str) -> list[dict[str, object]]:
            return [
                {
                    "rec_texts": ["実行行為"],
                    "rec_scores": [0.98],
                    "rec_boxes": [[10, 20, 110, 50]],
                    "rec_polys": [[[10, 20], [110, 20], [110, 50], [10, 50]]],
                }
            ]

    fake_module = types.ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOcr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    engine = paddle.PaddleOcrEngine(model_root=tmp_path)
    assert calls == []
    result = engine.recognize(tmp_path / "crop.png")

    assert calls[0]["device"] == "cpu"
    assert calls[0]["enable_mkldnn"] is False
    assert calls[0]["text_detection_model_dir"] == str(
        tmp_path / "official_models" / "PP-OCRv6_medium_det"
    )
    assert result.text == "実行行為"
    assert result.lines[0].bbox is not None
    assert result.lines[0].polygon == [
        (10.0, 20.0),
        (110.0, 20.0),
        (110.0, 50.0),
        (10.0, 50.0),
    ]
    assert result.backend is not None
    assert result.backend.device == "cpu"
    assert result.backend.offline is True
    assert result.backend.model_hashes == expected_hashes
    assert result.executed_at is not None


def test_model_hash_mismatch_blocks_offline_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_model_store(tmp_path)
    _fake_versions(monkeypatch)
    changed = tmp_path / "official_models" / "PP-OCRv6_medium_det" / "model.bin"
    changed.write_bytes(b"changed")

    status = paddle.inspect_paddle_installation(tmp_path)

    assert status["ready"] is False
    assert "model_hash_mismatch:PP-OCRv6_medium_det" in status["problems"]


def test_runtime_version_change_requires_manifest_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_model_store(tmp_path)
    monkeypatch.setattr(
        paddle,
        "_installed_version",
        lambda name: {"paddleocr": "3.7.0", "paddlepaddle": "3.2.2"}.get(name),
    )

    status = paddle.inspect_paddle_installation(tmp_path)

    assert status["ready"] is False
    assert "runtime_version_mismatch:3.3.1:3.2.2" in status["problems"]


def test_engine_metadata_does_not_initialize_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_model_store(tmp_path)
    _fake_versions(monkeypatch)
    calls: list[dict[str, object]] = []

    class FakePaddleOcr:
        def __init__(self, **options: object) -> None:
            calls.append(options)

    fake_module = types.ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOcr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    engine = paddle.PaddleOcrEngine(model_root=tmp_path)
    metadata = engine.metadata

    assert metadata.engine == "paddleocr"
    assert calls == []
