"""The price planner: choose the quarters in which the heat pump may be paused.

A pure function with no I/O. It is a port of ``pick_pauses_with_hold`` and
``follows_pause_rules`` from the simulation study (``heatpump_models.py``), extended with a
rolling 24 h budget, the recovery after the last pause, and quarters without a price.
Scoring pauses by net saving comes in phase 4; here every pause the rules allow is kept.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from heatpump import budget
from heatpump.config import PricePauseSettings
from heatpump.models import Pause, PauseEvent, PauseKind, Quarter

SLOT_MINUTES = 15
SLOT = timedelta(minutes=SLOT_MINUTES)
WINDOW_SLOTS = 24 * 60 // SLOT_MINUTES


@dataclass(frozen=True)
class PauseRules:
    """The pause rules expressed in whole quarters."""

    min_slots: int
    max_slots: int
    recovery_fraction: float
    recovery_min_slots: int
    budget_minutes: float = 0

    @classmethod
    def from_settings(cls, settings: PricePauseSettings) -> "PauseRules":
        """:raises ValueError: if ``max_minutes`` cannot hold a pause of ``min_minutes``."""
        min_slots = math.ceil(settings.min_minutes / SLOT_MINUTES)
        max_slots = settings.max_minutes // SLOT_MINUTES
        if max_slots < min_slots:
            raise ValueError(
                f"price_pause.max_minutes ({settings.max_minutes}) is shorter than "
                f"min_minutes ({settings.min_minutes}) rounded up to whole quarters"
            )
        return cls(
            min_slots=min_slots,
            max_slots=max_slots,
            recovery_fraction=settings.recovery_fraction,
            recovery_min_slots=math.ceil(settings.recovery_min_minutes / SLOT_MINUTES),
            budget_minutes=settings.budget_minutes_per_24h,
        )

    def recovery_slots(self, pause_slots: int) -> int:
        """Quarters the pump must run after a pause of ``pause_slots`` quarters."""
        return max(self.recovery_min_slots, math.ceil(pause_slots * self.recovery_fraction))


def floor_to_quarter(moment: datetime) -> datetime:
    """Round down to the start of the quarter, in UTC."""
    moment = moment.astimezone(UTC)
    return moment.replace(
        minute=moment.minute // SLOT_MINUTES * SLOT_MINUTES, second=0, microsecond=0
    )


def pause_blocks(paused: Sequence[bool]) -> list[tuple[int, int]]:
    """(start, length) of every run of consecutive paused quarters."""
    blocks: list[tuple[int, int]] = []
    start = None
    for i, is_paused in enumerate([*paused, False]):
        if is_paused and start is None:
            start = i
        elif not is_paused and start is not None:
            blocks.append((start, i - start))
            start = None
    return blocks


def follows_pause_rules(paused: Sequence[bool], rules: PauseRules) -> bool:
    """Every pause is between min and max length and is followed by enough running time.

    The running time after the last pause is not checked: it lies beyond the horizon and is
    enforced when the next plan is made.
    """
    blocks = pause_blocks(paused)
    for (start, length), next_block in zip(blocks, [*blocks[1:], None], strict=True):
        if not rules.min_slots <= length <= rules.max_slots:
            return False
        if next_block is not None:
            run_after = next_block[0] - (start + length)
            if run_after < rules.recovery_slots(length):
                return False
    return True


def pick_pauses_with_hold(
    prices: Sequence[float | None],
    min_slots: int,
    rules_ok: Callable[[list[bool]], bool],
) -> list[bool]:
    """Greedy pause planner where every pause is at least ``min_slots`` quarters long.

    Walk the quarters from most to least expensive. A quarter next to an existing pause tries to
    extend it; otherwise it tries to start a new pause with the most expensive window of
    ``min_slots`` quarters that contains it. A change is kept only if ``rules_ok`` accepts the
    result. Quarters with a price of ``None`` are never paused.
    """
    n = len(prices)
    paused = [False] * n

    def try_pause(slots: list[int]) -> bool:
        new = [s for s in slots if not paused[s]]
        for s in new:
            paused[s] = True
        if rules_ok(paused):
            return True
        for s in new:
            paused[s] = False
        return False

    priced = [i for i in range(n) if prices[i] is not None]
    for i in sorted(priced, key=lambda i: prices[i], reverse=True):
        if paused[i]:
            continue
        next_to_pause = (i > 0 and paused[i - 1]) or (i < n - 1 and paused[i + 1])
        if next_to_pause and try_pause([i]):
            continue
        windows = [
            list(range(s, s + min_slots))
            for s in range(i - min_slots + 1, i + 1)
            if s >= 0
            and s + min_slots <= n
            and all(prices[q] is not None for q in range(s, s + min_slots))
        ]
        windows.sort(key=lambda w: sum(prices[q] for q in w), reverse=True)
        for window in windows:
            if try_pause(window):
                break
    return paused


def plan_pauses(
    quarters: Sequence[Quarter],
    history: Sequence[PauseEvent],
    now: datetime,
    settings: PricePauseSettings,
) -> list[Pause]:
    """Plan price pauses from the current quarter to the end of the known prices.

    :param quarters: stored prices; quarters before the current one are ignored and a quarter
        that is missing (or has no price) is never paused.
    :param history: pause events of the last 24 h, from the database.
    :param now: the current time.
    :param settings: ``price_pause`` section of the configuration.
    :return: price pauses in time order, aligned to quarters.
    :raises ValueError: if a quarter is not 15 minutes long or the pause lengths are inconsistent.
    """
    rules = PauseRules.from_settings(settings)
    first_slot = floor_to_quarter(now)
    prices = _prices_on_grid(quarters, first_slot)
    if not prices:
        return []

    recovery_end = budget.recovery_until(
        history, settings.recovery_fraction, settings.recovery_min_minutes
    )
    in_recovery = [
        recovery_end is not None and first_slot + SLOT * i < recovery_end
        for i in range(len(prices))
    ]
    used_before = budget.paused_minutes_per_slot(
        history, first_slot - SLOT * WINDOW_SLOTS, WINDOW_SLOTS + len(prices), now
    )

    def rules_ok(paused: list[bool]) -> bool:
        return (
            follows_pause_rules(paused, rules)
            and not any(p and r for p, r in zip(paused, in_recovery, strict=True))
            and _within_rolling_budget(paused, used_before, rules.budget_minutes)
        )

    paused = pick_pauses_with_hold(prices, rules.min_slots, rules_ok)
    return [
        Pause(PauseKind.PRICE, first_slot + SLOT * start, first_slot + SLOT * (start + length))
        for start, length in pause_blocks(paused)
    ]


def _prices_on_grid(quarters: Sequence[Quarter], first_slot: datetime) -> list[float | None]:
    """One price per quarter from ``first_slot`` to the last known quarter; ``None`` for holes."""
    prices: dict[int, float] = {}
    for quarter in quarters:
        if quarter.slot_minutes != SLOT_MINUTES:
            raise ValueError(f"Quarter {quarter.start} is {quarter.slot_minutes} minutes long")
        index = (quarter.start - first_slot) // SLOT
        if index >= 0:
            prices[index] = quarter.price
    return [prices.get(i) for i in range(max(prices, default=-1) + 1)]


def _within_rolling_budget(
    paused: Sequence[bool], used_before: Sequence[float], budget_minutes: float
) -> bool:
    """Every 24 h window that contains a planned pause stays within the budget.

    ``used_before`` holds the already-paused minutes per quarter, starting 24 h before the
    horizon, so planned quarter ``i`` is at position ``WINDOW_SLOTS + i``.
    """
    load = list(used_before)
    for i, is_paused in enumerate(paused):
        if is_paused:
            load[WINDOW_SLOTS + i] += SLOT_MINUTES
    prefix = [0.0]
    for minutes in load:
        prefix.append(prefix[-1] + minutes)
    planned_prefix = [0]
    for is_paused in paused:
        planned_prefix.append(planned_prefix[-1] + is_paused)
    for i in range(len(paused)):
        window_first = i + 1  # window of WINDOW_SLOTS quarters ending at planned quarter i
        planned_in_window = planned_prefix[i + 1] - planned_prefix[max(0, i + 1 - WINDOW_SLOTS)]
        if (
            planned_in_window
            and prefix[WINDOW_SLOTS + i + 1] - prefix[window_first] > budget_minutes + 1e-9
        ):
            return False
    return True
