import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import db, prices
from heatpump.cli import app
from heatpump.models import Quarter

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


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


@pytest.fixture
def tibber_returns_two_days(monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.loads((FIXTURES / "tibber_two_days.json").read_text())

    async def fake_fetch(token: str, home_index: int) -> list[Quarter]:
        return prices.parse_price_info(data, home_index)

    monkeypatch.setenv("TIBBER_TOKEN", "secret")
    monkeypatch.setattr(prices, "fetch_prices", fake_fetch)


def test_prices_fetch_stores_quarters(
    config_path: Path, db_path: Path, tibber_returns_two_days: None
) -> None:
    result = invoke(config_path, "prices", "fetch")
    assert result.exit_code == 0, result.output
    assert "192" in result.output
    conn = db.connect(db_path, read_only=True)
    assert db.latest_price_slot(conn) == datetime(2026, 10, 21, 21, 45, tzinfo=UTC)


def test_prices_fetch_show_prints_the_table(
    config_path: Path, tibber_returns_two_days: None
) -> None:
    result = invoke(config_path, "prices", "fetch", "--show")
    assert result.exit_code == 0, result.output
    assert "2026-10-20" in result.output
    assert "2026-10-21" in result.output


def test_prices_fetch_without_token_fails(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIBBER_TOKEN", raising=False)
    result = invoke(config_path, "prices", "fetch")
    assert result.exit_code == 1
    assert "TIBBER_TOKEN" in result.output


def test_prices_fetch_reports_tibber_failure(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_fetch(token: str, home_index: int) -> list[Quarter]:
        raise prices.PriceDataError("no usable data")

    monkeypatch.setenv("TIBBER_TOKEN", "secret")
    monkeypatch.setattr(prices, "fetch_prices", failing_fetch)
    result = invoke(config_path, "prices", "fetch")
    assert result.exit_code == 1
    assert "no usable data" in result.output


def store_day(db_path: Path, first_start: datetime, count: int = 96) -> None:
    conn = db.connect(db_path)
    db.create_schema(conn)
    quarters = [
        Quarter(
            start=first_start + timedelta(minutes=15 * i),
            energy=0.5 + i / 100,
            tax=0.5,
            total=1.0 + i / 100,
        )
        for i in range(count)
    ]
    db.upsert_prices(conn, quarters, fetched_at=first_start)


def test_prices_show_lists_the_day_in_local_time(config_path: Path, db_path: Path) -> None:
    store_day(db_path, datetime(2026, 10, 19, 22, 0, tzinfo=UTC))  # 2026-10-20 00:00 CEST
    result = invoke(config_path, "prices", "show", "--date", "2026-10-20")
    assert result.exit_code == 0, result.output
    assert "00:00" in result.output
    assert "23:45" in result.output
    assert "0.50" in result.output


def test_prices_show_on_a_dst_day_has_100_rows(config_path: Path, db_path: Path) -> None:
    store_day(db_path, datetime(2026, 10, 24, 22, 0, tzinfo=UTC), count=100)  # 25 Oct, 25 h
    result = invoke(config_path, "prices", "show", "--date", "2026-10-25")
    assert result.exit_code == 0, result.output
    assert len(re.findall(r"2026-10-25 \d\d:\d\d", result.output)) == 100
    assert "missing" not in result.output.lower()


def test_prices_show_reports_gaps(config_path: Path, db_path: Path) -> None:
    store_day(db_path, datetime(2026, 10, 19, 22, 0, tzinfo=UTC), count=40)
    result = invoke(config_path, "prices", "show", "--date", "2026-10-20")
    assert result.exit_code == 0, result.output
    assert "missing" in result.output.lower()


def test_prices_show_without_prices_fails(config_path: Path, db_path: Path) -> None:
    store_day(db_path, datetime(2026, 10, 19, 22, 0, tzinfo=UTC))
    result = invoke(config_path, "prices", "show", "--date", "2026-11-30")
    assert result.exit_code == 1
    assert "prices fetch" in result.output


def test_prices_show_without_database_fails(config_path: Path) -> None:
    result = invoke(config_path, "prices", "show")
    assert result.exit_code == 1
    assert "db init" in result.output


def test_prices_show_rejects_a_bad_date(config_path: Path, db_path: Path) -> None:
    store_day(db_path, datetime(2026, 10, 19, 22, 0, tzinfo=UTC))
    result = invoke(config_path, "prices", "show", "--date", "yesterday")
    assert result.exit_code == 2
