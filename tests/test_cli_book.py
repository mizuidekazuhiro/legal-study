from typer.testing import CliRunner

from legal_study.cli import app


def test_book_commands_are_exposed_without_chat_or_cloud_dependencies() -> None:
    runner = CliRunner()

    one = runner.invoke(app, ["export-book", "--help"])
    many = runner.invoke(app, ["export-books", "--help"])

    assert one.exit_code == 0
    assert "--preserve-markup" in one.stdout
    assert "--verbatim" in one.stdout
    assert "--resume" in one.stdout
    assert many.exit_code == 0
