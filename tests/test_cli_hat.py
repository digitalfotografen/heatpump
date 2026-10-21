from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_i2c import FakeSMBus
from typer.testing import CliRunner

from heatpump import cli, db, relay
from heatpump.cli import app
from heatpump.i2c import I2CBus
from heatpump.models import TemperatureReading
from heatpump.relay import ServiceLock

runner = CliRunner()


@pytest.fixture
def smbus(monkeypatch: pytest.MonkeyPatch) -> FakeSMBus:
    """A HAT with the expander at 0x22 and the thermocouple at 0x66, used by the CLI."""
    fake = FakeSMBus(devices={0x66}, mcp23017={0x22})
    bus = I2CBus(fake)
    monkeypatch.setattr(cli, "shared_bus", lambda number: bus)
    monkeypatch.setattr(relay, "shared_bus", lambda number: bus)
    return fake


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    return tmp_path / "heatpump.lock"


def write_config(tmp_path: Path, lock_file: Path, driver: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"relay:\n  driver: {driver}\n"
        f"database:\n  path: {tmp_path / 'data' / 'heatpump.sqlite3'}\n"
        f"control:\n  lock_file: {lock_file}\n"
    )
    return path


@pytest.fixture
def hat_config(tmp_path: Path, lock_file: Path) -> Path:
    return write_config(tmp_path, lock_file, "mcp23017")


@pytest.fixture
def dry_config(tmp_path: Path, lock_file: Path) -> Path:
    return write_config(tmp_path, lock_file, "dry-run")


def invoke(config: Path, *args: str):
    return runner.invoke(app, ["--config", str(config), *args])


def test_relay_set_paused_then_running_on_the_hat(hat_config: Path, smbus: FakeSMBus) -> None:
    assert invoke(hat_config, "relay", "set", "paused").exit_code == 0
    status = invoke(hat_config, "relay", "status")
    assert "Relay: paused" in status.output
    assert "mcp23017" in status.output
    assert invoke(hat_config, "relay", "set", "running").exit_code == 0
    assert "Relay: running" in invoke(hat_config, "relay", "status").output


def test_relay_status_never_writes(hat_config: Path, smbus: FakeSMBus) -> None:
    invoke(hat_config, "relay", "status")
    assert smbus.writes == []


def test_relay_set_is_refused_while_the_service_runs(
    hat_config: Path, lock_file: Path, smbus: FakeSMBus
) -> None:
    with ServiceLock(lock_file):
        result = invoke(hat_config, "relay", "set", "paused")
    assert result.exit_code == 1
    assert "service is running" in result.output
    assert smbus.writes == []


def test_relay_set_reports_a_pin_that_does_not_follow(hat_config: Path, smbus: FakeSMBus) -> None:
    smbus.stuck = True
    result = invoke(hat_config, "relay", "set", "paused")
    assert result.exit_code == 1
    assert "reads" in result.output


def test_relay_set_rejects_other_words(hat_config: Path, smbus: FakeSMBus) -> None:
    assert invoke(hat_config, "relay", "set", "on").exit_code == 2


def test_the_dry_run_driver_needs_no_hardware(dry_config: Path) -> None:
    assert invoke(dry_config, "relay", "set", "paused").exit_code == 0
    result = invoke(dry_config, "relay", "status")
    assert result.exit_code == 0
    assert "dry-run" in result.output


def test_hat_scan_lists_roles(hat_config: Path, smbus: FakeSMBus) -> None:
    smbus.devices.add(0x48)
    result = invoke(hat_config, "hat", "scan")
    assert result.exit_code == 0, result.output
    assert "MCP23017" in result.output
    assert "MCP9600" in result.output
    assert "0x48" in result.output
    assert "other" in result.output


def test_hat_scan_fails_when_an_expected_device_is_missing(
    hat_config: Path, smbus: FakeSMBus
) -> None:
    smbus.devices.discard(0x66)
    result = invoke(hat_config, "hat", "scan")
    assert result.exit_code == 1
    assert "NOT FOUND" in result.output


def test_heating_shows_the_latest_reading(hat_config: Path, tmp_path: Path) -> None:
    reading = TemperatureReading(datetime.now(UTC) - timedelta(seconds=30), 31.25, 24.5)
    with closing(db.connect(tmp_path / "data" / "heatpump.sqlite3")) as conn:
        db.create_schema(conn)
        db.insert_heating_reading(conn, reading)
    result = invoke(hat_config, "heating")
    assert result.exit_code == 0, result.output
    assert "31.25" in result.output
    assert "24.5" in result.output


def test_heating_without_a_reading_fails(hat_config: Path, tmp_path: Path) -> None:
    with closing(db.connect(tmp_path / "data" / "heatpump.sqlite3")) as conn:
        db.create_schema(conn)
    result = invoke(hat_config, "heating")
    assert result.exit_code == 1
    assert "--watch" in result.output


def test_heating_watch_needs_the_sensor_enabled(hat_config: Path) -> None:
    result = invoke(hat_config, "heating", "--watch")
    assert result.exit_code == 1
    assert "heating.enabled" in result.output
