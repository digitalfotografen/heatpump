import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from heatpump import planner
from heatpump.config import PricePauseSettings
from heatpump.models import Pause, PauseEvent, PauseKind, Quarter

SLOT = timedelta(minutes=15)
DAY_START = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
SETTINGS = PricePauseSettings()


def quarters_from(start: datetime, prices: Sequence[float | None]) -> list[Quarter]:
    """Consecutive quarters from ``start``; a ``None`` price leaves that quarter out."""
    return [
        Quarter(start=start + SLOT * i, energy=price)
        for i, price in enumerate(prices)
        if price is not None
    ]


def plan_day(
    prices: Sequence[float | None],
    history: Sequence[PauseEvent] = (),
    now: datetime = DAY_START,
    settings: PricePauseSettings = SETTINGS,
) -> list[Pause]:
    start = planner.floor_to_quarter(now)
    return planner.plan_pauses(quarters_from(start, prices), list(history), now, settings)


def price_event(start: datetime, minutes: int) -> PauseEvent:
    end = start + timedelta(minutes=minutes)
    return PauseEvent(kind=PauseKind.PRICE, started_at=start, planned_end=end, ended_at=end)


def minutes_paused(pauses: Sequence[Pause]) -> float:
    return sum(p.minutes for p in pauses)


# --- the rules, checked independently of the planner's own implementation ---


def assert_pauses_follow_rules(
    pauses: Sequence[Pause],
    quarters: Sequence[Quarter],
    history: Sequence[PauseEvent],
    now: datetime,
    settings: PricePauseSettings,
) -> None:
    priced = {q.start for q in quarters}
    ordered = sorted(pauses, key=lambda p: p.start)
    for pause in ordered:
        assert pause.kind is PauseKind.PRICE
        assert settings.min_minutes <= pause.minutes <= settings.max_minutes, pause
        assert pause.start.minute % 15 == 0 and pause.start.second == 0
        slot = pause.start
        while slot < pause.end:
            assert slot in priced, f"paused a quarter without a price: {slot}"
            slot += SLOT
    for before, after in zip(ordered, ordered[1:], strict=False):
        recovery = max(settings.recovery_min_minutes, before.minutes * settings.recovery_fraction)
        assert after.start - before.end >= timedelta(minutes=recovery), (before, after)
    ended = [e for e in history if e.ended_at is not None]
    if ended and ordered:
        last = max(ended, key=lambda e: e.ended_at)
        duration = (last.ended_at - last.started_at).total_seconds() / 60
        recovery = max(settings.recovery_min_minutes, duration * settings.recovery_fraction)
        assert ordered[0].start >= last.ended_at + timedelta(minutes=recovery)
    assert_rolling_budget(ordered, quarters, history, now, settings)


def assert_rolling_budget(
    pauses: Sequence[Pause],
    quarters: Sequence[Quarter],
    history: Sequence[PauseEvent],
    now: datetime,
    settings: PricePauseSettings,
) -> None:
    """In every 24 h window that ends at a planned quarter end, the price pauses fit the budget."""
    intervals = [(e.started_at, e.ended_at or now) for e in history if e.kind is PauseKind.PRICE]
    intervals += [(p.start, p.end) for p in pauses]
    for pause in pauses:
        slot_end = pause.start + SLOT
        while slot_end <= pause.end:
            window_start = slot_end - timedelta(hours=24)
            used = sum(
                max(0.0, (min(b, slot_end) - max(a, window_start)).total_seconds() / 60)
                for a, b in intervals
            )
            assert used <= settings.budget_minutes_per_24h + 1e-6, (slot_end, used)
            slot_end += SLOT


# --- hand-made days ---------------------------------------------------------


def flat_day(price: float = 1.0) -> list[float]:
    return [price] * 96


