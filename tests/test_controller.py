import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heatpump import controller as controller_module
from heatpump import db
from heatpump.config import ControlSettings, DatabaseSettings, MqttSettings, Settings
from heatpump.controller import Controller, run_service, run_ticks
from heatpump.models import ControlMode, Pause, PauseKind, Quarter, TemperatureReading
from heatpump.relay import DryRunRelay, ServiceAlreadyRunning, ServiceLock, service_running
from heatpump.stove import StoveMonitor

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
QUARTER = timedelta(minutes=15)
BASE_PRICE = 1.0
PEAK_PRICE = 10.0
EVENING_PEAK = (T0 + timedelta(hours=2), T0 + timedelta(hours=5))  # 14:00-17:00
MORNING_PEAK = (T0 + timedelta(hours=18), T0 + timedelta(hours=21))  # 06:00-09:00 tomorrow


def price_quarters(peaks: tuple[tuple[datetime, datetime], ...]) -> list[Quarter]:
    """Two days of quarters from T0, flat except for the given expensive periods."""
    quarters = []
    for i in range(48 * 4):
        start = T0 + QUARTER * i
        expensive = any(begin <= start < end for begin, end in peaks)
        quarters.append(Quarter(start, PEAK_PRICE if expensive else BASE_PRICE))
    return quarters


class FlakyRelay(DryRunRelay):
    """A relay whose read-back can be made to disagree with what was written."""

    def __init__(self) -> None:
        super().__init__()
        self.stuck_at: bool | None = None
        self.reinitialised = 0

    def read(self) -> bool:
        return super().read() if self.stuck_at is None else self.stuck_at

    def reinitialise(self) -> None:
        self.reinitialised += 1


