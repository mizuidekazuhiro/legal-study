from pathlib import Path

from typer.testing import CliRunner

from legal_study import cli

runner = CliRunner()


def test_doctor_ocr_reports_offline_readiness_without_warmup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LEGAL_STUDY_HOME", str(tmp_path / "home"))
    calls: list[Path] = []

    def inspect(root: Path):
        calls.append(root)
        return {"ready": False, "problems": ["missing_model:test"]}

    monkeypatch.setattr(cli, "inspect_paddle_installation", inspect)

    result = runner.invoke(cli.app, ["doctor", "--ocr"])

    assert result.exit_code == 1
    assert calls == [(tmp_path / "home" / "models" / "paddleocr").resolve()]
    assert "NOT READY" in result.stdout


def test_warmup_ocr_is_the_explicit_model_download_command(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LEGAL_STUDY_HOME", str(tmp_path / "home"))
    calls: list[Path] = []

    def warmup(root: Path):
        calls.append(root)
        return {"ready": True, "problems": []}

    monkeypatch.setattr(cli, "warmup_paddle_models", warmup)

    result = runner.invoke(cli.app, ["warmup-ocr"])

    assert result.exit_code == 0
    assert calls == [(tmp_path / "home" / "models" / "paddleocr").resolve()]
    assert "may use the network" in result.stdout