def test_one_sharp_peak_is_paused() -> None:
    prices = flat_day(1.0)
    for i in range(48, 52):  # 12:00-13:00
        prices[i] = 3.0
    pauses = plan_day(prices)
    covering_peak = [p for p in pauses if p.start <= DAY_START + 48 * SLOT < p.end]
    assert len(covering_peak) == 1
    assert covering_peak[0].start <= DAY_START + 48 * SLOT
    assert covering_peak[0].end >= DAY_START + 52 * SLOT
    assert_pauses_follow_rules(pauses, quarters_from(DAY_START, prices), [], DAY_START, SETTINGS)


def test_two_peaks_give_two_separate_pauses() -> None:
    prices = flat_day(1.0)
    for i in (28, 29, 30, 31, 72, 73, 74, 75):  # 07:00-08:00 and 18:00-19:00
        prices[i] = 3.0
    pauses = plan_day(prices)
    morning = [p for p in pauses if p.start <= DAY_START + 28 * SLOT < p.end]
    evening = [p for p in pauses if p.start <= DAY_START + 72 * SLOT < p.end]
    assert len(morning) == 1 and len(evening) == 1
    assert morning[0] is not evening[0]
    assert_pauses_follow_rules(pauses, quarters_from(DAY_START, prices), [], DAY_START, SETTINGS)


def test_a_pause_is_never_shorter_than_the_minimum() -> None:
    prices = flat_day(1.0)
    prices[40] = 5.0  # a single expensive quarter
    pauses = plan_day(prices)
    assert pauses
    assert all(p.minutes >= SETTINGS.min_minutes for p in pauses)
    assert any(p.start <= DAY_START + 40 * SLOT < p.end for p in pauses)


def test_a_long_spike_is_capped_at_the_maximum_pause() -> None:
    prices = flat_day(1.0)
    for i in range(30, 70):  # a 10 hour spike
        prices[i] = 3.0
    pauses = plan_day(prices)
    assert max(p.minutes for p in pauses) == SETTINGS.max_minutes
    assert_pauses_follow_rules(pauses, quarters_from(DAY_START, prices), [], DAY_START, SETTINGS)


def test_a_flat_day_still_follows_the_rules() -> None:
    # Whether a flat day is worth pausing is decided by the scoring in phase 4.
    pauses = plan_day(flat_day())
    assert minutes_paused(pauses) <= SETTINGS.budget_minutes_per_24h
    assert_pauses_follow_rules(
        pauses, quarters_from(DAY_START, flat_day()), [], DAY_START, SETTINGS
    )


def test_the_budget_is_the_total_paused_minutes() -> None:
    prices = [1.0 + (i % 7) for i in range(96)]
    pauses = plan_day(prices)
    assert minutes_paused(pauses) <= SETTINGS.budget_minutes_per_24h
    assert minutes_paused(pauses) >= 0.75 * SETTINGS.budget_minutes_per_24h


def test_a_half_spent_budget_leaves_half_to_plan() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [price_event(now - timedelta(hours=10), 180)]  # 3 h used, ages out from now + 14 h
    prices = [1.0 + (i % 7) for i in range(96)]
    pauses = plan_day(prices, history, now)
    before_history_ages_out = [p for p in pauses if p.end <= now + timedelta(hours=14)]
    assert 0 < minutes_paused(before_history_ages_out) <= SETTINGS.budget_minutes_per_24h - 180
    quarters = quarters_from(planner.floor_to_quarter(now), prices)
    assert_pauses_follow_rules(pauses, quarters, history, now, SETTINGS)


def test_a_spent_budget_gives_no_pauses_until_old_ones_age_out() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [
        price_event(now - timedelta(hours=10), 180),
        price_event(now - timedelta(hours=5), 180),
    ]
    prices = [1.0 + (i % 7) for i in range(96)]
    pauses = plan_day(prices, history, now)
    assert pauses
    assert min(p.start for p in pauses) >= now + timedelta(hours=14)
    quarters = quarters_from(planner.floor_to_quarter(now), prices)
    assert_pauses_follow_rules(pauses, quarters, history, now, SETTINGS)


