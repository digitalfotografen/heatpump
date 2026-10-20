import pytest
from typer.testing import CliRunner

from heatpump.cli import app

runner = CliRunner()

STUB_COMMANDS = [
    ["run"],
    ["status"],
    ["relay", "status"],
    ["hat", "scan"],
    ["mqtt", "publish-state"],
    ["service", "status"],
]


def test_help_shows_the_whole_surface() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ["run", "status", "db", "prices", "plan", "relay", "hat", "service", "replay"]:
        assert name in result.output


@pytest.mark.parametrize("args", STUB_COMMANDS)
def test_stubs_exit_non_zero_and_name_the_phase(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "Not implemented in this phase" in result.output
