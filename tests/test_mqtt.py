import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from heatpump import mqtt
from heatpump.config import MqttSettings
from heatpump.models import (
    ControlDecision,
    ControlMode,
    Pause,
    PauseKind,
    Plan,
    Quarter,
    StoveState,
    TemperatureReading,
)
from heatpump.mqtt import CommandHandler, Message, Publisher

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Stockholm")


def decision(**kwargs: object) -> ControlDecision:
    fields = dict(
        ts=NOW,
        mode=ControlMode.PAUSED_STOVE,
        relay_requested=True,
        relay_actual=True,
        guard_reason=None,
        stove_state=StoveState.BURNING,
        stove_temp=82.5,
        outdoor_temp=3.5,
    )
    return ControlDecision(**(fields | kwargs))  # type: ignore[arg-type]


def drain(publisher: Publisher) -> list[Message]:
    return list(publisher.drain())


# --- payloads --------------------------------------------------------------


def test_state_payload_has_the_documented_fields() -> None:
    since = NOW - timedelta(minutes=20)
    payload = json.loads(mqtt.state_payload(decision(), since))
    assert payload == {
        "ts": "2026-10-21T12:00:00+00:00",
        "mode": "paused_stove",
        "relay": "paused",
        "since": "2026-10-21T11:40:00+00:00",
        "guard_reason": None,
        "stove_state": "burning",
        "stove_temp": 82.5,
        "outdoor_temp": 3.5,
    }


def test_relay_text_follows_the_read_back_and_falls_back_to_the_request() -> None:
    assert mqtt.relay_text(decision(relay_actual=False, relay_requested=True)) == "running"
    assert mqtt.relay_text(decision(relay_actual=None, relay_requested=True)) == "paused"
    assert mqtt.relay_text(decision(relay_actual=None, relay_requested=False)) == "running"


def test_plan_payload_has_utc_and_local_times() -> None:
    start = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    plan = Plan(
        NOW,
        "ok",
        NOW,
        NOW + timedelta(hours=24),
        expected_saving_sek=2.5,
        pauses=(Pause(PauseKind.PRICE, start, start + timedelta(hours=2)),),
    )
    payload = json.loads(mqtt.plan_payload(plan, ZONE))
    assert payload["created_at"] == "2026-10-21T12:00:00+00:00"
    assert payload["expected_saving_sek"] == 2.5
    (pause,) = payload["pauses"]
    assert pause == {
        "kind": "price",
        "start": "2026-10-21T15:00:00+00:00",
        "end": "2026-10-21T17:00:00+00:00",
        "start_local": "2026-10-21T17:00:00+02:00",
        "end_local": "2026-10-21T19:00:00+02:00",
        "minutes": 120,
    }


def test_price_payloads() -> None:
    quarter = Quarter(NOW, 1.25, tax=0.5, total=1.75)
    assert json.loads(mqtt.quarter_payload(quarter)) == {
        "slot_start": "2026-10-21T12:00:00+00:00",
        "energy": 1.25,
        "total": 1.75,
    }
    (entry,) = json.loads(mqtt.quarters_payload([quarter]))
    assert entry == {
        "start": "2026-10-21T12:00:00+00:00",
        "energy": 1.25,
        "tax": 0.5,
        "total": 1.75,
    }


def test_heating_payload() -> None:
    reading = TemperatureReading(NOW, 31.5, 24.0)
    assert json.loads(mqtt.heating_payload(reading)) == {
        "ts": "2026-10-21T12:00:00+00:00",
        "temperature": 31.5,
        "cold_c": 24.0,
    }


def test_alert_payload() -> None:
    assert json.loads(mqtt.alert_payload("no_price", NOW)) == {
        "reason": "no_price",
        "ts": "2026-10-21T12:00:00+00:00",
    }


# --- the publisher -----------------------------------------------------------


def test_topics_get_the_prefix_and_the_retain_flag() -> None:
    publisher = Publisher("heatpump")
    publisher.publish("relay", "paused")
    publisher.alert("no_price", NOW)
    relay, alert = drain(publisher)
    assert relay == Message("heatpump/relay", "paused", True)
    assert (alert.topic, alert.retain) == ("heatpump/alert", False)


def test_the_retained_snapshot_holds_the_latest_value_per_topic() -> None:
    publisher = Publisher("heatpump")
    publisher.publish("relay", "paused")
    publisher.publish("relay", "running")
    publisher.alert("no_price", NOW)
    assert publisher.retained_snapshot() == [Message("heatpump/relay", "running", True)]


def test_an_empty_retained_payload_clears_the_topic_in_the_snapshot() -> None:
    publisher = Publisher("heatpump")
    publisher.publish("price/current", "x")
    publisher.publish("price/current", "")
    assert publisher.retained_snapshot() == []
    assert drain(publisher)[-1] == Message("heatpump/price/current", "", True)


def test_a_full_outbox_drops_the_oldest_message() -> None:
    publisher = Publisher("heatpump", outbox_size=2)
    for text in ("a", "b", "c"):
        publisher.publish("state", text)
    assert [m.payload for m in drain(publisher)] == ["b", "c"]