def test_a_spent_budget_with_a_short_horizon_gives_no_pauses() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [
        price_event(now - timedelta(hours=10), 180),
        price_event(now - timedelta(hours=5), 180),
    ]
    assert plan_day([1.0 + (i % 7) for i in range(40)], history, now) == []


def test_old_pauses_leave_the_rolling_window() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [price_event(now - timedelta(hours=30), 180)]  # more than 24 h ago
    pauses = plan_day([1.0 + (i % 7) for i in range(96)], history, now)
    assert minutes_paused(pauses) >= 0.75 * SETTINGS.budget_minutes_per_24h


def test_budget_comes_back_as_old_pauses_age_out_during_the_horizon() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [price_event(now - timedelta(hours=23), 360)]  # leaves the window in 1 h to 7 h
    prices = [1.0] * 96
    prices[12] = 5.0  # 15:00, when part of the history has left the window
    pauses = plan_day(prices, history, now)
    assert pauses
    assert min(p.start for p in pauses) >= now + timedelta(hours=1)
    assert any(p.start <= now + 12 * SLOT < p.end for p in pauses)
    quarters = quarters_from(planner.floor_to_quarter(now), prices)
    assert_pauses_follow_rules(pauses, quarters, history, now, SETTINGS)


def test_recovery_after_a_recent_pause_blocks_the_next_quarters() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [price_event(now - timedelta(minutes=130), 120)]  # ended 10 min ago, recovery 60 min
    prices = flat_day(1.0)
    prices[0:4] = [9.0] * 4  # the most expensive quarters are inside the recovery window
    pauses = plan_day(prices, history, now)
    assert pauses
    assert min(p.start for p in pauses) >= now + timedelta(minutes=60)
    quarters = quarters_from(planner.floor_to_quarter(now), prices)
    assert_pauses_follow_rules(pauses, quarters, history, now, SETTINGS)


def test_a_stove_pause_in_history_does_not_use_price_budget_or_recovery() -> None:
    now = DAY_START + timedelta(hours=12)
    stove = PauseEvent(
        kind=PauseKind.STOVE,
        started_at=now - timedelta(hours=5),
        planned_end=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )
    prices = [9.0] * 4 + [1.0] * 92
    pauses = plan_day(prices, [stove], now)
    assert pauses[0].start == planner.floor_to_quarter(now)


def test_quarters_without_a_price_are_never_paused() -> None:
    prices: list[float | None] = [1.0 + (i % 5) for i in range(96)]
    for i in range(20, 40):
        prices[i] = None
    pauses = plan_day(prices)
    quarters = quarters_from(DAY_START, prices)
    assert_pauses_follow_rules(pauses, quarters, [], DAY_START, SETTINGS)
    for pause in pauses:
        assert not (pause.start < DAY_START + 40 * SLOT and pause.end > DAY_START + 20 * SLOT)


def test_the_horizon_starts_at_the_current_quarter() -> None:
    now = DAY_START + timedelta(hours=10, minutes=37)
    pauses = plan_day([1.0 + (i % 5) for i in range(40)], now=now)
    assert pauses
    assert min(p.start for p in pauses) >= DAY_START + timedelta(hours=10, minutes=30)


def test_quarters_before_now_are_ignored() -> None:
    now = DAY_START + timedelta(hours=1)
    prices = [9.0] * 4 + [1.0] * 92  # the expensive hour is already over
    quarters = quarters_from(DAY_START, prices)
    pauses = planner.plan_pauses(quarters, [], now, SETTINGS)
    assert all(p.start >= now for p in pauses)


def test_no_quarters_gives_no_pauses() -> None:
    assert planner.plan_pauses([], [], DAY_START, SETTINGS) == []


def test_zero_budget_gives_no_pauses() -> None:
    settings = PricePauseSettings(budget_minutes_per_24h=0)
    assert plan_day([1.0 + (i % 5) for i in range(96)], settings=settings) == []


