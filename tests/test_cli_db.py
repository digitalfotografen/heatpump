import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import db
from heatpump.cli import app
from heatpump.models import Quarter

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"database:\n  path: {tmp_path / 'data' / 'heatpump.sqlite3'}\n")
    return path


def invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["--config", str(config_path), *args])


def test_db_init_creates_schema_and_is_idempotent(config_path: Path, tmp_path: Path) -> None:
    for _ in range(2):
        result = invoke(config_path, "db", "init")
        assert result.exit_code == 0, result.output
    connection = db.connect(tmp_path / "data" / "heatpump.sqlite3", read_only=True)
    assert [s.rows for s in db.table_summaries(connection)] == [0] * len(db.TABLES)


def test_db_info_without_database_fails(config_path: Path) -> None:
    result = invoke(config_path, "db", "info")
    assert result.exit_code == 1
    assert "db init" in result.output


def test_db_info_json_lists_row_counts(config_path: Path, tmp_path: Path) -> None:
    invoke(config_path, "db", "init")
    start = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    with db.connect(tmp_path / "data" / "heatpump.sqlite3") as connection:
        db.upsert_prices(connection, [Quarter(start, 1.0)], fetched_at=start)
    result = invoke(config_path, "db", "info", "--json")
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["tables"]["prices"] == {
        "rows": 1,
        "first": "2026-10-20T12:00:00+00:00",
        "last": "2026-10-20T12:00:00+00:00",
    }
    assert info["tables"]["plans"]["rows"] == 0


def test_db_info_table_shows_local_time(config_path: Path, tmp_path: Path) -> None:
    invoke(config_path, "db", "init")
    start = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    with db.connect(tmp_path / "data" / "heatpump.sqlite3") as connection:
        db.upsert_prices(connection, [Quarter(start, 1.0)], fetched_at=start)
    result = invoke(config_path, "db", "info")
    assert result.exit_code == 0
    assert "prices" in result.output
    assert "2026-10-20 14:00" in result.output  # Stockholm is UTC+2 on this date
