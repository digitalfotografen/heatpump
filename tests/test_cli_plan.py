from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import db, planner
from heatpump.cli import app
from heatpump.models import Pause, PauseKind, Plan, Quarter

runner = CliRunner()
SLOT = timedelta(minutes=15)


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


def store_prices(db_path: Path, prices: list[float]) -> None:
    """Store prices from the current quarter on."""
    first = planner.floor_to_quarter(datetime.now(UTC))
    quarters = [Quarter(start=first + SLOT * i, energy=price) for i, price in enumerate(prices)]
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
        db.upsert_prices(conn, quarters, datetime.now(UTC))


def peaked_day() -> list[float]:
    prices = [1.0] * 96
    prices[40:44] = [4.0] * 4
    return prices


def test_plan_now_shows_the_pauses_and_the_saving(config_path: Path, db_path: Path) -> None:
    store_prices(db_path, peaked_day())
    result = invoke(config_path, "plan", "now")
    assert result.exit_code == 0, result.output
    assert "planned" in result.output
    assert "3.00 SEK" in result.output  # 4 quarters * (4.0 - 1.0) * 0.25


def test_plan_now_does_not_store_the_plan(config_path: Path, db_path: Path) -> None:
    store_prices(db_path, peaked_day())
    invoke(config_path, "plan", "now")
    with closing(db.connect(db_path, read_only=True)) as conn:
        assert db.latest_plan(conn) is None


def test_plan_now_on_a_flat_day_says_so(config_path: Path, db_path: Path) -> None:
    store_prices(db_path, [1.0] * 96)
    result = invoke(config_path, "plan", "now")
    assert result.exit_code == 0, result.output
    assert "flat-day" in result.output
    assert "No price pauses" in result.output


def test_plan_now_without_prices_fails(config_path: Path, db_path: Path) -> None:
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
    result = invoke(config_path, "plan", "now")
    assert result.exit_code == 1
    assert "prices fetch" in result.output


def test_plan_now_without_a_database_fails(config_path: Path) -> None:
    result = invoke(config_path, "plan", "now")
    assert result.exit_code == 1


def test_plan_now_takes_the_pause_history_into_account(config_path: Path, db_path: Path) -> None:
    store_prices(db_path, peaked_day())
    now = datetime.now(UTC)
    with closing(db.connect(db_path)) as conn:
        for hours_ago in (10, 5):  # 6 h of price pauses in the last 24 h: the budget is spent
            start = now - timedelta(hours=hours_ago)
            end = start + timedelta(hours=3)
            event_id = db.open_pause_event(conn, PauseKind.PRICE, start, end)
            db.close_pause_event(conn, event_id, end, "planned")
    result = invoke(config_path, "plan", "now")
    assert result.exit_code == 0, result.output
    assert "No price pauses" in result.output


def test_plan_show_prints_the_stored_plan(config_path: Path, db_path: Path) -> None:
    start = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    plan = Plan(
        created_at=start,
        reason="planned",
        horizon_start=start,
        horizon_end=start + timedelta(hours=24),
        expected_saving_sek=3.5,
        pauses=(Pause(PauseKind.PRICE, start + timedelta(hours=2), start + timedelta(hours=4)),),
    )
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
        db.insert_plan(conn, plan)
    result = invoke(config_path, "plan", "show")
    assert result.exit_code == 0, result.output
    assert "planned" in result.output
    assert "3.50 SEK" in result.output
    assert "2026-10-21" in result.output
    assert "14:00" in result.output  # 12:00 UTC is 14:00 in Stockholm


def test_plan_show_without_a_stored_plan_fails(config_path: Path, db_path: Path) -> None:
    with closing(db.connect(db_path)) as conn:
        db.create_schema(conn)
    result = invoke(config_path, "plan", "show")
    assert result.exit_code == 1
    assert "plan now" in result.output
