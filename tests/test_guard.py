import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from heatpump.config import GuardSettings
from heatpump.guard import BUILD_DATE, GuardReason, GuardState, decide
from heatpump.models import PauseKind, StoveState

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SETTINGS = GuardSettings(absolute_max_pause_minutes=360, max_paused_minutes_per_24h=720)


def healthy(**changes: object) -> GuardState:
    """A state in which a price pause is allowed; tests change one thing at a time."""
    state = GuardState(
        now=NOW,
        pause_kind=PauseKind.PRICE,
        paused_since=NOW - timedelta(minutes=30),
        paused_minutes_24h=60,
        price_present=True,
        stove_state=StoveState.COLD,
        last_tick_at=NOW - timedelta(minutes=1),
        force_run=False,
        readback_mismatches=0,
    )
    return replace(state, **changes)


def test_healthy_state_lets_a_requested_pause_through() -> None:
    assert decide(True, healthy(), SETTINGS) == (True, None)


def test_no_request_means_run_without_a_reason() -> None:
    assert decide(False, healthy(force_run=True), SETTINGS) == (False, None)


def test_first_pause_tick_has_no_paused_since() -> None:
    assert decide(True, healthy(paused_since=None), SETTINGS) == (True, None)


def test_pause_longer_than_absolute_max_is_released() -> None:
    state = healthy(paused_since=NOW - timedelta(minutes=361))
    assert decide(True, state, SETTINGS) == (False, GuardReason.ABSOLUTE_MAX_PAUSE)


def test_pause_of_exactly_absolute_max_is_allowed() -> None:
    state = healthy(paused_since=NOW - timedelta(minutes=360))
    assert decide(True, state, SETTINGS) == (True, None)


def test_absolute_max_applies_to_stove_pauses_too() -> None:
    state = healthy(
        pause_kind=PauseKind.STOVE,
        stove_state=StoveState.BURNING,
        paused_since=NOW - timedelta(minutes=400),
    )
    assert decide(True, state, SETTINGS) == (False, GuardReason.ABSOLUTE_MAX_PAUSE)


def test_more_than_the_daily_total_is_released() -> None:
    state = healthy(paused_minutes_24h=720.5)
    assert decide(True, state, SETTINGS) == (False, GuardReason.MAX_PAUSED_PER_24H)


def test_exactly_the_daily_total_is_allowed() -> None:
    assert decide(True, healthy(paused_minutes_24h=720), SETTINGS) == (True, None)


def test_price_pause_without_a_price_is_released() -> None:
    state = healthy(price_present=False)
    assert decide(True, state, SETTINGS) == (False, GuardReason.NO_PRICE)


def test_stove_pause_does_not_need_a_price() -> None:
    state = healthy(pause_kind=PauseKind.STOVE, stove_state=StoveState.BURNING, price_present=False)
    assert decide(True, state, SETTINGS) == (True, None)


@pytest.mark.parametrize("kind", [PauseKind.PRICE, PauseKind.STOVE])
def test_stove_pause_with_stale_feed_is_released(kind: PauseKind) -> None:
    state = healthy(pause_kind=kind, stove_state=StoveState.STALE)
    expected = (False, GuardReason.STOVE_STALE) if kind is PauseKind.STOVE else (True, None)
    assert decide(True, state, SETTINGS) == expected


def test_clock_before_the_build_date_is_released() -> None:
    before = BUILD_DATE - timedelta(days=1)
    state = healthy(now=before, last_tick_at=None, paused_since=None)
    assert decide(True, state, SETTINGS) == (False, GuardReason.CLOCK_IMPLAUSIBLE)


@pytest.mark.parametrize("jump", [timedelta(minutes=61), timedelta(minutes=-61)])
def test_clock_jump_of_more_than_an_hour_is_released(jump: timedelta) -> None:
    state = healthy(last_tick_at=NOW - jump, paused_since=None)
    assert decide(True, state, SETTINGS) == (False, GuardReason.CLOCK_IMPLAUSIBLE)


def test_a_gap_of_an_hour_is_still_plausible() -> None:
    state = healthy(last_tick_at=NOW - timedelta(minutes=60), paused_since=None)
    assert decide(True, state, SETTINGS) == (True, None)


def test_first_tick_after_start_has_no_jump_to_judge() -> None:
    state = healthy(last_tick_at=None, paused_since=None)
    assert decide(True, state, SETTINGS) == (True, None)


def test_force_run_releases() -> None:
    assert decide(True, healthy(force_run=True), SETTINGS) == (False, GuardReason.FORCE_RUN)


def test_one_readback_mismatch_releases_and_asks_for_a_retry() -> None:
    state = healthy(readback_mismatches=1)
    assert decide(True, state, SETTINGS) == (False, GuardReason.READBACK_MISMATCH)


def test_two_readback_mismatches_in_a_row_keep_it_released_and_alert() -> None:
    state = healthy(readback_mismatches=2)
    assert decide(True, state, SETTINGS) == (False, GuardReason.READBACK_MISMATCH_REPEATED)
    assert GuardReason.READBACK_MISMATCH_REPEATED.is_alert


def test_a_single_mismatch_is_not_an_alert() -> None:
    assert not GuardReason.READBACK_MISMATCH.is_alert


def test_requested_pause_of_unknown_kind_is_released() -> None:
    state = healthy(pause_kind=None)
    assert decide(True, state, SETTINGS) == (False, GuardReason.UNKNOWN_PAUSE_KIND)


def test_force_run_wins_over_other_reasons() -> None:
    state = healthy(force_run=True, price_present=False, paused_minutes_24h=9999)
    assert decide(True, state, SETTINGS) == (False, GuardReason.FORCE_RUN)


def random_state(rng: random.Random) -> GuardState:
    now = NOW + timedelta(minutes=rng.randint(-3 * 24 * 60, 3 * 24 * 60))
    return GuardState(
        now=now,
        pause_kind=rng.choice([None, PauseKind.PRICE, PauseKind.STOVE]),
        paused_since=rng.choice([None, now - timedelta(minutes=rng.randint(-10, 900))]),
        paused_minutes_24h=rng.uniform(0, 1500),
        price_present=rng.random() < 0.5,
        stove_state=rng.choice(list(StoveState)),
        last_tick_at=rng.choice([None, now - timedelta(minutes=rng.randint(-200, 200))]),
        force_run=rng.random() < 0.2,
        readback_mismatches=rng.choice([0, 0, 0, 1, 2, 5]),
    )


def test_guard_can_never_turn_false_into_true() -> None:
    rng = random.Random(637)
    for _ in range(20_000):
        relay, reason = decide(False, random_state(rng), SETTINGS)
        assert relay is False
        assert reason is None


def test_guard_never_pauses_without_a_reason_to_allow_it() -> None:
    """Whenever the guard lets a pause through, none of its rules is violated."""
    rng = random.Random(1)
    allowed = 0
    for _ in range(20_000):
        state = random_state(rng)
        relay, reason = decide(True, state, SETTINGS)
        assert (reason is None) == relay
        if relay:
            allowed += 1
            assert not state.force_run
            assert state.readback_mismatches == 0
            assert state.pause_kind is not None
            assert state.paused_minutes_24h <= SETTINGS.max_paused_minutes_per_24h
    assert allowed > 0
