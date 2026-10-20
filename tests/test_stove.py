import json
from datetime import UTC, datetime, timedelta

import pytest

from heatpump import stove
from heatpump.config import MqttSettings, StoveSettings
from heatpump.models import StoveReading, StoveState

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
SETTINGS = StoveSettings()  # burning at >= 50, cold at <= 40, stale after 300 s


def monitor_after(temperatures: list[float]) -> stove.StoveMonitor:
    """A monitor that has seen one reading per minute."""
    monitor = stove.StoveMonitor(SETTINGS)
    for i, temperature in enumerate(temperatures):
        monitor.update(temperature, T0 + MINUTE * i)
    return monitor


def state_sequence(temperatures: list[float]) -> list[StoveState]:
    monitor = stove.StoveMonitor(SETTINGS)
    states = []
    for i, temperature in enumerate(temperatures):
        monitor.update(temperature, T0 + MINUTE * i)
        states.append(monitor.state(T0 + MINUTE * i))
    return states


# --- hysteresis ------------------------------------------------------------


def test_no_reading_means_stale() -> None:
    assert stove.StoveMonitor(SETTINGS).state(T0) is StoveState.STALE


def test_a_cold_stove_stays_cold_in_the_band() -> None:
    assert set(state_sequence([20, 30, 45, 49.9])) == {StoveState.COLD}


def test_burning_starts_at_the_threshold() -> None:
    assert state_sequence([49.9, 50.0]) == [StoveState.COLD, StoveState.BURNING]


def test_burning_continues_through_the_band() -> None:
    states = state_sequence([55, 49, 45, 41, 40.1])
    assert set(states) == {StoveState.BURNING}


def test_burning_ends_at_the_cold_threshold() -> None:
    assert state_sequence([60, 41, 40.0]) == [
        StoveState.BURNING,
        StoveState.BURNING,
        StoveState.COLD,
    ]


def test_it_can_ignite_again_after_cooling() -> None:
    assert state_sequence([60, 35, 45, 52]) == [
        StoveState.BURNING,
        StoveState.COLD,
        StoveState.COLD,
        StoveState.BURNING,
    ]


def test_zero_hysteresis_switches_at_the_threshold() -> None:
    monitor = stove.StoveMonitor(StoveSettings(hysteresis_c=0))
    monitor.update(50, T0)
    assert monitor.state(T0) is StoveState.BURNING
    monitor.update(49.9, T0 + MINUTE)
    assert monitor.state(T0 + MINUTE) is StoveState.COLD


# --- staleness ---------------------------------------------------------------


def test_a_reading_goes_stale_after_the_configured_time() -> None:
    monitor = monitor_after([60])
    assert monitor.state(T0 + timedelta(seconds=300)) is StoveState.BURNING
    assert monitor.state(T0 + timedelta(seconds=301)) is StoveState.STALE


def test_availability_offline_is_stale_even_with_a_fresh_reading() -> None:
    monitor = monitor_after([60])
    monitor.set_available(False)
    assert monitor.state(T0) is StoveState.STALE
    monitor.set_available(True)
    monitor.update(60, T0 + MINUTE)  # burning has to be seen again
    assert monitor.state(T0 + MINUTE) is StoveState.BURNING


def test_the_availability_is_unknown_until_announced_and_counts_as_available() -> None:
    assert monitor_after([60]).state(T0) is StoveState.BURNING


def test_a_feed_that_comes_back_in_the_band_is_cold_not_burning() -> None:
    monitor = monitor_after([60])
    gap = T0 + timedelta(minutes=20)
    monitor.update(45, gap)  # the band: unknown after a gap, so no pause is requested
    assert monitor.state(gap) is StoveState.COLD


def test_a_feed_that_comes_back_hot_is_burning() -> None:
    monitor = monitor_after([60])
    gap = T0 + timedelta(minutes=20)
    monitor.update(70, gap)
    assert monitor.state(gap) is StoveState.BURNING


