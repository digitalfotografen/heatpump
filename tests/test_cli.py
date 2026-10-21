from typer.testing import CliRunner

from heatpump.cli import app

runner = CliRunner()


def test_help_shows_the_whole_surface() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ["run", "status", "db", "prices", "plan", "relay", "hat", "service", "replay"]:
        assert name in result.output
