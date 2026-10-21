from contextlib import closing
from datetime import UTC, datetime, timedelta

from heatpump import db, status
from heatpump.config import Settings
from heatpump.models import (
    ControlDecision,
    ControlMode,
    Pause,
    PauseKind,
    Plan,
    Quarter,
    StoveState,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
PAUSED = {ControlMode.PAUSED_PRICE, ControlMode.PAUSED_STOVE}


def tick(minutes_ago: int, mode: ControlMode) -> ControlDecision:
    return ControlDecision(
        ts=NOW - minutes_ago * MINUTE,
        mode=mode,
        relay_requested=mode in PAUSED,
        relay_actual=mode in PAUSED,
        stove_state=StoveState.COLD,
    )


def database():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    return conn


def test_no_status_before_the_first_tick() -> None:
    with closing(database()) as conn:
        assert status.collect(conn, Settings(), NOW, False) is None


def test_time_in_mode_counts_back_over_the_unbroken_run() -> None:
    log = [
        tick(5, ControlMode.PAUSED_PRICE),
        tick(4, ControlMode.RUNNING),
        tick(3, ControlMode.RUNNING),
        tick(2, ControlMode.PAUSED_PRICE),
        tick(1, ControlMode.PAUSED_PRICE),
    ]
    assert status.mode_since(log) == NOW - 2 * MINUTE


def test_status_collects_the_last_tick_pause_minutes_and_next_pause() -> None:
    with closing(database()) as conn:
        for decision in [tick(2, ControlMode.RUNNING), tick(1, ControlMode.PAUSED_STOVE)]:
            db.insert_control_log(conn, decision)
        price = db.open_pause_event(conn, PauseKind.PRICE, NOW - timedelta(hours=3), NOW)
        db.close_pause_event(conn, price, NOW - timedelta(hours=2), "plan_ended")
        db.open_pause_event(conn, PauseKind.STOVE, NOW - 30 * MINUTE, NOW + timedelta(hours=6))
        upcoming = Pause(PauseKind.PRICE, NOW + timedelta(hours=2), NOW + timedelta(hours=4))
        db.insert_plan(conn, Plan(NOW, "planned", NOW, NOW + timedelta(days=1), 2.0, (upcoming,)))
        db.upsert_prices(conn, [Quarter(NOW, 1.0)], fetched_at=NOW)

        result = status.collect(conn, Settings(), NOW, service_is_running=True)

    assert result is not None
    assert result.last.mode is ControlMode.PAUSED_STOVE
    assert result.mode_since == NOW - MINUTE
    assert (result.paused_24h.price, result.paused_24h.stove, result.paused_24h.total) == (
        60,
        30,
        90,
    )
    assert result.next_pause == upcoming
    assert result.service_running
    as_dict = result.as_dict()
    assert as_dict["paused_minutes_24h"] == {"price": 60.0, "stove": 30.0, "total": 90.0}
    assert as_dict["next_pause"]["start"] == "2026-10-21T14:00:00+00:00"
