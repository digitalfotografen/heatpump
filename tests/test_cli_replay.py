from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import db
from heatpump.cli import app
from heatpump.models import Quarter

runner = CliRunner()
SLOT = timedelta(minutes=15)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"database:\n  path: {tmp_path / 'heatpump.sqlite3'}\nlocation:\n  timezone: UTC\n"
    )
    return path


def store_day(config_path: Path, prices: list[float]) -> None:
    start = datetime(2026, 10, 10, tzinfo=UTC)
    quarters = [Quarter(start=start + SLOT * i, energy=p) for i, p in enumerate(prices)]
    db_path = next(config_path.parent.glob("*.sqlite3"), config_path.parent / "heatpump.sqlite3")
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
        db.upsert_prices(conn, quarters, datetime.now(UTC))


def test_replay_prints_the_pauses_and_the_saving(config_path: Path) -> None:
    prices = [1.0] * 96
    prices[40:48] = [4.0] * 8
    store_day(config_path, prices)
    result = runner.invoke(app, ["--config", str(config_path), "replay", "--date", "2026-10-10"])
    assert result.exit_code == 0, result.output
    assert "2026-10-10" in result.output
    assert "Expected saving" in result.output


def test_replay_of_a_day_without_prices_fails(config_path: Path) -> None:
    store_day(config_path, [1.0] * 96)
    result = runner.invoke(app, ["--config", str(config_path), "replay", "--date", "2026-01-01"])
    assert result.exit_code == 1
    assert "No prices stored" in result.output


def test_replay_never_touches_the_relay_or_the_database(config_path: Path) -> None:
    store_day(config_path, [1.0] * 96)
    db_file = config_path.parent / "heatpump.sqlite3"
    before = db_file.stat().st_mtime_ns
    runner.invoke(app, ["--config", str(config_path), "replay", "--date", "2026-10-10"])
    assert db_file.stat().st_mtime_ns == before
