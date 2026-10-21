"""The controller: one tick per minute turns plan, stove and guard into a relay state.

Each tick decides which pause (if any) is wanted, asks the guard, drives the relay, reads it
back and writes one ``control_log`` row. Pauses are recorded as ``pause_events`` and the
controller rebuilds its memory from them after a restart. The pump may run in five modes
(:class:`~heatpump.models.ControlMode`); a stove pause overrules the price plan, and every
pause, whatever its kind, is followed by a recovery in which no new pause starts.

The relay is only ever touched here, always through the guard (plan §2).
"""

import asyncio
import logging
import signal
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from heatpump import budget, db, planner, prices
from heatpump.config import Settings, mqtt_credentials, tibber_token
from heatpump.guard import GuardDecision, GuardReason, GuardState, decide
from heatpump.heating import open_sensor, run_sampler
from heatpump.mcp9600 import Mcp9600, Mcp9600NotFoundError
from heatpump.models import (
    ControlDecision,
    ControlMode,
    Pause,
    PauseEvent,
    PauseKind,
    Plan,
    StoveState,
)
from heatpump.relay import Relay, RelayMismatch, ServiceLock, create_relay
from heatpump.stove import StoveListener, StoveMonitor, run_listener
from heatpump.weather import OpenMeteoProvider, is_stale, run_poller

log = logging.getLogger(__name__)

RESUME_GRACE = timedelta(minutes=5)  # a longer gap between ticks ends the pause on restart
HEATING_MAX_AGE = timedelta(minutes=5)  # an older heating-circuit sample is not logged
STARTUP_FETCH_TIMEOUT_SECONDS = 60  # Tibber trouble must not keep the controller from starting
PAUSE_MODES = {PauseKind.PRICE: ControlMode.PAUSED_PRICE, PauseKind.STOVE: ControlMode.PAUSED_STOVE}


@dataclass
class ControllerState:
    """What the controller remembers between ticks.

    ``active`` and ``paused_since`` are rebuilt from ``pause_events`` after a restart.
    ``paused_since`` is the start of the uninterrupted stretch with the relay closed, which can
    span a price pause that a stove pause took over.
    """

    active: PauseEvent | None = None
    paused_since: datetime | None = None
    last_tick_at: datetime | None = None
    readback_mismatches: int = 0
    last_guard_reason: GuardReason | None = None
    last_mode: ControlMode | None = None
    burning: bool = False
    plan: Plan | None = None
    planned_at: datetime | None = None
    planned_price_slot: datetime | None = None
    pending_replan: str | None = None