class Rig:
    """A controller on an in-memory database with a scripted clock, stove and prices."""

    def __init__(
        self,
        peaks: tuple[tuple[datetime, datetime], ...] = (EVENING_PEAK, MORNING_PEAK),
        conn: sqlite3.Connection | None = None,
        relay: DryRunRelay | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.conn = conn or db.connect(":memory:")
        db.create_schema(self.conn)
        if conn is None:
            db.upsert_prices(self.conn, price_quarters(peaks), fetched_at=T0)
        self.relay = relay or DryRunRelay()
        self.settings = settings or Settings()
        self.stove = StoveMonitor(self.settings.stove)
        self.controller = Controller(self.settings, self.conn, self.relay, self.stove)
        self.now = T0

    def start(self) -> None:
        self.controller.start(self.now)

    def stove_reads(self, temperature: float) -> None:
        self.stove.update(temperature, self.now)

    def tick(self, stove_temperature: float | None = 20.0):
        """Advance one minute, feed the stove (None = silent feed), and run a tick."""
        self.now += MINUTE
        if stove_temperature is not None:
            self.stove_reads(stove_temperature)
        return self.controller.tick(self.now)

    def run_until(self, moment: datetime, stove_temperature: float | None = 20.0):
        decision = None
        while self.now < moment:
            decision = self.tick(stove_temperature)
        return decision

    def events(self):
        return db.pause_events_since(self.conn, T0 - timedelta(days=1))


def rig(**kwargs: object) -> Rig:
    started = Rig(**kwargs)
    started.stove_reads(20.0)
    started.start()
    return started


def first_price_pause(r: Rig) -> Pause:
    """The first pause of the plan; it ends the expensive stretch, where catching up is cheap."""
    assert r.controller.plan is not None and r.controller.plan.pauses
    return r.controller.plan.pauses[0]


# --- price pauses ----------------------------------------------------------


def test_a_flat_day_runs_and_logs_every_tick() -> None:
    r = rig(peaks=())
    decisions = [r.tick() for _ in range(3)]
    assert [d.mode for d in decisions] == [ControlMode.RUNNING] * 3
    assert not r.relay.read()
    assert [d.ts for d in db.control_log_since(r.conn, T0)] == [d.ts for d in decisions]


def test_the_log_row_has_the_inputs_of_the_tick() -> None:
    r = rig(peaks=())
    db.insert_outdoor_reading(r.conn, TemperatureReading(T0, 3.5, "open-meteo"))
    decision = r.tick(25.0)
    assert decision.stove_temp == 25.0
    assert decision.price_now == BASE_PRICE
    assert decision.outdoor_temp == 3.5
    assert decision.relay_actual is False
    assert decision.plan_id is not None


def test_the_pump_is_paused_in_the_planned_peak_and_runs_after_it() -> None:
    r = rig()
    pause = first_price_pause(r)
    assert r.run_until(pause.start - 5 * MINUTE).mode is ControlMode.RUNNING
    during = r.run_until(pause.start + 45 * MINUTE)
    assert during.mode is ControlMode.PAUSED_PRICE
    assert during.relay_requested and during.relay_actual
    assert r.relay.read()
    (event,) = r.events()
    assert event.kind is PauseKind.PRICE and event.started_at == pause.start
    assert event.ended_at is None
    r.run_until(pause.end + 30 * MINUTE)
    assert not r.relay.read()
    (event,) = r.events()
    assert event.ended_at == pause.end and event.end_reason == "plan_ended"


def test_recovery_after_a_price_pause_is_half_its_length() -> None:
    r = rig()
    pause = first_price_pause(r)  # 3 h, so 90 min of running follow
    assert r.run_until(pause.end + 10 * MINUTE).mode is ControlMode.RECOVERY
    assert r.run_until(pause.end + 85 * MINUTE).mode is ControlMode.RECOVERY
    assert r.run_until(pause.end + 95 * MINUTE).mode is ControlMode.RUNNING


def test_a_price_pause_lasts_at_least_its_minimum_even_if_the_plan_drops_it() -> None:
    r = rig()
    pause = first_price_pause(r)
    r.run_until(pause.start + 2 * MINUTE)
    assert r.relay.read()
    db.upsert_prices(r.conn, price_quarters(()), fetched_at=r.now)  # the peak disappears
    r.controller.request_replan("test")
    r.tick()
    assert r.relay.read()  # only 3 minutes into the pause
    r.run_until(pause.start + 31 * MINUTE)
    assert not r.relay.read()


# --- stove pauses ----------------------------------------------------------


def test_a_burning_stove_pauses_and_a_cold_one_resumes_after_the_minimum() -> None:
    r = rig(peaks=())
    assert r.tick(80.0).mode is ControlMode.PAUSED_STOVE
    assert r.relay.read()
    assert r.run_until(T0 + 20 * MINUTE, 30.0).mode is ControlMode.PAUSED_STOVE  # min 30 min
    assert r.run_until(T0 + 40 * MINUTE, 30.0).mode is ControlMode.RECOVERY
    (event,) = r.events()
    assert event.kind is PauseKind.STOVE and event.end_reason == "stove_cold"


def test_the_stove_pause_stops_at_six_hours_and_recovery_follows() -> None:
    r = rig(peaks=())
    r.tick(80.0)  # the pause starts at 12:01
    assert r.run_until(T0 + timedelta(hours=6), 80.0).mode is ControlMode.PAUSED_STOVE
    released = r.tick(80.0)
    assert released.mode is ControlMode.RECOVERY and not r.relay.read()
    assert r.events()[0].end_reason == "max_length"
    # recovery is half the pause: 3 h; the stove still burns, so it pauses again afterwards
    assert r.run_until(T0 + timedelta(hours=9), 80.0).mode is ControlMode.RECOVERY
    assert r.run_until(T0 + timedelta(hours=9, minutes=5), 80.0).mode is ControlMode.PAUSED_STOVE


def test_stove_pauses_are_not_charged_to_the_price_budget_but_overrule_the_plan() -> None:
    r = rig()
    r.run_until(T0 + 30 * MINUTE)
    first_plan = r.controller.plan
    decision = r.tick(80.0)  # the stove is lit well before the evening peak
    assert decision.mode is ControlMode.PAUSED_STOVE
    r.tick(80.0)  # the next tick re-plans with the stove pause in place
    plan = r.controller.plan
    assert plan is not None and first_plan is not None and plan.id != first_plan.id
    assert all(p.start >= T0 + timedelta(hours=6) for p in plan.pauses)  # the evening peak is gone
    assert any(p.start >= MORNING_PEAK[0] for p in plan.pauses)  # budget moved to the next peak


def test_lighting_the_stove_during_a_price_pause_switches_without_a_gap() -> None:
    r = rig()
    r.run_until(first_price_pause(r).start + 10 * MINUTE)
    assert r.tick(80.0).mode is ControlMode.PAUSED_STOVE
    price, stove = r.events()
    assert price.end_reason == "stove_override"
    assert price.ended_at == stove.started_at
    assert r.relay.read()


def test_no_price_pause_directly_after_a_stove_pause() -> None:
    r = rig()
    pause = first_price_pause(r)
    r.run_until(pause.start - 20 * MINUTE)
    r.tick(80.0)
    r.run_until(pause.start + 20 * MINUTE, 30.0)  # cold again; the stove pause ended at +11 min
    assert r.controller.state.active is None
    assert r.tick(30.0).mode is ControlMode.RECOVERY


def test_a_stove_may_pause_during_the_recovery_after_a_price_pause() -> None:
    r = rig()
    pause = first_price_pause(r)
    assert r.run_until(pause.end + 10 * MINUTE).mode is ControlMode.RECOVERY
    assert r.tick(80.0).mode is ControlMode.PAUSED_STOVE
    assert r.relay.read()


def test_the_stove_replan_runs_hourly_while_burning() -> None:
    r = rig(peaks=())
    r.run_until(T0 + 3 * MINUTE, 80.0)
    plans_before = r.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    r.run_until(T0 + 70 * MINUTE, 80.0)
    plans_after = r.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    assert plans_after - plans_before == 1


def test_new_prices_trigger_a_replan() -> None:
    r = rig(peaks=())
    r.tick()
    plans = r.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    later = [Quarter(T0 + QUARTER * i, BASE_PRICE) for i in range(48 * 4, 48 * 4 + 8)]
    db.upsert_prices(r.conn, later, fetched_at=r.now)
    r.tick()
    r.tick()
    assert r.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == plans + 1


def test_without_stove_news_the_plan_is_renewed_every_six_hours() -> None:
    r = rig(peaks=())
    r.run_until(T0 + timedelta(hours=5, minutes=50))
    assert r.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    r.run_until(T0 + timedelta(hours=6, minutes=2))
    assert r.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 2


# --- the guard -------------------------------------------------------------


def test_a_stale_stove_feed_releases_a_stove_pause() -> None:
    r = rig(peaks=())
    r.tick(80.0)
    r.run_until(T0 + 10 * MINUTE, None)  # the feed goes silent
    forced = [d for d in db.control_log_since(r.conn, T0) if d.mode is ControlMode.FORCED_RUN]
    assert [d.guard_reason for d in forced] == ["stove_stale"]
    assert not r.relay.read()
    assert r.events()[0].end_reason == "guard:stove_stale"


def test_force_run_blocks_a_planned_pause_until_it_is_cleared() -> None:
    r = rig()
    r.controller.force_run = True
    decision = r.run_until(first_price_pause(r).start + 10 * MINUTE)
    assert decision.mode is ControlMode.FORCED_RUN and decision.guard_reason == "force_run"
    assert not r.relay.read() and r.events() == []
    r.controller.force_run = False
    assert r.tick().mode is ControlMode.PAUSED_PRICE


def test_a_missing_price_for_the_current_quarter_never_pauses() -> None:
    r = rig(peaks=())
    r.conn.execute("DELETE FROM prices")
    assert r.tick(80.0).mode is ControlMode.PAUSED_STOVE  # stove pauses need no price
    r.run_until(T0 + 10 * MINUTE, 80.0)
    assert r.relay.read()
    decision = r.tick(80.0)
    assert decision.price_now is None


def test_one_readback_mismatch_releases_reinitialises_and_retries() -> None:
    relay = FlakyRelay()
    r = rig(peaks=(), relay=relay)
    relay.stuck_at = False  # the pin refuses to go high
    first = r.tick(80.0)
    assert first.mode is ControlMode.PAUSED_STOVE and first.relay_actual is False
    relay.stuck_at = None  # and recovers
    second = r.tick(80.0)
    assert second.mode is ControlMode.FORCED_RUN
    assert second.guard_reason == "readback_mismatch"
    assert relay.reinitialised == 1
    assert not relay.read()
    assert r.events()[0].end_reason == "guard:readback_mismatch"


def test_two_mismatches_in_a_row_keep_the_relay_released_and_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = FlakyRelay()
    r = rig(peaks=(), relay=relay)
    relay.stuck_at = False
    r.tick(80.0)  # the pin does not go high
    relay.stuck_at = True  # and then sticks high, so the release reads back wrong too
    r.tick(80.0)
    with caplog.at_level(logging.ERROR):
        third = r.tick(80.0)
    assert third.guard_reason == "readback_mismatch_repeated"
    assert "readback_mismatch_repeated" in caplog.text


# --- restart ---------------------------------------------------------------


def test_startup_releases_the_relay() -> None:
    relay = DryRunRelay()
    relay.set(True)
    rig(peaks=(), relay=relay)
    assert not relay.read()


def test_a_restart_in_the_middle_of_a_pause_continues_it() -> None:
    first = rig()
    first.run_until(first_price_pause(first).start + 10 * MINUTE)
    (original,) = first.events()

    second = Rig(conn=first.conn, relay=DryRunRelay())
    second.now = first.now + 2 * MINUTE
    second.stove_reads(20.0)
    second.start()
    assert not second.relay.read()  # released at startup
    decision = second.tick()
    assert decision.mode is ControlMode.PAUSED_PRICE and second.relay.read()
    (resumed,) = second.events()
    assert resumed.id == original.id and resumed.started_at == original.started_at
    assert second.controller.state.paused_since == original.started_at


def test_after_a_long_outage_the_old_pause_is_closed_and_recovery_applies() -> None:
    first = rig()
    first.run_until(first_price_pause(first).start + 10 * MINUTE)
    last_tick = first.now

    second = Rig(conn=first.conn, relay=DryRunRelay())
    second.now = last_tick + 10 * MINUTE
    second.stove_reads(20.0)
    second.start()
    (event,) = second.events()
    assert event.ended_at == last_tick and event.end_reason == "restart_gap"
    assert second.tick().mode is ControlMode.RECOVERY  # 30 min minimum from the last tick


def test_shutdown_releases_the_relay_even_if_it_fails() -> None:
    relay = FlakyRelay()
    r = rig(peaks=(), relay=relay)
    r.tick(80.0)
    assert relay.read()
    r.controller.shutdown()
    assert not relay.read()


# --- the tick loop and the service ----------------------------------------


class SpyRelay(DryRunRelay):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def release(self) -> None:
        self.calls.append("release")
        super().release()


async def test_a_failing_tick_releases_the_relay_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = SpyRelay()
    r = rig(peaks=(), relay=relay)
    r.controller._price_now = lambda now: 1 / 0  # type: ignore[method-assign]
    sleeps = 0

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise StopAsyncIteration

    with pytest.raises(StopAsyncIteration):
        await run_ticks(r.controller, 60, sleep, clock=lambda: T0)
    assert sleeps == 2  # the first tick failed and the loop still slept and ticked again
    assert relay.calls.count("release") == 3  # startup, and one per failed tick
    assert "Tick failed" in caplog.text


async def forever(*_: object, **__: object) -> None:
    await asyncio.Event().wait()


def service_settings(tmp_path: Path) -> Settings:
    return Settings(
        mqtt=MqttSettings(username_env=None, password_env=None),
        database=DatabaseSettings(path=tmp_path / "heatpump.sqlite3"),
        control=ControlSettings(lock_file=tmp_path / "heatpump.lock", tick_seconds=1),
    )


@pytest.fixture
def service_parts(monkeypatch: pytest.MonkeyPatch) -> SpyRelay:
    """Replace everything that talks to the outside world; return the relay the service gets."""
    relay = SpyRelay()
    monkeypatch.setenv("TIBBER_TOKEN", "token")
    monkeypatch.setattr(controller_module, "create_relay", lambda _: relay)
    monkeypatch.setattr(controller_module, "run_listener", forever)
    monkeypatch.setattr(controller_module, "run_poller", forever)
    monkeypatch.setattr(controller_module.prices, "run_scheduler", forever)

    async def no_fetch(*_: object, **__: object) -> float:
        return 60

    monkeypatch.setattr(controller_module.prices, "fetch_if_due", no_fetch)
    return relay


async def test_the_service_starts_ticks_and_cleans_up_on_stop(
    tmp_path: Path, service_parts: SpyRelay
) -> None:
    settings = service_settings(tmp_path)
    stop = asyncio.Event()
    service = asyncio.create_task(run_service(settings, stop))
    while not service_running(settings.control.lock_file):
        await asyncio.sleep(0.01)
    await asyncio.sleep(1.2)  # long enough for the first tick at tick_seconds=1
    stop.set()
    await asyncio.wait_for(service, timeout=5)
    assert not service_running(settings.control.lock_file)
    assert service_parts.calls[0] == "release" and service_parts.calls[-1] == "release"
    with closing(db.connect(settings.database.path, read_only=True)) as conn:
        assert db.latest_plan(conn) is not None
        assert db.latest_control_log(conn) is not None


async def test_a_crashing_task_stops_the_service_with_the_relay_released(
    tmp_path: Path, service_parts: SpyRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("poller crashed")

    monkeypatch.setattr(controller_module, "run_poller", crash)
    settings = service_settings(tmp_path)
    with pytest.raises(RuntimeError, match="poller crashed"):
        await asyncio.wait_for(run_service(settings), timeout=5)
    assert not service_running(settings.control.lock_file)
    assert service_parts.calls[-1] == "release"


async def test_a_second_instance_is_refused(tmp_path: Path, service_parts: SpyRelay) -> None:
    settings = service_settings(tmp_path)
    with ServiceLock(settings.control.lock_file), pytest.raises(ServiceAlreadyRunning):
        await run_service(settings)
    assert service_parts.calls == []  # the running instance's relay is left alone