def test_going_offline_forgets_that_the_stove_was_burning() -> None:
    monitor = monitor_after([60])
    monitor.set_available(False)
    monitor.set_available(True)
    monitor.update(45, T0 + MINUTE)
    assert monitor.state(T0 + MINUTE) is StoveState.COLD


# --- ordering -------------------------------------------------------------------


def test_an_older_timestamp_is_ignored() -> None:
    monitor = stove.StoveMonitor(SETTINGS)
    assert monitor.update(60, T0 + MINUTE) is True
    assert monitor.update(20, T0) is False
    assert monitor.state(T0 + MINUTE) is StoveState.BURNING
    assert monitor.last_temperature == 60


def test_the_same_timestamp_twice_is_ignored() -> None:
    monitor = stove.StoveMonitor(SETTINGS)
    assert monitor.update(60, T0) is True
    assert monitor.update(20, T0) is False


def test_the_latest_reading_is_remembered() -> None:
    monitor = monitor_after([20, 30])
    assert (monitor.last_temperature, monitor.last_reading_at) == (30, T0 + MINUTE)


# --- payload -----------------------------------------------------------------------


def payload(**overrides: object) -> bytes:
    data = {
        "ts": "2026-10-21T12:00:00+00:00",
        "hot_c": 61.25,
        "cold_c": 24.5,
        "delta_c": 36.75,
        "fault": False,
        "status": 0,
    }
    return json.dumps({**data, **overrides}).encode()


def test_parse_payload_reads_the_hot_junction_and_timestamp() -> None:
    assert stove.parse_payload(payload()) == (T0, 61.25)


def test_parse_payload_accepts_a_zulu_timestamp() -> None:
    assert stove.parse_payload(payload(ts="2026-10-21T12:00:00Z")) == (T0, 61.25)


def test_a_naive_timestamp_is_rejected() -> None:
    with pytest.raises(stove.StovePayloadError, match="time zone"):
        stove.parse_payload(payload(ts="2026-10-21T12:00:00"))


def test_a_faulty_reading_is_rejected() -> None:
    with pytest.raises(stove.StovePayloadError, match="fault"):
        stove.parse_payload(payload(fault=True))


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"ts": "2026-10-21T12:00:00+00:00"}',
        payload(hot_c="hot"),
        payload(ts=5),
    ],
)
def test_malformed_payloads_are_rejected(raw: bytes) -> None:
    with pytest.raises(stove.StovePayloadError):
        stove.parse_payload(raw)


# --- listener -------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.readings: list[StoveReading] = []

    def __call__(self, reading: StoveReading) -> None:
        self.readings.append(reading)


def make_listener() -> tuple[stove.StoveListener, Recorder]:
    recorder = Recorder()
    return stove.StoveListener(SETTINGS, on_reading=recorder), recorder


def test_a_temperature_message_is_reported_with_its_state() -> None:
    listener, recorder = make_listener()
    listener.handle(SETTINGS.temperature_topic, payload(hot_c=61.0))
    assert recorder.readings == [StoveReading(T0, 61.0, StoveState.BURNING)]
    assert listener.monitor.state(T0) is StoveState.BURNING


def test_every_accepted_reading_is_reported() -> None:
    listener, recorder = make_listener()
    for i, temperature in enumerate([20, 30, 55]):
        ts = (T0 + MINUTE * i).isoformat()
        listener.handle(SETTINGS.temperature_topic, payload(ts=ts, hot_c=temperature))
    assert [r.state for r in recorder.readings] == [
        StoveState.COLD,
        StoveState.COLD,
        StoveState.BURNING,
    ]


def test_an_out_of_order_message_is_not_reported() -> None:
    listener, recorder = make_listener()
    listener.handle(SETTINGS.temperature_topic, payload(ts=(T0 + MINUTE).isoformat()))
    listener.handle(SETTINGS.temperature_topic, payload(ts=T0.isoformat()))
    assert len(recorder.readings) == 1


