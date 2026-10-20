import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heatpump import planner
from heatpump.config import PricePauseSettings
from heatpump.models import Pause, PauseEvent, PauseKind, Plan, Quarter

SLOT = timedelta(minutes=15)
DAY_START = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
SETTINGS = PricePauseSettings()
FIXTURES = Path(__file__).parent / "fixtures"


def quarters_from(start: datetime, prices: list[float | None]) -> list[Quarter]:
    return [
        Quarter(start=start + SLOT * i, energy=price)
        for i, price in enumerate(prices)
        if price is not None
    ]


def plan_for(
    prices: list[float | None],
    now: datetime = DAY_START,
    history: list[PauseEvent] | None = None,
    settings: PricePauseSettings = SETTINGS,
    stove_pause: Pause | None = None,
) -> Plan:
    start = planner.floor_to_quarter(now)
    return planner.plan_pauses(
        quarters_from(start, prices), history or [], now, settings, stove_pause=stove_pause
    )


def load_day(day: str) -> list[Quarter]:
    with (FIXTURES / f"prices_{day}.csv").open() as file:
        return [
            Quarter(
                start=datetime.fromisoformat(row["start"].replace("Z", "+00:00")),
                energy=float(row["energy"]),
            )
            for row in csv.DictReader(file)
        ]


def plan_fixture_day(day: str) -> tuple[Plan, list[Quarter]]:
    quarters = load_day(day)
    plan = planner.plan_pauses(quarters, [], quarters[0].start, SETTINGS)
    return plan, quarters