# --- commands ----------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.force_run: list[bool] = []
        self.replans = 0

    def handler(self) -> CommandHandler:
        return CommandHandler("heatpump", self.force_run.append, lambda: self._replan())

    def _replan(self) -> None:
        self.replans += 1


@pytest.mark.parametrize(
    ("payload", "expected"), [("on", True), (b"ON", True), (" off ", False), (b"Off", False)]
)
def test_force_run_command(payload: str | bytes, expected: bool) -> None:
    recorder = Recorder()
    recorder.handler().handle("heatpump/cmd/force_run", payload)
    assert recorder.force_run == [expected]


def test_an_unusable_force_run_payload_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    recorder.handler().handle("heatpump/cmd/force_run", "maybe")
    assert recorder.force_run == []
    assert "maybe" in caplog.text


def test_replan_command_takes_any_payload() -> None:
    recorder = Recorder()
    recorder.handler().handle("heatpump/cmd/replan", b"")
    recorder.handler().handle("heatpump/cmd/replan", "now please")
    assert recorder.replans == 2


def test_other_topics_are_ignored() -> None:
    recorder = Recorder()
    recorder.handler().handle("heatpump/state", "on")
    assert recorder.force_run == [] and recorder.replans == 0


def test_the_command_topics() -> None:
    assert CommandHandler("x", print, print).topics == ["x/cmd/force_run", "x/cmd/replan"]


# --- the broker loop -----------------------------------------------------------


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = type("Topic", (), {"value": topic})()
        self.payload = payload


class FakeClient:
    """Stands in for ``aiomqtt.Client``: records what is published, delivers scripted commands."""

    instances: list["FakeClient"] = []
    scripts: list[list[FakeMessage]] = []

    def __init__(self, hostname: str, port: int = 1883, **kwargs: object) -> None:
        self.hostname, self.port, self.kwargs = hostname, port, kwargs
        self.subscribed: list[str] = []
        self.published: list[Message] = []
        self.script = self.scripts.pop(0) if self.scripts else []
        FakeClient.instances.append(self)

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append(Message(topic, payload, retain))

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        for message in self.script:
            yield message
        await asyncio.Event().wait()


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.instances, FakeClient.scripts = [], []
    monkeypatch.setattr(mqtt.aiomqtt, "Client", FakeClient)
    return FakeClient


async def test_the_loop_announces_online_sends_the_snapshot_then_the_outbox(
    fake_client: type[FakeClient],
) -> None:
    publisher = Publisher("heatpump")
    publisher.publish("relay", "running")
    task = asyncio.create_task(
        mqtt.run_publisher(
            publisher, MqttSettings(host="broker", port=1884), ("user", "pw"), Recorder().handler()
        )
    )
    await asyncio.sleep(0.05)
    publisher.publish("state", "{}")
    publisher.alert("no_price", NOW)
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    client = fake_client.instances[0]
    assert (client.hostname, client.port) == ("broker", 1884)
    assert client.kwargs["username"] == "user" and client.kwargs["password"] == "pw"
    will = client.kwargs["will"]
    assert (will.topic, will.payload, will.retain) == ("heatpump/availability", "offline", True)
    assert client.subscribed == ["heatpump/cmd/force_run", "heatpump/cmd/replan"]
    topics = [(m.topic, m.retain) for m in client.published]
    assert topics[0] == ("heatpump/availability", True)
    assert client.published[0].payload == "online"
    assert ("heatpump/relay", True) in topics
    assert ("heatpump/state", True) in topics
    assert ("heatpump/alert", False) in topics
    assert client.published[-1] == Message("heatpump/availability", "offline", True)


async def test_the_loop_hands_commands_to_the_handler(fake_client: type[FakeClient]) -> None:
    fake_client.scripts = [[FakeMessage("heatpump/cmd/force_run", b"on")]]
    recorder = Recorder()
    task = asyncio.create_task(
        mqtt.run_publisher(Publisher("heatpump"), MqttSettings(), None, recorder.handler())
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert recorder.force_run == [True]


async def test_the_loop_reconnects_and_resends_the_retained_values(
    fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenClient(FakeClient):
        async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
            raise mqtt.aiomqtt.MqttError("connection lost")

    clients: list[type[FakeClient]] = [BrokenClient, FakeClient]
    monkeypatch.setattr(mqtt.aiomqtt, "Client", lambda *a, **k: clients.pop(0)(*a, **k))
    publisher = Publisher("heatpump")
    publisher.publish("relay", "paused")
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    task = asyncio.create_task(
        mqtt.run_publisher(publisher, MqttSettings(), None, Recorder().handler(), sleep=fake_sleep)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert delays == [mqtt.RECONNECT_FIRST_SECONDS]
    resent = fake_client.instances[1].published
    assert Message("heatpump/relay", "paused", True) in resent


# --- one-shot publishing for `mqtt publish-state` ---------------------------------


async def test_publish_once_sends_the_messages_and_disconnects(
    fake_client: type[FakeClient],
) -> None:
    messages = [Message("heatpump/state", "{}", True), Message("heatpump/relay", "running", True)]
    await mqtt.publish_once(MqttSettings(host="broker"), None, messages)
    assert fake_client.instances[0].published == messages
