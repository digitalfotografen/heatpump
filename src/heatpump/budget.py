"""Pause bookkeeping from the pause history: rolling 24 h minutes and recovery windows.

Pure functions on lists of :class:`PauseEvent`; the database supplies the events.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from heatpump.models import PauseEvent, PauseKind

WINDOW = timedelta(hours=24)
SLOT = timedelta(minutes=15)


def _overlap_minutes(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    overlap = min(a_end, b_end) - max(a_start, b_start)
    return max(0.0, overlap.total_seconds() / 60)


def _actual_end(event: PauseEvent, now: datetime) -> datetime:
    """When the pause really ended; an open pause has lasted until now."""
    return event.ended_at if event.ended_at is not None else now


def paused_minutes(
    events: Sequence[PauseEvent],
    since: datetime,
    until: datetime,
    now: datetime,
    kind: PauseKind | None = None,
) -> float:
    """Minutes the pump was paused inside ``[since, until]``, optionally for one kind only."""
    return sum(
        _overlap_minutes(event.started_at, _actual_end(event, now), since, until)
        for event in events
        if kind is None or event.kind is kind
    )


def price_paused_minutes_last_24h(events: Sequence[PauseEvent], now: datetime) -> float:
    """Price-pause minutes in the last 24 h; stove pauses are not charged to the price budget."""
    return paused_minutes(events, now - WINDOW, now, now, PauseKind.PRICE)


def paused_minutes_per_slot(
    events: Sequence[PauseEvent], first_slot: datetime, count: int, now: datetime
) -> list[float]:
    """Price-pause minutes inside each of ``count`` quarters starting at ``first_slot``."""
    return [
        paused_minutes(
            events, first_slot + SLOT * i, first_slot + SLOT * (i + 1), now, PauseKind.PRICE
        )
        for i in range(count)
    ]


def recovery_until(
    events: Sequence[PauseEvent], fraction: float, min_minutes: float
) -> datetime | None:
    """End of the recovery after the most recent finished price pause, or ``None`` if there is none.

    Recovery lasts ``fraction`` of the pause, but at least ``min_minutes``. The result may lie in
    the past; the caller compares it with the quarters it plans.
    """
    finished = [e for e in events if e.kind is PauseKind.PRICE and e.ended_at is not None]
    if not finished:
        return None
    last = max(finished, key=lambda e: e.ended_at)
    pause_minutes = (last.ended_at - last.started_at).total_seconds() / 60
    return last.ended_at + timedelta(minutes=max(min_minutes, pause_minutes * fraction))