class Controller:
    """Owns the relay. Call :meth:`start` once, :meth:`tick` every minute, :meth:`shutdown` last."""

    def __init__(
        self, settings: Settings, conn: sqlite3.Connection, relay: Relay, stove: StoveMonitor
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._relay = relay
        self._stove = stove
        self.state = ControllerState()
        self.force_run = False  # latched by the MQTT command in phase 10

    @property
    def plan(self) -> Plan | None:
        return self.state.plan

    # --- lifecycle ----------------------------------------------------------

    def start(self, now: datetime) -> None:
        """Release the relay, take over a pause that was running before a restart, and plan."""
        self._release_relay()
        self._resume_open_pause(now)
        self._replan(now, "startup")

    def shutdown(self) -> None:
        """Release the relay; an open pause event stays open so a quick restart can resume it."""
        self._release_relay()

    def request_replan(self, reason: str) -> None:
        """Re-plan on the next tick (the ``replan`` command topic)."""
        self.state.pending_replan = reason

    def _release_relay(self) -> None:
        _release(self._relay)

    def _resume_open_pause(self, now: datetime) -> None:
        """Continue the open pause after a short gap; close it at the last tick after a long one."""
        event = db.open_pause(self._conn)
        if event is None:
            return
        last = db.latest_control_log(self._conn)
        if last is not None and now - last.ts <= RESUME_GRACE:
            self.state.active = event
            self.state.paused_since = self._stretch_start(event, now)
            log.info("Resuming the %s pause that started %s", event.kind, event.started_at)
            return
        ended_at = last.ts if last is not None else event.started_at
        db.close_pause_event(self._conn, event.id, ended_at, "restart_gap")
        log.warning(
            "Closed the %s pause from %s: no tick since %s", event.kind, event.started_at, ended_at
        )

    def _stretch_start(self, event: PauseEvent, now: datetime) -> datetime:
        """When the uninterrupted relay-closed stretch of ``event`` began."""
        earlier = db.pause_events_since(self._conn, now - budget.WINDOW)
        start = event.started_at
        while predecessor := next((e for e in earlier if e.ended_at == start), None):
            start = predecessor.started_at
        return start

    # --- the tick -----------------------------------------------------------

    def tick(self, now: datetime) -> ControlDecision:
        """Decide, drive the relay, log. Any unexpected error releases the relay."""
        try:
            return self._tick(now)
        except Exception:
            self._release_relay()
            raise

    def _tick(self, now: datetime) -> ControlDecision:
        state = self.state
        if state.readback_mismatches:
            self._reinitialise_relay()
        stove_state = self._stove.state(now)
        burning = stove_state is StoveState.BURNING
        price = self._price_now(now)
        plan_id = state.plan.id if state.plan else None

        self._end_pause_if_over(now, stove_state)
        events = db.pause_events_since(self._conn, now - budget.WINDOW)
        recovery_end, recovery_after = self._recovery(events)
        recovering = state.active is None and recovery_end > now
        wanted = self._wanted_pause(
            now, burning, recovering, stove_blocked=recovering and recovery_after is PauseKind.STOVE
        )
        guard = decide(
            wanted is not None,
            self._guard_state(now, wanted, events, price, stove_state),
            self._settings.guard,
        )
        mode = self._apply_decision(now, wanted, guard, recovering)
        reason = guard.reason or self._standing_alert()
        actual = self._drive_relay(now, guard.relay)
        decision = ControlDecision(
            ts=now,
            mode=mode,
            relay_requested=guard.relay,
            relay_actual=actual,
            guard_reason=reason.value if reason else None,
            stove_state=stove_state,
            stove_temp=self._stove.last_temperature
            if stove_state is not StoveState.STALE
            else None,
            outdoor_temp=self._outdoor_temperature(now),
            heating_temp=self._heating_temperature(now),
            price_now=price,
            plan_id=plan_id,
        )
        db.insert_control_log(self._conn, decision)
        self._report_changes(decision, reason)
        state.last_tick_at = now
        if reason := self._replan_reason(now, burning):
            self._replan(now, reason)
        state.burning = burning
        return decision

    # --- which pause is wanted ---------------------------------------------

    def _end_pause_if_over(self, now: datetime, stove_state: StoveState) -> None:
        """Close the active pause once its own rules say it is over."""
        active = self.state.active
        if active is None:
            return
        elapsed = now - active.started_at
        if active.kind is PauseKind.STOVE:
            settings = self._settings.stove
            if elapsed >= timedelta(minutes=settings.max_pause_minutes):
                self._close_active(now, "max_length")
            elif stove_state is not StoveState.BURNING and elapsed >= timedelta(
                minutes=settings.min_pause_minutes
            ):
                self._close_active(
                    now, "stove_cold" if stove_state is StoveState.COLD else "stove_stale"
                )
            return
        settings = self._settings.price_pause
        if stove_state is StoveState.BURNING:
            return  # the stove takes over from this price pause
        if not self._plan_covers(now) and elapsed >= timedelta(minutes=settings.min_minutes):
            self._close_active(now, "plan_ended")
        elif elapsed >= timedelta(minutes=settings.max_minutes):
            self._close_active(now, "max_length")

    def _wanted_pause(
        self, now: datetime, burning: bool, recovering: bool, stove_blocked: bool
    ) -> PauseKind | None:
        """The pause that should be in force this tick, before the guard has its say."""
        active = self.state.active
        if active is not None and active.kind is PauseKind.STOVE:
            return PauseKind.STOVE  # kept until its own rules end it
        if burning and (active is not None or not stove_blocked):  # price recovery doesn't block
            return PauseKind.STOVE
        if active is not None:
            return PauseKind.PRICE
        if recovering:
            return None
        return PauseKind.PRICE if self._plan_covers(now) else None

    def _plan_covers(self, now: datetime) -> bool:
        return self._covering_pause(now) is not None

    def _covering_pause(self, now: datetime) -> Pause | None:
        plan = self.state.plan
        return next((p for p in plan.pauses if p.start <= now < p.end), None) if plan else None

    def _recovery(self, events: Sequence[PauseEvent]) -> tuple[datetime, PauseKind | None]:
        """When the running time after the latest finished pause is over, and what it followed."""
        finished = [e for e in events if e.ended_at is not None]
        if not finished:
            return datetime.min.replace(tzinfo=UTC), None
        last = max(finished, key=lambda e: e.ended_at)
        settings = (
            self._settings.stove if last.kind is PauseKind.STOVE else self._settings.price_pause
        )
        minutes = (last.ended_at - last.started_at).total_seconds() / 60
        recovery = max(settings.recovery_min_minutes, minutes * settings.recovery_fraction)
        return last.ended_at + timedelta(minutes=recovery), last.kind

    # --- applying the decision ----------------------------------------------

    def _guard_state(
        self,
        now: datetime,
        wanted: PauseKind | None,
        events: Sequence[PauseEvent],
        price: float | None,
        stove_state: StoveState,
    ) -> GuardState:
        return GuardState(
            now=now,
            pause_kind=wanted,
            paused_since=self.state.paused_since,
            paused_minutes_24h=budget.paused_minutes(events, now - budget.WINDOW, now, now),
            price_present=price is not None,
            stove_state=stove_state,
            last_tick_at=self.state.last_tick_at,
            force_run=self.force_run,
            readback_mismatches=self.state.readback_mismatches,
        )

    def _apply_decision(
        self, now: datetime, wanted: PauseKind | None, guard: GuardDecision, recovering: bool
    ) -> ControlMode:
        """Open, switch or close the pause event to match the guarded decision; return the mode."""
        active = self.state.active
        if wanted is None:
            return ControlMode.RECOVERY if recovering else ControlMode.RUNNING
        if not guard.relay:
            if active is not None:
                self._close_active(now, f"guard:{guard.reason}")
            return ControlMode.FORCED_RUN
        if active is not None and active.kind is not wanted:
            self._close_active(now, "stove_override")
            active = None
        if active is None:
            self._open_pause(now, wanted)
        return PAUSE_MODES[wanted]

    def _open_pause(self, now: datetime, kind: PauseKind) -> None:
        if kind is PauseKind.STOVE:
            planned_end = now + timedelta(minutes=self._settings.stove.max_pause_minutes)
        elif covering := self._covering_pause(now):
            planned_end = covering.end
        else:
            planned_end = now + timedelta(minutes=self._settings.price_pause.min_minutes)
        event_id = db.open_pause_event(self._conn, kind, now, planned_end)
        self.state.active = PauseEvent(kind, now, planned_end, id=event_id)
        if kind is PauseKind.STOVE:
            self.request_replan("stove pause started")
        log.info("%s pause started, planned until %s", kind, planned_end)

    def _close_active(self, now: datetime, reason: str) -> None:
        active = self.state.active
        assert active is not None and active.id is not None
        db.close_pause_event(self._conn, active.id, now, reason)
        self.state.active = None
        self.request_replan(f"{active.kind} pause ended")
        log.info("%s pause ended: %s", active.kind, reason)

    # --- the relay ----------------------------------------------------------

    def _drive_relay(self, now: datetime, paused: bool) -> bool | None:
        """Write the state, read it back, count consecutive disagreements; returns the read-back."""
        state = self.state
        failed = False
        try:
            self._relay.set(paused)
        except (OSError, RelayMismatch) as error:
            log.error("Relay write failed: %s", error)
            failed = True
        try:
            actual: bool | None = self._relay.read()
        except OSError as error:
            log.error("Relay read-back failed: %s", error)
            actual = None
        state.readback_mismatches = (
            state.readback_mismatches + 1 if failed or actual != paused else 0
        )
        if actual is True:
            state.paused_since = state.paused_since or now
        elif actual is False:
            state.paused_since = None
        return actual

    def _reinitialise_relay(self) -> None:
        try:
            self._relay.reinitialise()
        except OSError as error:
            log.error("Could not re-initialise the relay: %s", error)

    # --- planning -----------------------------------------------------------

    def _replan_reason(self, now: datetime, burning: bool) -> str | None:
        state = self.state
        if state.pending_replan:
            reason, state.pending_replan = state.pending_replan, None
            return reason
        if burning != state.burning:
            return "stove started" if burning else "stove stopped"
        if db.latest_price_slot(self._conn) != state.planned_price_slot:
            return "new prices"
        settings = self._settings.price_pause
        hours = (
            settings.replan_interval_while_burning_hours
            if burning
            else settings.replan_interval_hours
        )
        if state.planned_at is None or now - state.planned_at >= timedelta(hours=hours):
            return "periodic"
        return None

    def _replan(self, now: datetime, reason: str) -> None:
        """Run the planner on stored prices and store the plan; on failure there are no pauses."""
        first_slot = planner.floor_to_quarter(now)
        found = db.prices_for_range(self._conn, first_slot, first_slot + planner.LOOKAHEAD)
        history = db.pause_events_since(self._conn, now - budget.WINDOW)
        stove_pause = self._latest_stove_pause(history)
        try:
            plan = planner.plan_pauses(
                found.quarters,
                history,
                now,
                self._settings.price_pause,
                stove_pause,
                self._settings.stove,
            )
        except ValueError:
            log.exception("The planner failed; planning no price pauses")
            plan = Plan(now, "planner-error", first_slot, first_slot)
        self.state.plan = replace(plan, id=db.insert_plan(self._conn, plan))
        self.state.planned_at = now
        self.state.planned_price_slot = db.latest_price_slot(self._conn)
        log.info(
            "Planned (%s): %s, %d price pauses, expected saving %.2f SEK",
            reason,
            plan.reason,
            len(plan.pauses),
            plan.expected_saving_sek,
        )

    @staticmethod
    def _latest_stove_pause(history: Sequence[PauseEvent]) -> Pause | None:
        """The latest stove pause, running or finished; the planner blocks it and its recovery."""
        stove_events = [e for e in history if e.kind is PauseKind.STOVE]
        if not stove_events:
            return None
        latest = max(stove_events, key=lambda e: e.started_at)
        return Pause(PauseKind.STOVE, latest.started_at, latest.ended_at or latest.planned_end)

    # --- inputs for the log -------------------------------------------------

    def _price_now(self, now: datetime) -> float | None:
        slot = planner.floor_to_quarter(now)
        quarters = db.prices_for_range(self._conn, slot, slot + planner.SLOT).quarters
        return quarters[0].energy if quarters else None

    def _outdoor_temperature(self, now: datetime) -> float | None:
        reading = db.latest_outdoor_reading(self._conn)
        return None if is_stale(reading, now, self._settings.weather) else reading.temperature

    def _heating_temperature(self, now: datetime) -> float | None:
        reading = db.latest_heating_reading(self._conn)
        if reading is None or now - reading.ts > HEATING_MAX_AGE:
            return None
        return reading.temperature

    def _standing_alert(self) -> GuardReason | None:
        """The repeated read-back mismatch is an alert even when no pause is being asked for."""
        if self.state.readback_mismatches >= 2:
            return GuardReason.READBACK_MISMATCH_REPEATED
        return None

    def _report_changes(self, decision: ControlDecision, reason: GuardReason | None) -> None:
        """Log mode changes, and every new guard reason (an alert at error level)."""
        state = self.state
        if decision.mode is not state.last_mode:
            log.info("Mode %s -> %s", state.last_mode, decision.mode)
        if reason is not None and reason is not state.last_guard_reason:
            level = logging.ERROR if reason.is_alert else logging.WARNING
            log.log(level, "Guard released the relay: %s", reason)
        state.last_mode = decision.mode
        state.last_guard_reason = reason


async def run_ticks(
    controller: Controller,
    tick_seconds: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Tick forever; a failing tick is logged (the controller has released the relay)."""
    while True:
        try:
            controller.tick(clock())
        except Exception:
            log.exception("Tick failed; the relay was released")
        await sleep(tick_seconds)


def _release(relay: Relay) -> None:
    """Open the relay, logging instead of raising; used where the relay must end up released."""
    try:
        relay.release()
    except (OSError, RelayMismatch) as error:
        log.error("Could not release the relay: %s", error)


async def run_service(settings: Settings, stop: asyncio.Event | None = None) -> None:
    """The service of plan §8: set everything up, run the tasks, and always release the relay.

    Runs until ``stop`` is set (SIGINT and SIGTERM set it) or a task fails.

    :raises ServiceAlreadyRunning: if another instance holds the lock.
    :raises MissingSecretError: if the Tibber token or MQTT credentials are not in the environment.
    :raises OSError: if the I2C bus or the database cannot be opened.
    """
    stop = stop or asyncio.Event()
    token = tibber_token(settings)
    credentials = mqtt_credentials(settings)
    relay = create_relay(settings.relay)
    settings.control.lock_file.parent.mkdir(parents=True, exist_ok=True)
    # The lock comes first: a second instance must not touch the first one's relay.
    with ServiceLock(settings.control.lock_file):
        try:
            _release(relay)
            with closing(db.connect(settings.database.path)) as conn:
                db.create_schema(conn)
                await _serve(settings, conn, relay, token, credentials, stop)
        finally:
            _release(relay)
            log.info("Service stopped; relay released")


async def _serve(
    settings: Settings,
    conn: sqlite3.Connection,
    relay: Relay,
    token: str,
    credentials: tuple[str, str] | None,
    stop: asyncio.Event,
) -> None:
    zone = ZoneInfo(settings.location.timezone)
    listener = StoveListener(settings.stove, lambda reading: db.insert_stove_reading(conn, reading))
    controller = Controller(settings, conn, relay, listener.monitor)
    try:
        async with asyncio.timeout(STARTUP_FETCH_TIMEOUT_SECONDS):
            await prices.fetch_if_due(conn, token, settings.tibber, zone)
    except TimeoutError:
        log.warning("Prices not fetched at startup; the scheduler keeps trying")
    controller.start(datetime.now(UTC))
    jobs = [
        run_ticks(controller, settings.control.tick_seconds),
        run_listener(listener, settings.mqtt, credentials),
        prices.run_scheduler(conn, token, settings.tibber, zone),
        run_poller(OpenMeteoProvider.from_settings(settings.location), conn, settings.weather),
    ]
    if settings.heating.enabled and (sensor := _open_heating_sensor(settings)):
        jobs.append(run_sampler(sensor, conn, settings.heating))
    tasks = [asyncio.create_task(job) for job in jobs]
    stopper = asyncio.create_task(stop.wait())
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)
    log.info("Controller running")
    try:
        done, _ = await asyncio.wait({stopper, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # a task that ended on its own failed: raise its error
    finally:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signal_number)
        for task in (stopper, *tasks):
            task.cancel()
        await asyncio.gather(stopper, *tasks, return_exceptions=True)
        controller.shutdown()


def _open_heating_sensor(settings: Settings) -> Mcp9600 | None:
    """The heating-circuit sensor, or ``None`` (logged): it is monitoring only, not essential."""
    try:
        return open_sensor(settings.heating)
    except (OSError, Mcp9600NotFoundError) as error:
        log.error("Heating-circuit sensor unavailable, not sampling: %s", error)
        return None
