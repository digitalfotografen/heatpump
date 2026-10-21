"""Replay the planner over a stored day: what would the controller have done?

Pure: no database, no relay. The caller supplies prices and gets the pauses back. Stove pauses
are not replayed because they depend on the fire, not on prices.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from heatpump import budget, planner
from heatpump.config import PricePauseSettings
from heatpump.models import Pause, PauseEvent, PauseKind, Quarter


@dataclass(frozen=True)
class ReplayResult:
    """The price pauses that started during the replayed day and what they would have saved."""

    pauses: list[Pause] = field(default_factory=list)
    saving_sek: float = 0.0


def replay_day(
    quarters: Sequence[Quarter],
    start: datetime,
    end: datetime,
    settings: PricePauseSettings,
) -> ReplayResult:
    """Plan every ``replan_interval_hours`` from ``start`` to ``end``, as the controller would.

    Each plan's pauses that begin before the next re-plan are taken as executed and become the
    history for the next plan, so the rolling 24 h budget is respected across the day.

    :param quarters: stored prices, including those after ``end`` that the planner may look at.
    :param start: first moment of the day (UTC).
    :param end: end of the day (UTC); no pause starts at or after it.
    :param settings: ``price_pause`` section of the configuration.
    :raises ValueError: if the planner rejects the prices (a quarter that is not 15 minutes).
    """
    interval = timedelta(hours=settings.replan_interval_hours)
    history: list[PauseEvent] = []
    executed: list[Pause] = []
    now = start
    while now < end:
        recent = [e for e in history if (e.ended_at or now) > now - budget.WINDOW]
        plan = planner.plan_pauses(quarters, recent, now, settings)
        next_replan = min(now + interval, end)
        for pause in plan.pauses:
            if pause.start < now or pause.start >= next_replan:
                continue
            executed.append(pause)
            history.append(
                PauseEvent(PauseKind.PRICE, pause.start, pause.end, pause.end, "planned")
            )
            next_replan = max(next_replan, min(pause.end, end))
        now = next_replan
    return ReplayResult(executed, _saving_sek(executed, quarters, settings.heat_demand_kw))


def _saving_sek(pauses: Sequence[Pause], quarters: Sequence[Quarter], demand_kw: float) -> float:
    """Sum of the pauses' net savings against the prices that follow them."""
    if not pauses:
        return 0.0
    first = planner.floor_to_quarter(min(p.start for p in pauses))
    by_start = {q.start: q.price for q in quarters}
    horizon = max(q.end for q in quarters)
    grid = [
        by_start.get(first + planner.SLOT * i) for i in range((horizon - first) // planner.SLOT)
    ]
    return sum(
        planner.net_saving_sek(
            grid,
            int((pause.start - first) / planner.SLOT),
            int(pause.minutes / planner.SLOT_MINUTES),
            demand_kw,
        )
        for pause in pauses
    )
