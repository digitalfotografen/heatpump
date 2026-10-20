from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import cli, db
from heatpump.cli import app
from heatpump.config import MqttSettings
from heatpump.models import StoveReading, StoveState
from heatpump.stove import StoveListener

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"database:\n  path: {tmp_path / 'data' / 'heatpump.sqlite3'}\n"
        "mqtt:\n  host: broker\n  username_env: null\n  password_env: null\n"
    )
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "heatpump.sqlite3"


def invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["--config", str(config_path), *args])


def store_reading(db_path: Path, age: timedelta, temperature: float, state: StoveState) -> None:
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
        db.insert_stove_reading(conn, StoveReading(datetime.now(UTC) - age, temperature, state))


def test_stove_shows_the_latest_stored_reading(config_path: Path, db_path: Path) -> None:
    store_reading(db_path, timedelta(seconds=30), 61.5, StoveState.BURNING)
    result = invoke(config_path, "stove")
    assert result.exit_code == 0, result.output
    assert "61.5" in result.output
    assert "burning" in result.output


def test_an_old_reading_is_shown_as_stale(config_path: Path, db_path: Path) -> None:
    store_reading(db_path, timedelta(hours=1), 61.5, StoveState.BURNING)
    result = invoke(config_path, "stove")
    assert result.exit_code == 0, result.output
    assert "stale" in result.output
    assert "burning" not in result.output


def test_stove_without_readings_fails(config_path: Path, db_path: Path) -> None:
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
    result = invoke(config_path, "stove")
    assert result.exit_code == 1
    assert "--watch" in result.output


def test_stove_watch_prints_each_reading(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(listener: StoveListener, mqtt: MqttSettings, credentials: object) -> None:
        assert mqtt.host == "broker" and credentials is None
        ts = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        listener._on_reading(StoveReading(ts, 55.0, StoveState.BURNING))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_listener", fake_run)
    result = invoke(config_path, "stove", "--watch")
    assert result.exit_code == 0, result.output
    assert "2026-10-21 14:00:00" in result.output  # local time
    assert "55.0" in result.output
    assert "burning" in result.output


def test_stove_watch_does_not_write_the_database(
    config_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_listener", fake_run)
    invoke(config_path, "stove", "--watch")
    assert not db_path.exists()


def test_stove_watch_needs_the_mqtt_credentials_named_in_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("mqtt:\n  username_env: NO_SUCH_USER\n  password_env: NO_SUCH_PASSWORD\n")
    monkeypatch.delenv("NO_SUCH_USER", raising=False)
    result = invoke(config, "stove", "--watch")
    assert result.exit_code == 1
    assert "NO_SUCH_USER" in result.output
