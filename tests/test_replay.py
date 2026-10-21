from datetime import UTC, datetime, timedelta

from heatpump import replay
from heatpump.config import PricePauseSettings
from heatpump.models import PauseKind, Quarter

SLOT = timedelta(minutes=15)
DAY_START = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)


def quarters(prices: list[float], start: datetime = DAY_START) -> list[Quarter]:
    return [Quarter(start=start + SLOT * i, energy=price) for i, price in enumerate(prices)]


def peaked_day() -> list[float]:
    prices = [1.0] * 96
    prices[40:48] = [4.0] * 8
    return prices


def test_replay_pauses_the_expensive_hours_of_a_peaked_day() -> None:
    result = replay.replay_day(
        quarters(peaked_day()), DAY_START, DAY_START + timedelta(days=1), PricePauseSettings()
    )
    assert result.pauses, "a clear price peak must be paused"
    assert all(pause.kind is PauseKind.PRICE for pause in result.pauses)
    peak_start = DAY_START + SLOT * 40
    assert any(pause.start >= peak_start - timedelta(hours=1) for pause in result.pauses)
    assert result.saving_sek > 0


def test_replay_of_a_flat_day_pauses_nothing() -> None:
    result = replay.replay_day(
        quarters([1.0] * 96), DAY_START, DAY_START + timedelta(days=1), PricePauseSettings()
    )
    assert result.pauses == []
    assert result.saving_sek == 0


def test_replay_pauses_stay_inside_the_day_and_the_budget() -> None:
    settings = PricePauseSettings()
    result = replay.replay_day(
        quarters(peaked_day()), DAY_START, DAY_START + timedelta(days=1), settings
    )
    assert all(
        DAY_START <= p.start and p.end <= DAY_START + timedelta(days=2) for p in result.pauses
    )
    assert sum(p.minutes for p in result.pauses) <= settings.budget_minutes_per_24h
    assert result.pauses == sorted(result.pauses, key=lambda p: p.start)


def test_replay_replans_with_the_pauses_already_taken_as_history() -> None:
    """A short re-plan interval must not take more budget than one 24 h window allows."""
    settings = PricePauseSettings(replan_interval_hours=1)
    result = replay.replay_day(
        quarters([1.0, 4.0] * 48), DAY_START, DAY_START + timedelta(days=1), settings
    )
    assert sum(p.minutes for p in result.pauses) <= settings.budget_minutes_per_24h


def test_replay_without_prices_gives_an_empty_result() -> None:
    result = replay.replay_day([], DAY_START, DAY_START + timedelta(days=1), PricePauseSettings())
    assert result.pauses == []
    assert result.saving_sek == 0