def test_a_bad_payload_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    listener, recorder = make_listener()
    listener.handle(SETTINGS.temperature_topic, b"garbage")
    assert recorder.readings == []
    assert "garbage" in caplog.text


def test_availability_messages_update_the_monitor() -> None:
    listener, _ = make_listener()
    listener.handle(SETTINGS.temperature_topic, payload())
    listener.handle(SETTINGS.availability_topic, b"offline")
    assert listener.monitor.state(T0) is StoveState.STALE
    listener.handle(SETTINGS.availability_topic, b"online")
    listener.handle(SETTINGS.temperature_topic, payload(ts=(T0 + MINUTE).isoformat()))
    assert listener.monitor.state(T0 + MINUTE) is StoveState.BURNING


def test_an_unknown_topic_is_ignored() -> None:
    listener, recorder = make_listener()
    listener.handle("other/topic", payload())
    assert recorder.readings == []


def test_an_unknown_availability_payload_counts_as_offline() -> None:
    listener, _ = make_listener()
    listener.handle(SETTINGS.temperature_topic, payload())
    listener.handle(SETTINGS.availability_topic, b"???")
    assert listener.monitor.state(T0) is StoveState.STALE


# --- the MQTT loop -----------------------------------------------------------------


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = type("Topic", (), {"value": topic})()
        self.payload = payload


class FakeClient:
    """Stands in for ``aiomqtt.Client``: delivers its messages, then raises ``MqttError``."""

    instances: list["FakeClient"] = []
    scripts: list[list[FakeMessage]] = []

    def __init__(self, hostname: str, port: int = 1883, **kwargs: object) -> None:
        self.hostname, self.port, self.kwargs = hostname, port, kwargs
        self.subscribed: list[str] = []
        self.script = self.scripts.pop(0) if self.scripts else []
        FakeClient.instances.append(self)

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        for message in self.script:
            yield message
        raise stove.aiomqtt.MqttError("connection lost")


async def test_the_loop_subscribes_delivers_and_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances, FakeClient.scripts = [], []
    FakeClient.scripts = [
        [FakeMessage(SETTINGS.temperature_topic, payload())],
        [FakeMessage(SETTINGS.temperature_topic, payload(ts="2026-10-21T12:01:00+00:00"))],
    ]
    monkeypatch.setattr(stove.aiomqtt, "Client", FakeClient)
    delays: list[float] = []
    received: list[StoveReading] = []

    class Stop(Exception):
        pass

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            raise Stop

    listener = stove.StoveListener(SETTINGS, on_reading=received.append)
    mqtt = MqttSettings(host="broker", port=1884)
    with pytest.raises(Stop):
        await stove.run_listener(listener, mqtt, None, sleep=fake_sleep)
    assert [r.temperature for r in received] == [61.25, 61.25]
    first = FakeClient.instances[0]
    assert (first.hostname, first.port) == ("broker", 1884)
    assert first.subscribed == [SETTINGS.temperature_topic, SETTINGS.availability_topic]
    assert len(FakeClient.instances) == 2
    assert delays == [stove.RECONNECT_FIRST_SECONDS, stove.RECONNECT_FIRST_SECONDS]


async def test_the_loop_passes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances, FakeClient.scripts = [], [[]]
    monkeypatch.setattr(stove.aiomqtt, "Client", FakeClient)

    async def stop(delay: float) -> None:
        raise RuntimeError("stop")

    listener = stove.StoveListener(SETTINGS, on_reading=lambda r: None)
    with pytest.raises(RuntimeError, match="stop"):
        await stove.run_listener(listener, MqttSettings(), ("user", "pw"), sleep=stop)
    assert FakeClient.instances[0].kwargs["username"] == "user"
    assert FakeClient.instances[0].kwargs["password"] == "pw"