def test_recovery_longer_than_the_pause_spaces_pauses_further_apart() -> None:
    settings = PricePauseSettings(recovery_fraction=2.0)
    prices = flat_day(1.0)
    for i in (10, 11, 12, 13, 20, 21, 22, 23):
        prices[i] = 5.0
    pauses = sorted(plan_day(prices, settings=settings), key=lambda p: p.start)
    for before, after in zip(pauses, pauses[1:], strict=False):
        assert after.start - before.end >= before.minutes * 2 * timedelta(minutes=1)


def test_plan_is_deterministic() -> None:
    prices = [1.0 + (i * 7 % 11) / 3 for i in range(96)]
    assert plan_day(prices) == plan_day(prices)


# --- porting helpers --------------------------------------------------------


def test_pause_blocks_lists_runs_of_paused_quarters() -> None:
    assert planner.pause_blocks([False, True, True, False, True]) == [(1, 2), (4, 1)]


RULES = planner.PauseRules(min_slots=2, max_slots=4, recovery_fraction=0.5, recovery_min_slots=2)


@pytest.mark.parametrize(
    ("paused", "ok"),
    [
        ([True, True, False, False, False], True),
        ([True, False, False, False], False),  # too short
        ([True] * 5 + [False] * 5, False),  # too long
        ([True, True, False, True, True, False, False, False], False),  # no recovery
        ([True, True, False, False, True, True, False], True),
        ([True, True, True, True, False, False, True, True], True),  # 4 long, 2 recovery
        ([True, True, True, True, False, True, True], False),  # 4 long, only 1 recovery
    ],
)
def test_follows_pause_rules(paused: list[bool], ok: bool) -> None:
    assert planner.follows_pause_rules(paused, RULES) is ok


def test_recovery_is_half_the_pause_but_at_least_the_minimum() -> None:
    rules = planner.PauseRules(2, 12, 0.5, 2)
    assert [rules.recovery_slots(n) for n in (2, 4, 5, 12)] == [2, 2, 3, 6]


def test_rules_from_settings() -> None:
    rules = planner.PauseRules.from_settings(SETTINGS)
    assert (rules.min_slots, rules.max_slots, rules.recovery_min_slots) == (2, 12, 2)
    assert rules.budget_minutes == 360


def test_settings_with_max_below_min_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_minutes"):
        planner.PauseRules.from_settings(PricePauseSettings(min_minutes=60, max_minutes=30))


# --- property test ----------------------------------------------------------


def random_day(seed: int):
    rng = random.Random(seed)
    now = DAY_START + timedelta(minutes=rng.randrange(0, 24 * 60))
    count = rng.randrange(8, 192)
    prices: list[float | None] = [round(rng.uniform(0.1, 4.0), 3) for _ in range(count)]
    if rng.random() < 0.3:  # prices with a spike pattern
        for i in range(0, count, rng.randrange(5, 30)):
            prices[i] = 6.0
    for _ in range(rng.randrange(0, 4)):  # holes in the price feed
        hole = rng.randrange(count)
        for i in range(hole, min(count, hole + rng.randrange(1, 10))):
            prices[i] = None
    history: list[PauseEvent] = []
    cursor = now - timedelta(hours=24)
    while rng.random() < 0.7 and cursor < now - timedelta(minutes=30):
        start = cursor + timedelta(minutes=rng.randrange(0, 360))
        length = rng.randrange(30, 181)
        if start + timedelta(minutes=length) > now:
            break
        history.append(price_event(start, length))
        cursor = start + timedelta(minutes=length)
    settings = PricePauseSettings(
        budget_minutes_per_24h=rng.choice([0, 90, 180, 360]),
        max_minutes=rng.choice([60, 120, 180]),
        recovery_fraction=rng.choice([0.5, 1.0]),
    )
    return now, prices, history, settings


@pytest.mark.parametrize("seed", range(200))
def test_random_days_never_violate_the_rules(seed: int) -> None:
    now, prices, history, settings = random_day(seed)
    start = planner.floor_to_quarter(now)
    quarters = quarters_from(start, prices)
    pauses = planner.plan_pauses(quarters, history, now, settings)
    assert_pauses_follow_rules(pauses, quarters, history, now, settings)
