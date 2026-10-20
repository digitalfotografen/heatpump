"""Plain data types shared by the planner, controller and database.

All datetimes are timezone-aware UTC. Conversion to local time happens only
when printing or when a rule is expressed in local time.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class PauseKind(StrEnum):
    PRICE = "price"
    STOVE = "stove"


class StoveState(StrEnum):
    COLD = "cold"
    BURNING = "burning"
    STALE = "stale"


class ControlMode(StrEnum):
    """What the controller decided for one tick."""

    RUNNING = "running"
    PAUSED_PRICE = "paused_price"
    PAUSED_STOVE = "paused_stove"
    RECOVERY = "recovery"
    FORCED_RUN = "forced_run"


@dataclass(frozen=True)
class Quarter:
    """One price slot, keyed by its UTC start time."""

    start: datetime
    energy: float
    tax: float = 0.0
    total: float | None = None
    currency: str = "SEK"
    slot_minutes: int = 15

    @property
    def price(self) -> float:
        """The price the planner optimises: Tibber's ``energy`` component."""
        return self.energy

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.slot_minutes)


@dataclass(frozen=True)
class Pause:
    kind: PauseKind
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class Plan:
    """The result of one planner run; ``id`` is set once it is stored."""

    created_at: datetime
    reason: str
    horizon_start: datetime
    horizon_end: datetime
    expected_saving_sek: float = 0.0
    pauses: tuple[Pause, ...] = ()
    id: int | None = None


@dataclass(frozen=True)
class PauseEvent:
    """A pause that has started (and maybe ended); the source for budget bookkeeping."""

    kind: PauseKind
    started_at: datetime
    planned_end: datetime
    ended_at: datetime | None = None
    end_reason: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class StoveReading:
    ts: datetime
    temperature: float
    state: StoveState


@dataclass(frozen=True)
class TemperatureReading:
    """An outdoor or heating-circuit sample."""

    ts: datetime
    temperature: float
    detail: float | str | None = None  # cold junction (heating) or source (outdoor)


@dataclass(frozen=True)
class ControlDecision:
    """The outcome of one controller tick, as written to ``control_log``."""

    ts: datetime
    mode: ControlMode
    relay_requested: bool
    relay_actual: bool | None = None
    guard_reason: str | None = None
    stove_state: StoveState | None = None
    stove_temp: float | None = None
    outdoor_temp: float | None = None
    heating_temp: float | None = None
    price_now: float | None = None
    plan_id: int | None = None
