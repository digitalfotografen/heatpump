"""The price planner: choose the quarters in which the heat pump may be paused.

A pure function with no I/O. It is a port of ``pick_pauses_with_hold`` and
``follows_pause_rules`` from the simulation study (``heatpump_models.py``), extended with a
rolling 24 h budget, the recovery after the last pause, quarters without a price, a stove pause
as a blocked interval, and a price pause that is already in progress.

Every pause is scored by its net saving: the price of the paused quarters minus the price of the
same number of quarters right after it, where the pump catches up. Pauses that do not save money
are dropped and their budget goes to the next candidate.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from heatpump import budget
from heatpump.config import PricePauseSettings, StoveSettings
from heatpump.models import Pause, PauseEvent, PauseKind, Plan, Quarter

SLOT_MINUTES = 15
SLOT = timedelta(minutes=SLOT_MINUTES)
WINDOW_SLOTS = 24 * 60 // SLOT_MINUTES
SLOT_HOURS = SLOT_MINUTES / 60
LOOKAHEAD = timedelta(hours=48)  # Tibber publishes today and tomorrow at most


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
    for i, (start, length) in enumerate(blocks):
        next_block = blocks[i + 1] if i + 1 < len(blocks) else None
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
    initial: Sequence[bool] | None = None,
) -> list[bool]:
    """Greedy pause planner where every pause is at least ``min_slots`` quarters long.

    Walk the quarters from most to least expensive. A quarter next to an existing pause tries to
    extend it; otherwise it tries to start a new pause with the most expensive window of
    ``min_slots`` quarters that contains it. A change is kept only if ``rules_ok`` accepts the
    result. Quarters with a price of ``None`` are never newly paused. ``initial`` holds quarters
    that are paused from the start, such as a pause already in progress.
    """
    n = len(prices)
    paused = list(initial) if initial is not None else [False] * n

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
    stove_pause: Pause | None = None,
    stove_settings: StoveSettings | None = None,
) -> Plan:
    """Plan price pauses from the current quarter to the end of the known prices.

    :param quarters: stored prices; quarters before the current one are ignored and a quarter
        that is missing (or has no price) is never paused.
    :param history: pause events of the last 24 h, from the database. A price pause without an
        end is in progress and is kept until its minimum length.
    :param now: the current time.
    :param settings: ``price_pause`` section of the configuration.
    :param stove_pause: the current stove pause, if any; it and the recovery after it are blocked.
    :param stove_settings: ``stove`` section, for the recovery after the stove pause (default
        settings if omitted).
    :return: the plan; its pauses are price pauses in time order, aligned to quarters.
    :raises ValueError: if a quarter is not 15 minutes long or the pause lengths are inconsistent.
    """
    rules = PauseRules.from_settings(settings)
    stove_settings = stove_settings or StoveSettings()
    first_slot = floor_to_quarter(now)
    prices = _prices_on_grid(quarters, first_slot)
    horizon_end = first_slot + SLOT * len(prices)
    if not prices:
        return Plan(now, "no-prices", first_slot, first_slot)

    in_progress = next(
        (e for e in history if e.kind is PauseKind.PRICE and e.ended_at is None), None
    )
    elapsed = _elapsed_slots(in_progress, first_slot)
    grid_prices = [None] * elapsed + prices  # quarters of a pause in progress that lie behind us
    blocked = [False] * elapsed + _blocked_slots(
        history, stove_pause, stove_settings, settings, first_slot, len(prices)
    )
    used_before = budget.paused_minutes_per_slot(
        history, first_slot - SLOT * WINDOW_SLOTS, WINDOW_SLOTS + len(prices), now
    )

    def allowed(paused: list[bool]) -> bool:
        return (
            follows_pause_rules(paused, rules)
            and not any(p and b for p, b in zip(paused, blocked, strict=True))
            and _within_rolling_budget(paused[elapsed:], used_before, rules.budget_minutes)
        )

    kept = _kept_slots(in_progress, elapsed, rules, grid_prices)
    if not allowed(kept):
        kept = [False] * len(grid_prices)

    def rules_ok(paused: list[bool]) -> bool:
        return allowed(paused) and all(
            any(kept[start : start + length])
            or net_saving_sek(grid_prices, start, length, settings.heat_demand_kw) > 0
            for start, length in pause_blocks(paused)
        )

    paused = pick_pauses_with_hold(grid_prices, rules.min_slots, rules_ok, kept)

    def build(flags: Sequence[bool]) -> tuple[Pause, ...]:
        return tuple(
            Pause(
                PauseKind.PRICE,
                first_slot + SLOT * (start - elapsed),
                first_slot + SLOT * (start - elapsed + length),
            )
            for start, length in pause_blocks(flags)
            if start + length > elapsed
        )

    saving = sum(
        net_saving_sek(grid_prices, start, length, settings.heat_demand_kw)
        for start, length in pause_blocks(paused)
    )
    if saving < settings.min_expected_saving_sek:
        return Plan(now, "flat-day", first_slot, horizon_end, 0.0, build(kept))
    reason = "planned" if any(paused) else "no-pauses"
    return Plan(now, reason, first_slot, horizon_end, saving, build(paused))


def net_saving_sek(
    prices: Sequence[float | None], start: int, length: int, demand_kw: float
) -> float:
    """Saving of pausing ``length`` quarters from ``start`` and catching up in the next ones.

    Each paused quarter is compared with the quarter ``length`` later. A quarter with no known
    price on either side claims no saving, so a pause at the end of the horizon is not
    credited with a catch-up that has not been priced yet.
    """
    difference = 0.0
    for i in range(start, start + length):
        paused_price = prices[i]
        catch_up_price = prices[i + length] if i + length < len(prices) else None
        if paused_price is not None and catch_up_price is not None:
            difference += paused_price - catch_up_price
    return demand_kw * SLOT_HOURS * difference


def _elapsed_slots(in_progress: PauseEvent | None, first_slot: datetime) -> int:
    """Whole quarters of the pause in progress that lie before the current quarter."""
    if in_progress is None:
        return 0
    return max(0, (first_slot - floor_to_quarter(in_progress.started_at)) // SLOT)


def _kept_slots(
    in_progress: PauseEvent | None,
    elapsed: int,
    rules: PauseRules,
    grid_prices: Sequence[float | None],
) -> list[bool]:
    """The pause in progress, extended to its minimum length; nothing if it cannot be kept."""
    kept = [False] * len(grid_prices)
    if in_progress is None:
        return kept
    last = max(elapsed, rules.min_slots)
    if any(price is None for price in grid_prices[elapsed:last]):
        return kept
    kept[:last] = [True] * last
    return kept


def _blocked_slots(
    history: Sequence[PauseEvent],
    stove_pause: Pause | None,
    stove_settings: StoveSettings,
    settings: PricePauseSettings,
    first_slot: datetime,
    count: int,
) -> list[bool]:
    """Quarters that must run: after a price pause, and during a stove pause and after it."""
    intervals: list[tuple[datetime, datetime]] = []
    recovery_end = budget.recovery_until(
        history, settings.recovery_fraction, settings.recovery_min_minutes
    )
    if recovery_end is not None:
        intervals.append((first_slot - SLOT * WINDOW_SLOTS, recovery_end))
    if stove_pause is not None:
        recovery = max(
            stove_settings.recovery_min_minutes,
            stove_pause.minutes * stove_settings.recovery_fraction,
        )
        intervals.append((stove_pause.start, stove_pause.end + timedelta(minutes=recovery)))
    return [
        any(
            first_slot + SLOT * i < end and first_slot + SLOT * (i + 1) > start
            for start, end in intervals
        )
        for i in range(count)
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
