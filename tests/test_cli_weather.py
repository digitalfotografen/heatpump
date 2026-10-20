from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import cli, db
from heatpump.cli import app
from heatpump.models import TemperatureReading
from heatpump.weather import WeatherError

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"database:\n  path: {tmp_path / 'data' / 'heatpump.sqlite3'}\n")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "heatpump.sqlite3"


def invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["--config", str(config_path), *args])


def store_reading(db_path: Path, age: timedelta, temperature: float) -> None:
    reading = TemperatureReading(datetime.now(UTC) - age, temperature, "open-meteo")
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
        db.insert_outdoor_reading(conn, reading)


def test_weather_shows_the_latest_reading_and_its_age(config_path: Path, db_path: Path) -> None:
    store_reading(db_path, timedelta(minutes=10), -3.5)
    result = invoke(config_path, "weather")
    assert result.exit_code == 0, result.output
    assert "-3.5" in result.output
    assert "10 min ago" in result.output
    assert "stale" not in result.output


def test_an_old_reading_is_marked_stale(config_path: Path, db_path: Path) -> None:
    store_reading(db_path, timedelta(hours=4), 5.0)
    result = invoke(config_path, "weather")
    assert result.exit_code == 0, result.output
    assert "stale" in result.output


def test_weather_without_a_reading_fails(config_path: Path, db_path: Path) -> None:
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
    result = invoke(config_path, "weather")
    assert result.exit_code == 1
    assert "--fetch" in result.output


def test_weather_fetch_stores_and_prints(
    config_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(conn, provider) -> TemperatureReading:
        reading = TemperatureReading(datetime.now(UTC), 7.7, "open-meteo")
        db.insert_outdoor_reading(conn, reading)
        return reading

    monkeypatch.setattr(cli, "fetch_and_store", fake_fetch)
    result = invoke(config_path, "weather", "--fetch")
    assert result.exit_code == 0, result.output
    assert "7.7" in result.output
    assert "open-meteo" in result.output
    with closing(db.connect(db_path, read_only=True)) as conn:
        assert db.latest_outdoor_reading(conn).temperature == 7.7


def test_weather_fetch_reports_a_failure(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_fetch(conn, provider) -> TemperatureReading:
        raise WeatherError("Open-Meteo request failed")

    monkeypatch.setattr(cli, "fetch_and_store", failing_fetch)
    result = invoke(config_path, "weather", "--fetch")
    assert result.exit_code == 1
    assert "Open-Meteo request failed" in result.output
