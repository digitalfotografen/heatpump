"""The data behind ``heatpump status``: the latest tick, the 24 h pause minutes and what is next.

Everything is read from the database, so the command works while the service runs.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from heatpump import budget, db, prices
from heatpump.config import Settings
from heatpump.models import ControlDecision, Pause, PauseKind, Plan

HISTORY = timedelta(hours=48)  # how far back "time in the current mode" looks


@dataclass(frozen=True)
class PausedMinutes:
    price: float
    stove: float
    total: float


@dataclass(frozen=True)
class Status:
    now: datetime
    service_running: bool
    last: ControlDecision
    mode_since: datetime
    paused_24h: PausedMinutes
    next_pause: Pause | None
    next_price_fetch: datetime

    @property
    def tick_age(self) -> timedelta:
        return self.now - self.last.ts

    def as_dict(self) -> dict:
        """JSON-ready: times as UTC ISO 8601, minutes as numbers."""
        last = self.last
        return {
            "now": db.to_iso(self.now),
            "service_running": self.service_running,
            "last_tick": db.to_iso(last.ts),
            "last_tick_age_seconds": int(self.tick_age.total_seconds()),
            "mode": last.mode.value,
            "mode_since": db.to_iso(self.mode_since),
            "relay_requested": last.relay_requested,
            "relay_actual": last.relay_actual,
            "guard_reason": last.guard_reason,
            "stove_state": last.stove_state.value if last.stove_state else None,
            "stove_temp": last.stove_temp,
            "outdoor_temp": last.outdoor_temp,
            "heating_temp": last.heating_temp,
            "price_now": last.price_now,
            "paused_minutes_24h": {
                "price": round(self.paused_24h.price, 1),
                "stove": round(self.paused_24h.stove, 1),
                "total": round(self.paused_24h.total, 1),
            },
            "next_pause": (
                {"start": db.to_iso(self.next_pause.start), "end": db.to_iso(self.next_pause.end)}
                if self.next_pause
                else None
            ),
            "next_price_fetch": db.to_iso(self.next_price_fetch),
        }


def mode_since(log: list[ControlDecision]) -> datetime:
    """Start of the unbroken run of the latest mode at the end of a time-ordered log."""
    since = log[-1].ts
    for decision in reversed(log):
        if decision.mode is not log[-1].mode:
            break
        since = decision.ts
    return since


def collect(
    conn: sqlite3.Connection, settings: Settings, now: datetime, service_is_running: bool
) -> Status | None:
    """The current status, or ``None`` if the controller has never ticked."""
    log = db.control_log_since(conn, now - HISTORY)
    if not log:
        return None
    events = db.pause_events_since(conn, now - budget.WINDOW)
    since = now - budget.WINDOW
    paused = PausedMinutes(
        price=budget.paused_minutes(events, since, now, now, PauseKind.PRICE),
        stove=budget.paused_minutes(events, since, now, now, PauseKind.STOVE),
        total=budget.paused_minutes(events, since, now, now),
    )
    zone = ZoneInfo(settings.location.timezone)
    next_fetch = prices.next_fetch_time(
        now.astimezone(zone), db.latest_price_slot(conn), settings.tibber.fetch_after_local_time
    )
    return Status(
        now=now,
        service_running=service_is_running,
        last=log[-1],
        mode_since=mode_since(log),
        paused_24h=paused,
        next_pause=_next_pause(db.latest_plan(conn), now),
        next_price_fetch=next_fetch,
    )


def _next_pause(plan: Plan | None, now: datetime) -> Pause | None:
    if plan is None:
        return None
    return next((p for p in plan.pauses if p.start > now), None)