def net_saving_sek(pause: Pause, quarters: list[Quarter], demand_kw: float) -> float:
    """Independent re-computation of a pause's saving against the same-length catch-up."""
    by_start = {q.start: q.energy for q in quarters}
    saving = 0.0
    for k in range(int(pause.minutes // 15)):
        paused_at = by_start[pause.start + SLOT * k]
        caught_up_at = by_start.get(pause.end + SLOT * k)
        if caught_up_at is not None:
            saving += paused_at - caught_up_at
    return saving * demand_kw * 0.25


# --- scoring ----------------------------------------------------------------


def test_a_peak_followed_by_cheap_hours_is_worth_pausing() -> None:
    prices = [1.0] * 96
    prices[48:52] = [3.0] * 4
    plan = plan_for(prices)
    assert plan.reason == "planned"
    assert plan.pauses
    # 4 quarters at 3.0 against 4 at 1.0, 1 kW, 0.25 h per quarter
    assert plan.expected_saving_sek == pytest.approx(4 * 2.0 * 0.25)


def test_a_pause_whose_catch_up_lands_on_a_higher_price_is_dropped() -> None:
    # prices climb all day: whatever is paused is caught up at a higher price
    prices = [1.0 + i * 0.05 for i in range(96)]
    plan = plan_for(prices)
    assert plan.pauses == ()
    assert plan.expected_saving_sek == 0.0


def test_the_budget_of_a_dropped_pause_goes_to_the_next_candidate() -> None:
    prices = [1.0] * 96
    prices[60:64] = [4.0] * 4  # the most expensive hour, but the price stays high after it
    prices[64:72] = [4.0] * 8
    prices[20:24] = [3.0] * 4  # a smaller peak followed by cheap hours
    plan = plan_for(prices)
    assert any(p.start <= DAY_START + 20 * SLOT < p.end for p in plan.pauses)


def test_every_planned_pause_saves_money() -> None:
    prices = [1.0 + ((i * 37) % 11) / 4 for i in range(96)]
    plan = plan_for(prices)
    quarters = quarters_from(DAY_START, prices)
    assert plan.pauses
    for pause in plan.pauses:
        assert net_saving_sek(pause, quarters, SETTINGS.heat_demand_kw) > 0


def test_expected_saving_is_the_sum_over_the_pauses() -> None:
    prices = [1.0] * 96
    for i in (28, 29, 30, 31, 72, 73, 74, 75):
        prices[i] = 3.0
    plan = plan_for(prices)
    quarters = quarters_from(DAY_START, prices)
    expected = sum(net_saving_sek(p, quarters, SETTINGS.heat_demand_kw) for p in plan.pauses)
    assert plan.expected_saving_sek == pytest.approx(expected)


def test_heat_demand_scales_the_saving() -> None:
    prices = [1.0] * 96
    prices[48:52] = [3.0] * 4
    base = plan_for(prices).expected_saving_sek
    doubled = plan_for(prices, settings=PricePauseSettings(heat_demand_kw=2.0)).expected_saving_sek
    assert doubled == pytest.approx(2 * base)


def test_catch_up_beyond_the_known_prices_claims_no_saving() -> None:
    prices = [1.0] * 40
    prices[38:40] = [9.0, 9.0]  # the peak is the last known quarters; the catch-up is unknown
    plan = plan_for(prices, settings=PricePauseSettings(min_expected_saving_sek=0))
    assert plan.expected_saving_sek == 0.0
    assert plan.pauses == ()


# --- minimum saving -----------------------------------------------------------


def test_a_flat_day_gives_an_empty_plan() -> None:
    plan = plan_for([1.0] * 96)
    assert plan.pauses == ()
    assert plan.reason == "flat-day"


def test_a_plan_below_the_minimum_saving_is_empty() -> None:
    prices = [1.0] * 96
    prices[48:52] = [1.2] * 4  # saves 4 * 0.2 * 0.25 = 0.2 SEK
    plan = plan_for(prices)
    assert plan.pauses == ()
    assert plan.reason == "flat-day"


def test_a_lower_minimum_saving_accepts_the_same_day() -> None:
    prices = [1.0] * 96
    prices[48:52] = [1.2] * 4
    plan = plan_for(prices, settings=PricePauseSettings(min_expected_saving_sek=0.1))
    assert plan.pauses
    assert plan.reason == "planned"


def test_no_prices_gives_a_plan_that_says_so() -> None:
    plan = planner.plan_pauses([], [], DAY_START, SETTINGS)
    assert (plan.pauses, plan.reason) == ((), "no-prices")


# --- horizon ------------------------------------------------------------------


def test_the_horizon_runs_from_the_current_quarter_to_the_last_known_price() -> None:
    now = DAY_START + timedelta(hours=10, minutes=37)
    plan = plan_for([1.0] * 100, now=now)
    assert plan.created_at == now
    assert plan.horizon_start == DAY_START + timedelta(hours=10, minutes=30)
    assert plan.horizon_end == plan.horizon_start + SLOT * 100


def test_the_horizon_crosses_midnight_when_tomorrow_is_known() -> None:
    now = DAY_START + timedelta(hours=18)  # 18:00 UTC; prices to the end of the next day
    prices = [1.0] * 24 + [1.0] * 96
    prices[28:32] = [4.0] * 4  # tomorrow 01:00 UTC
    plan = plan_for(prices, now=now)
    assert plan.horizon_end == now + SLOT * 120
    assert any(p.start >= DAY_START + timedelta(days=1) for p in plan.pauses)


def test_a_pause_can_span_midnight() -> None:
    now = DAY_START + timedelta(hours=22)
    prices = [1.0] * 24 + [1.0] * 24
    prices[6:10] = [4.0] * 4  # 23:30 to 00:30
    plan = plan_for(prices, now=now)
    spanning = [p for p in plan.pauses if p.start < DAY_START + timedelta(days=1) < p.end]
    assert len(spanning) == 1


# --- stove pause --------------------------------------------------------------


def stove_pause(start: datetime, end: datetime) -> Pause:
    return Pause(PauseKind.STOVE, start, end)


def test_the_stove_pause_interval_is_never_planned_as_a_price_pause() -> None:
    now = DAY_START + timedelta(hours=12)
    prices = [1.0] * 96
    prices[0:4] = [5.0] * 4  # peak inside the stove pause
    stove = stove_pause(now - timedelta(hours=1), now + timedelta(hours=1))
    plan = plan_for(prices, now=now, stove_pause=stove)
    for pause in plan.pauses:
        assert pause.start >= now + timedelta(hours=1)


def test_the_recovery_after_a_stove_pause_is_blocked_too() -> None:
    now = DAY_START + timedelta(hours=12)
    prices = [1.0] * 96
    prices[4:8] = [5.0] * 4  # 1 h to 2 h from now, while the stove pause is still running
    prices[8:12] = [5.0] * 4  # 2 h to 3 h from now, in the recovery
    prices[16:20] = [4.0] * 4  # 4 h from now, after the recovery
    stove = stove_pause(now - timedelta(hours=3), now + timedelta(hours=1))
    # a 4 h stove pause is followed by 2 h recovery with the default stove settings
    plan = plan_for(prices, now=now, stove_pause=stove)
    assert plan.pauses
    assert min(p.start for p in plan.pauses) >= now + timedelta(hours=3)


def test_the_stove_pause_is_not_charged_to_the_price_budget() -> None:
    now = DAY_START + timedelta(hours=12)
    stove = stove_pause(now - timedelta(hours=5), now + timedelta(minutes=30))
    prices = [1.0] * 96
    prices[16:20] = [5.0] * 4  # well after the recovery
    plan = plan_for(prices, now=now, stove_pause=stove)
    assert plan.pauses


def test_a_stove_pause_in_the_past_has_no_effect() -> None:
    now = DAY_START + timedelta(hours=12)
    stove = stove_pause(now - timedelta(hours=8), now - timedelta(hours=6))
    prices = [5.0] * 2 + [1.0] * 94
    plan = plan_for(prices, now=now, stove_pause=stove)
    assert plan.pauses and plan.pauses[0].start == now


# --- price pause in progress ------------------------------------------------


def open_price_event(started_at: datetime, minutes: int = 120) -> PauseEvent:
    return PauseEvent(
        kind=PauseKind.PRICE,
        started_at=started_at,
        planned_end=started_at + timedelta(minutes=minutes),
    )


def test_a_pause_in_progress_is_kept_until_its_minimum_length() -> None:
    now = DAY_START + timedelta(hours=12, minutes=15)
    history = [open_price_event(DAY_START + timedelta(hours=12))]
    prices = [1.0] * 96  # nothing is expensive: a fresh plan would not pause
    plan = plan_for(prices, now=now, history=history)
    assert len(plan.pauses) == 1
    kept = plan.pauses[0]
    assert kept.start == DAY_START + timedelta(hours=12)
    assert kept.end >= DAY_START + timedelta(hours=12, minutes=30)


def test_a_pause_in_progress_longer_than_the_minimum_may_be_ended() -> None:
    now = DAY_START + timedelta(hours=13)
    history = [open_price_event(DAY_START + timedelta(hours=12))]
    plan = plan_for([1.0] * 96, now=now, history=history)
    assert plan.pauses == ()


def test_a_pause_in_progress_is_extended_when_the_next_quarters_are_expensive() -> None:
    now = DAY_START + timedelta(hours=12, minutes=15)
    history = [open_price_event(DAY_START + timedelta(hours=12))]
    prices = [1.0] * 96
    prices[0:4] = [4.0] * 4
    plan = plan_for(prices, now=now, history=history)
    assert plan.pauses[0].start == DAY_START + timedelta(hours=12)
    assert plan.pauses[0].end >= now + timedelta(hours=1)


def test_a_flat_day_still_keeps_the_pause_in_progress_to_its_minimum() -> None:
    now = DAY_START + timedelta(hours=12)
    history = [open_price_event(now - timedelta(minutes=5))]
    plan = plan_for([1.0] * 96, now=now, history=history)
    assert plan.reason == "flat-day"
    assert len(plan.pauses) == 1 and plan.pauses[0].minutes >= SETTINGS.min_minutes


def test_a_pause_in_progress_past_the_maximum_is_not_kept() -> None:
    now = DAY_START + timedelta(hours=16)
    history = [open_price_event(DAY_START + timedelta(hours=12), minutes=240)]
    plan = plan_for([1.0] * 96, now=now, history=history)
    assert plan.pauses == ()


# --- real days ----------------------------------------------------------------


def test_2026_08_11_has_no_money_losing_pause() -> None:
    plan, quarters = plan_fixture_day("2026-08-11")
    for pause in plan.pauses:
        assert net_saving_sek(pause, quarters, SETTINGS.heat_demand_kw) > 0


def test_2026_10_10_is_a_flat_day() -> None:
    plan, _ = plan_fixture_day("2026-10-10")
    assert plan.pauses == ()


def test_2026_02_19_pauses_over_the_morning_spike() -> None:
    plan, quarters = plan_fixture_day("2026-02-19")
    spike = max(
        (q for q in quarters if q.start.hour in range(5, 10)),  # 06:00-11:00 local in winter
        key=lambda q: q.energy,
    )
    assert any(p.start <= spike.start < p.end for p in plan.pauses)
    assert plan.reason == "planned"


def test_2026_09_23_follows_the_rules_and_saves_money() -> None:
    plan, quarters = plan_fixture_day("2026-09-23")
    assert sum(p.minutes for p in plan.pauses) <= SETTINGS.budget_minutes_per_24h
    for pause in plan.pauses:
        assert net_saving_sek(pause, quarters, SETTINGS.heat_demand_kw) > 0
