import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heatpump import db
from heatpump.models import (
    ControlDecision,
    ControlMode,
    Pause,
    PauseEvent,
    PauseKind,
    Plan,
    Quarter,
    StoveReading,
    StoveState,
    TemperatureReading,
)

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.create_schema(connection)
    return connection


def quarters(count: int, start: int = 0) -> list[Quarter]:
    return [
        Quarter(at(start + 15 * i), energy=1.0 + i, tax=0.5, total=1.5 + i) for i in range(count)
    ]


def test_iso_round_trip_and_utc_normalisation() -> None:
    local = datetime.fromisoformat("2026-10-20T14:00:00+02:00")
    assert db.to_iso(local) == "2026-10-20T12:00:00+00:00"
    assert db.from_iso(db.to_iso(local)) == local


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError):
        db.to_iso(datetime(2026, 10, 20, 12, 0))


def test_create_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    db.create_schema(conn)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(db.TABLES) <= names


def test_file_database_uses_wal_and_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "heatpump.sqlite3"
    connection = db.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_read_only_connection_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "heatpump.sqlite3"
    writer = db.connect(path)
    db.create_schema(writer)
    reader = db.connect(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        db.upsert_prices(reader, quarters(1), fetched_at=at(0))
    reader.close()
    writer.close()


def test_read_only_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        db.connect(tmp_path / "missing.sqlite3", read_only=True)


def test_prices_round_trip_in_order(conn: sqlite3.Connection) -> None:
    stored = quarters(4)
    db.upsert_prices(conn, list(reversed(stored)), fetched_at=at(-60))
    result = db.prices_for_range(conn, at(0), at(60))
    assert result.quarters == stored
    assert result.gaps == []


def test_price_refetch_upserts(conn: sqlite3.Connection) -> None:
    db.upsert_prices(conn, quarters(1), fetched_at=at(-60))
    changed = Quarter(at(0), energy=9.0, tax=0.5, total=9.5)
    db.upsert_prices(conn, [changed], fetched_at=at(-30))
    assert db.prices_for_range(conn, at(0), at(15)).quarters == [changed]


def test_prices_for_range_reports_gaps(conn: sqlite3.Connection) -> None:
    all_quarters = quarters(8)
    db.upsert_prices(conn, all_quarters[:2] + all_quarters[5:], fetched_at=at(-60))
    result = db.prices_for_range(conn, at(0), at(120))
    assert [q.start for q in result.quarters] == [at(0), at(15), at(75), at(90), at(105)]
    assert result.gaps == [(at(30), at(75))]


def test_prices_for_range_with_nothing_stored_is_one_gap(conn: sqlite3.Connection) -> None:
    result = db.prices_for_range(conn, at(0), at(60))
    assert result.quarters == []
    assert result.gaps == [(at(0), at(60))]


def test_prices_for_range_reports_trailing_gap(conn: sqlite3.Connection) -> None:
    db.upsert_prices(conn, quarters(2), fetched_at=at(-60))
    assert db.prices_for_range(conn, at(0), at(60)).gaps == [(at(30), at(60))]


def test_latest_slot_start(conn: sqlite3.Connection) -> None:
    assert db.latest_price_slot(conn) is None
    db.upsert_prices(conn, quarters(3), fetched_at=at(-60))
    assert db.latest_price_slot(conn) == at(30)


def test_stove_reading_round_trip(conn: sqlite3.Connection) -> None:
    assert db.latest_stove_reading(conn) is None
    reading = StoveReading(at(0), 63.5, StoveState.BURNING)
    db.insert_stove_reading(conn, reading)
    db.insert_stove_reading(conn, StoveReading(at(-1), 20.0, StoveState.COLD))
    assert db.latest_stove_reading(conn) == reading


def test_outdoor_reading_round_trip(conn: sqlite3.Connection) -> None:
    reading = TemperatureReading(at(0), 4.2, "open-meteo")
    db.insert_outdoor_reading(conn, reading)
    assert db.latest_outdoor_reading(conn) == reading


def test_heating_reading_round_trip(conn: sqlite3.Connection) -> None:
    reading = TemperatureReading(at(0), 38.25, 24.5)
    db.insert_heating_reading(conn, reading)
    assert db.latest_heating_reading(conn) == reading


def test_plan_round_trip_including_empty_plan(conn: sqlite3.Connection) -> None:
    pause = Pause(PauseKind.PRICE, at(60), at(120))
    plan = Plan(at(0), "peak", at(0), at(1440), 3.5, (pause,))
    empty = Plan(at(10), "flat-day", at(0), at(1440))
    plan_id = db.insert_plan(conn, plan)
    empty_id = db.insert_plan(conn, empty)
    assert empty_id > plan_id
    assert db.latest_plan(conn) == Plan(at(10), "flat-day", at(0), at(1440), 0.0, (), empty_id)
    assert db.get_plan(conn, plan_id) == Plan(
        at(0), "peak", at(0), at(1440), 3.5, (pause,), plan_id
    )


def test_latest_plan_when_none(conn: sqlite3.Connection) -> None:
    assert db.latest_plan(conn) is None
    assert db.get_plan(conn, 1) is None


def test_pause_event_lifecycle(conn: sqlite3.Connection) -> None:
    event_id = db.open_pause_event(conn, PauseKind.STOVE, at(0), planned_end=at(360))
    [open_event] = db.pause_events_since(conn, at(-60))
    assert open_event == PauseEvent(PauseKind.STOVE, at(0), at(360), None, None, event_id)
    assert db.open_pause(conn) == open_event
    db.close_pause_event(conn, event_id, ended_at=at(45), end_reason="stove-cold")
    [closed] = db.pause_events_since(conn, at(-60))
    assert closed.ended_at == at(45)
    assert closed.end_reason == "stove-cold"
    assert db.open_pause(conn) is None


def test_pause_events_since_keeps_pauses_overlapping_the_window(conn: sqlite3.Connection) -> None:
    old = db.open_pause_event(conn, PauseKind.PRICE, at(-3000), at(-2900))
    db.close_pause_event(conn, old, at(-2900), "planned")
    spanning = db.open_pause_event(conn, PauseKind.PRICE, at(-1500), at(-1400))
    db.close_pause_event(conn, spanning, at(-1400), "planned")
    db.open_pause_event(conn, PauseKind.STOVE, at(-10), at(300))
    kinds = [e.kind for e in db.pause_events_since(conn, at(-1440))]
    assert kinds == [PauseKind.PRICE, PauseKind.STOVE]


def test_control_log_round_trip(conn: sqlite3.Connection) -> None:
    decision = ControlDecision(
        ts=at(0),
        mode=ControlMode.PAUSED_STOVE,
        relay_requested=True,
        relay_actual=True,
        guard_reason=None,
        stove_state=StoveState.BURNING,
        stove_temp=71.0,
        outdoor_temp=-3.5,
        heating_temp=35.0,
        price_now=1.23,
        plan_id=None,
    )
    db.insert_control_log(conn, decision)
    assert db.control_log_since(conn, at(-1)) == [decision]


def test_control_log_with_missing_inputs(conn: sqlite3.Connection) -> None:
    decision = ControlDecision(at(0), ControlMode.FORCED_RUN, False, None, "no-price")
    db.insert_control_log(conn, decision)
    assert db.control_log_since(conn, at(0)) == [decision]


def test_table_summary_counts_and_range(conn: sqlite3.Connection) -> None:
    db.upsert_prices(conn, quarters(3), fetched_at=at(-60))
    summary = {s.table: s for s in db.table_summaries(conn)}
    assert list(summary) == list(db.TABLES)
    assert summary["prices"].rows == 3
    assert summary["prices"].first == at(0)
    assert summary["prices"].last == at(30)
    assert summary["plans"].rows == 0
    assert summary["plans"].first is None
