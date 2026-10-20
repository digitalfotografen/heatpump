from datetime import UTC, datetime, timedelta

from heatpump import budget
from heatpump.models import PauseEvent, PauseKind

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def event(
    start_minutes_ago: float,
    length_minutes: float,
    kind: PauseKind = PauseKind.PRICE,
    open_: bool = False,
) -> PauseEvent:
    started = NOW - timedelta(minutes=start_minutes_ago)
    planned_end = started + timedelta(minutes=length_minutes)
    return PauseEvent(
        kind=kind,
        started_at=started,
        planned_end=planned_end,
        ended_at=None if open_ else planned_end,
    )


def test_paused_minutes_sums_events_inside_the_window() -> None:
    events = [event(300, 60), event(100, 45)]
    assert budget.paused_minutes(events, NOW - timedelta(hours=24), NOW, NOW) == 105


def test_paused_minutes_clips_events_that_started_before_the_window() -> None:
    events = [event(24 * 60 + 30, 60)]  # ran from 24 h 30 min ago to 23 h 30 min ago
    assert budget.paused_minutes(events, NOW - timedelta(hours=24), NOW, NOW) == 30


def test_paused_minutes_counts_an_open_pause_up_to_now() -> None:
    events = [event(20, 90, open_=True)]
    assert budget.paused_minutes(events, NOW - timedelta(hours=24), NOW, NOW) == 20


def test_paused_minutes_filters_by_kind() -> None:
    events = [event(300, 60), event(100, 45, kind=PauseKind.STOVE)]
    window_start = NOW - timedelta(hours=24)
    assert budget.paused_minutes(events, window_start, NOW, NOW, PauseKind.PRICE) == 60
    assert budget.paused_minutes(events, window_start, NOW, NOW, PauseKind.STOVE) == 45
    assert budget.paused_minutes(events, window_start, NOW, NOW) == 105


def test_price_paused_minutes_last_24h_ignores_stove_pauses() -> None:
    events = [event(300, 60), event(100, 240, kind=PauseKind.STOVE)]
    assert budget.price_paused_minutes_last_24h(events, NOW) == 60


def test_recovery_after_a_long_pause_is_half_the_pause() -> None:
    events = [event(130, 120)]  # ended 10 minutes ago
    assert budget.recovery_until(events, 0.5, 30) == NOW - timedelta(minutes=10) + timedelta(
        minutes=60
    )


def test_recovery_after_a_short_pause_is_at_least_the_minimum() -> None:
    events = [event(100, 30)]  # ended 70 minutes ago
    ended = NOW - timedelta(minutes=70)
    assert budget.recovery_until(events, 0.5, 30) == ended + timedelta(minutes=30)


def test_recovery_uses_the_most_recent_price_pause() -> None:
    events = [event(600, 180), event(100, 30), event(50, 60, kind=PauseKind.STOVE)]
    ended = NOW - timedelta(minutes=70)
    assert budget.recovery_until(events, 0.5, 30) == ended + timedelta(minutes=30)


def test_no_recovery_without_a_finished_price_pause() -> None:
    assert budget.recovery_until([], 0.5, 30) is None
    assert budget.recovery_until([event(20, 90, open_=True)], 0.5, 30) is None


def test_paused_minutes_per_slot_splits_an_event_across_quarters() -> None:
    first_slot = NOW - timedelta(minutes=45)
    events = [event(40, 25)]  # 11:20 to 11:45
    minutes = budget.paused_minutes_per_slot(events, first_slot, 4, NOW)
    assert minutes == [10, 15, 0, 0]
