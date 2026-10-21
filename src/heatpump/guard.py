"""Safety guard between the controller's decision and the relay (plan §6).

A pure function with no I/O. It may only turn a requested pause into "run", never the other
way round, and it has the last word. The controller gathers a :class:`GuardState` each tick,
and acts on the reason: re-initialising the MCP23017 after a read-back mismatch, publishing
alerts, logging every forced release.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NamedTuple

from heatpump.config import GuardSettings
from heatpump.models import PauseKind, StoveState

BUILD_DATE = datetime(2026, 10, 20, tzinfo=UTC)  # a clock before this has not been set
MAX_CLOCK_JUMP = timedelta(hours=1)


class GuardReason(StrEnum):
    """Why the guard forced the pump to run."""

    FORCE_RUN = "force_run"
    CLOCK_IMPLAUSIBLE = "clock_implausible"
    READBACK_MISMATCH = "readback_mismatch"
    READBACK_MISMATCH_REPEATED = "readback_mismatch_repeated"
    ABSOLUTE_MAX_PAUSE = "absolute_max_pause"
    MAX_PAUSED_PER_24H = "max_paused_per_24h"
    NO_PRICE = "no_price"
    STOVE_STALE = "stove_stale"
    UNKNOWN_PAUSE_KIND = "unknown_pause_kind"

    @property
    def is_alert(self) -> bool:
        """Whether the controller should publish an alert, not only log the release."""
        return self is GuardReason.READBACK_MISMATCH_REPEATED


class GuardDecision(NamedTuple):
    """The relay state to apply (``True`` = paused) and, if the guard forced run, why."""

    relay: bool
    reason: GuardReason | None


@dataclass(frozen=True)
class GuardState:
    """Everything the guard looks at, collected by the controller at ``now``.

    ``paused_since`` is the start of the current uninterrupted relay-closed stretch, whatever
    its reason. ``readback_mismatches`` counts consecutive ticks whose read-back disagreed with
    the requested state.
    """

    now: datetime
    pause_kind: PauseKind | None
    paused_since: datetime | None
    paused_minutes_24h: float
    price_present: bool
    stove_state: StoveState
    last_tick_at: datetime | None
    force_run: bool
    readback_mismatches: int


def _clock_is_plausible(now: datetime, last_tick_at: datetime | None) -> bool:
    if now < BUILD_DATE:
        return False
    return last_tick_at is None or abs(now - last_tick_at) <= MAX_CLOCK_JUMP


def _readback_reason(mismatches: int) -> GuardReason | None:
    if mismatches >= 2:
        return GuardReason.READBACK_MISMATCH_REPEATED
    if mismatches == 1:
        return GuardReason.READBACK_MISMATCH
    return None


def _forced_run_reason(state: GuardState, settings: GuardSettings) -> GuardReason | None:
    """The first rule a requested pause breaks, most fundamental first; ``None`` if none."""
    if state.force_run:
        return GuardReason.FORCE_RUN
    if not _clock_is_plausible(state.now, state.last_tick_at):
        return GuardReason.CLOCK_IMPLAUSIBLE
    if reason := _readback_reason(state.readback_mismatches):
        return reason
    if state.paused_since is not None:
        paused_for = state.now - state.paused_since
        if paused_for > timedelta(minutes=settings.absolute_max_pause_minutes):
            return GuardReason.ABSOLUTE_MAX_PAUSE
    if state.paused_minutes_24h > settings.max_paused_minutes_per_24h:
        return GuardReason.MAX_PAUSED_PER_24H
    match state.pause_kind:
        case PauseKind.PRICE if not state.price_present:
            return GuardReason.NO_PRICE
        case PauseKind.STOVE if state.stove_state is StoveState.STALE:
            return GuardReason.STOVE_STALE
        case None:
            return GuardReason.UNKNOWN_PAUSE_KIND
    return None


def decide(requested: bool, state: GuardState, settings: GuardSettings) -> GuardDecision:
    """Apply the safety rules to a requested relay state; only ever turns a pause into run."""
    if not requested:
        return GuardDecision(False, None)
    reason = _forced_run_reason(state, settings)
    return GuardDecision(reason is None, reason)
