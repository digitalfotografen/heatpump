import csv
import io
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import cli, db
from heatpump.cli import app
from heatpump.models import ControlDecision, ControlMode, PauseKind, Quarter, StoveState
from heatpump.relay import ServiceAlreadyRunning

runner = CliRunner()
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
MINUTE = timedelta(minutes=1)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"database:\n  path: {tmp_path / 'heatpump.sqlite3'}\n"
        f"control:\n  lock_file: {tmp_path / 'heatpump.lock'}\n"
    )
    return path


@pytest.fixture
def conn(tmp_path: Path):
    with closing(db.connect(tmp_path / "heatpump.sqlite3")) as connection:
        db.create_schema(connection)
        yield connection


def invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["--config", str(config_path), *args])


def log_tick(conn, minutes_ago: int, mode: ControlMode = ControlMode.RUNNING, **changes) -> None:
    paused = mode in {ControlMode.PAUSED_PRICE, ControlMode.PAUSED_STOVE}
    fields = dict(
        ts=NOW - minutes_ago * MINUTE,
        mode=mode,
        relay_requested=paused,
        relay_actual=paused,
        stove_state=StoveState.COLD,
        stove_temp=21.5,
        price_now=1.25,
    )
    db.insert_control_log(conn, ControlDecision(**(fields | changes)))


# --- run ---------------------------------------------------------------------


def test_run_starts_the_service_with_the_loaded_settings(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = []

    async def fake_run_service(settings) -> None:
        started.append(settings)

    monkeypatch.setattr(cli, "run_service", fake_run_service)
    result = invoke(config_path, "run")
    assert result.exit_code == 0, result.output
    assert started and started[0].control.tick_seconds == 60


def test_run_refuses_a_second_instance(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(settings) -> None:
        raise ServiceAlreadyRunning("locked")

    monkeypatch.setattr(cli, "run_service", refused)
    result = invoke(config_path, "run")
    assert result.exit_code == 1
    assert "already running" in result.output


def test_run_reports_a_missing_secret(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIBBER_TOKEN", raising=False)
    result = invoke(config_path, "run")
    assert result.exit_code == 1
    assert "TIBBER_TOKEN" in result.output


# --- status ------------------------------------------------------------------


def test_status_fails_before_the_first_tick(config_path: Path, conn) -> None:
    result = invoke(config_path, "status")
    assert result.exit_code == 1
    assert "no ticks" in result.output.lower()


def test_status_shows_mode_relay_and_pause_minutes(config_path: Path, conn) -> None:
    log_tick(conn, 3)
    log_tick(conn, 2, ControlMode.PAUSED_PRICE)
    log_tick(conn, 1, ControlMode.PAUSED_PRICE)
    db.open_pause_event(conn, PauseKind.PRICE, NOW - 2 * MINUTE, NOW + 60 * MINUTE)
    result = invoke(config_path, "status")
    assert result.exit_code == 0, result.output
    assert "paused_price" in result.output
    assert "1.25" in result.output  # price now


def test_status_json_is_machine_readable(config_path: Path, conn) -> None:
    log_tick(conn, 1, ControlMode.PAUSED_STOVE, guard_reason=None)
    result = invoke(config_path, "status", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["mode"] == "paused_stove" and data["relay_actual"] is True
    assert data["service_running"] is False
    assert set(data["paused_minutes_24h"]) == {"price", "stove", "total"}


# --- pauses and log ----------------------------------------------------------


def test_pauses_lists_events_with_duration_and_reason(config_path: Path, conn) -> None:
    event = db.open_pause_event(conn, PauseKind.STOVE, NOW - 90 * MINUTE, NOW + 60 * MINUTE)
    db.close_pause_event(conn, event, NOW - 30 * MINUTE, "stove_cold")
    db.open_pause_event(conn, PauseKind.PRICE, NOW - 5 * MINUTE, NOW + 60 * MINUTE)
    result = invoke(config_path, "pauses")
    assert result.exit_code == 0, result.output
    assert "stove" in result.output and "stove_cold" in result.output
    assert "60" in result.output  # minutes
    assert "price" in result.output and "open" in result.output


def test_pauses_since_leaves_out_older_events(config_path: Path, conn) -> None:
    old = db.open_pause_event(conn, PauseKind.PRICE, NOW - timedelta(days=3), NOW)
    db.close_pause_event(conn, old, NOW - timedelta(days=3) + 60 * MINUTE, "plan_ended")
    result = invoke(config_path, "pauses", "--since", (NOW - timedelta(days=1)).isoformat())
    assert result.exit_code == 0
    assert "plan_ended" not in result.output


def test_log_prints_one_line_per_tick_and_tail_keeps_the_last(config_path: Path, conn) -> None:
    for minutes_ago in (3, 2, 1):
        log_tick(
            conn, minutes_ago, ControlMode.PAUSED_PRICE if minutes_ago == 1 else ControlMode.RUNNING
        )
    result = invoke(config_path, "log", "--tail", "2")
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "paused_price" in lines[-1]


def test_log_without_rows_fails(config_path: Path, conn) -> None:
    assert invoke(config_path, "log").exit_code == 1


# --- export ------------------------------------------------------------------


def test_export_csv_to_stdout_with_a_header(config_path: Path, conn) -> None:
    db.upsert_prices(conn, [Quarter(NOW + MINUTE * 15 * i, 1.0 + i) for i in range(4)], NOW)
    result = invoke(config_path, "export", "prices")
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.output)))
    assert [row["energy"] for row in rows] == ["1.0", "2.0", "3.0", "4.0"]


def test_export_range_is_from_inclusive_to_exclusive(config_path: Path, conn) -> None:
    quarters = [
        Quarter(datetime(2026, 10, 21, 12, 0, tzinfo=UTC) + MINUTE * 15 * i, i) for i in range(8)
    ]
    db.upsert_prices(conn, quarters, NOW)
    result = invoke(
        config_path,
        "export",
        "prices",
        "--from",
        "2026-10-21T13:00:00+00:00",
        "--to",
        "2026-10-21T13:30:00+00:00",
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    assert [row["energy"] for row in json.loads(result.output)] == [4.0, 5.0]


def test_export_to_a_file(config_path: Path, conn, tmp_path: Path) -> None:
    log_tick(conn, 1)
    out = tmp_path / "log.csv"
    result = invoke(config_path, "export", "control_log", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "running" in out.read_text()


def test_export_rejects_an_unknown_table_and_format(config_path: Path, conn) -> None:
    assert invoke(config_path, "export", "nonsense").exit_code == 1
    assert invoke(config_path, "export", "prices", "--format", "xml").exit_code == 1


@pytest.mark.parametrize("table", list(db.TABLES))
def test_every_table_can_be_exported(config_path: Path, conn, table: str) -> None:
    assert invoke(config_path, "export", table).exit_code == 0
