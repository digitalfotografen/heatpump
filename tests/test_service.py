from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import service
from heatpump.cli import app

runner = CliRunner()


def test_unit_is_rendered_from_the_template() -> None:
    unit = service.render_unit(
        user="pi",
        executable=Path("/home/pi/heatpump/.venv/bin/heatpump"),
        config=Path("/etc/heatpump/config.yaml"),
        env_file=Path("/etc/heatpump/env"),
    )
    assert (
        "ExecStart=/home/pi/heatpump/.venv/bin/heatpump "
        "--config /etc/heatpump/config.yaml run" in unit
    )
    assert "User=pi" in unit
    assert "EnvironmentFile=/etc/heatpump/env" in unit
    assert "Restart=always" in unit
    assert "RestartSec=10" in unit
    assert "After=network-online.target" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "$" not in unit, "every placeholder must be filled"


def test_install_writes_the_unit_to_the_given_path(tmp_path: Path) -> None:
    target = tmp_path / "heatpump.service"
    result = runner.invoke(
        app,
        ["--config", "/etc/heatpump/config.yaml", "service", "install", "--output", str(target)],
    )
    assert result.exit_code == 0, result.output
    assert "--config /etc/heatpump/config.yaml run" in target.read_text()
    assert "systemctl enable --now heatpump" in result.output


def test_install_defaults_the_config_to_etc(tmp_path: Path) -> None:
    target = tmp_path / "heatpump.service"
    result = runner.invoke(app, ["service", "install", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert "--config /etc/heatpump/config.yaml run" in target.read_text()


def test_install_print_does_not_write(tmp_path: Path) -> None:
    target = tmp_path / "heatpump.service"
    result = runner.invoke(app, ["service", "install", "--print", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert "[Service]" in result.output
    assert not target.exists()


def test_install_reports_a_path_it_cannot_write(tmp_path: Path) -> None:
    target = tmp_path / "missing-dir" / "heatpump.service"
    result = runner.invoke(app, ["service", "install", "--output", str(target)])
    assert result.exit_code == 1
    assert "sudo" in result.output


def test_status_runs_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Completed:
        returncode = 3  # systemctl status: unit not active

    monkeypatch.setattr(service.subprocess, "run", lambda cmd, **_: calls.append(cmd) or Completed)
    result = runner.invoke(app, ["service", "status"])
    assert calls == [["systemctl", "status", "heatpump", "--no-pager"]]
    assert result.exit_code == 3


def test_status_without_systemctl_exits_with_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(service.subprocess, "run", missing)
    result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 1
    assert "systemctl" in result.output
