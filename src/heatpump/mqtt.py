"""MQTT publishing and commands (plan §9, phase 10).

The controller and the heating sampler are synchronous; they hand messages to a
:class:`Publisher`, which queues them. :func:`run_publisher` is the one aiomqtt connection that
sends the queue and receives the command topics. If the broker is away, retained values are
sent again when it comes back, so the broker always ends up with the latest state.

The payload builders are pure functions of the controller's data.
"""

import asyncio
import json
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

import aiomqtt

from heatpump import db, planner, status
from heatpump.config import MqttSettings, Settings
from heatpump.models import ControlDecision, Plan, Quarter, TemperatureReading

log = logging.getLogger(__name__)

AVAILABILITY = "availability"
ONLINE = "online"
OFFLINE = "offline"
FORCE_RUN_COMMAND = "cmd/force_run"
REPLAN_COMMAND = "cmd/replan"
OUTBOX_SIZE = 500
RECONNECT_FIRST_SECONDS = 5
RECONNECT_MAX_SECONDS = 300
OFFLINE_PUBLISH_TIMEOUT_SECONDS = 2


class Message(NamedTuple):
    topic: str
    payload: str
    retain: bool


# --- payloads --------------------------------------------------------------


def _json(data: object) -> str:
    return json.dumps(data, separators=(",", ":"))


def relay_text(decision: ControlDecision) -> str:
    """``paused`` or ``running``: what the relay was read back as, else what was requested."""
    paused = decision.relay_requested if decision.relay_actual is None else decision.relay_actual
    return "paused" if paused else "running"


def state_payload(decision: ControlDecision, since: datetime) -> str:
    """The ``state`` topic: the tick's mode, relay and inputs; ``since`` is when the mode began."""
    return _json(
        {
            "ts": db.to_iso(decision.ts),
            "mode": decision.mode.value,
            "relay": relay_text(decision),
            "since": db.to_iso(since),
            "guard_reason": decision.guard_reason,
            "stove_state": decision.stove_state.value if decision.stove_state else None,
            "stove_temp": decision.stove_temp,
            "outdoor_temp": decision.outdoor_temp,
        }
    )


def plan_payload(plan: Plan, zone: ZoneInfo) -> str:
    """The ``plan`` topic: every pause with UTC and local times."""
    return _json(
        {
            "created_at": db.to_iso(plan.created_at),
            "reason": plan.reason,
            "expected_saving_sek": plan.expected_saving_sek,
            "pauses": [
                {
                    "kind": pause.kind.value,
                    "start": db.to_iso(pause.start),
                    "end": db.to_iso(pause.end),
                    "start_local": pause.start.astimezone(zone).isoformat(),
                    "end_local": pause.end.astimezone(zone).isoformat(),
                    "minutes": round(pause.minutes),
                }
                for pause in plan.pauses
            ],
        }
    )


def quarter_payload(quarter: Quarter) -> str:
    """The ``price/current`` topic."""
    return _json(
        {"slot_start": db.to_iso(quarter.start), "energy": quarter.energy, "total": quarter.total}
    )


def quarters_payload(quarters: Sequence[Quarter]) -> str:
    """The ``price/today`` and ``price/tomorrow`` topics."""
    return _json(
        [
            {"start": db.to_iso(q.start), "energy": q.energy, "tax": q.tax, "total": q.total}
            for q in quarters
        ]
    )


def heating_payload(reading: TemperatureReading) -> str:
    """The ``heating/temperature`` topic; ``cold_c`` is the cold junction."""
    return _json(
        {"ts": db.to_iso(reading.ts), "temperature": reading.temperature, "cold_c": reading.detail}
    )


def alert_payload(reason: str, ts: datetime) -> str:
    return _json({"reason": reason, "ts": db.to_iso(ts)})


# --- the publisher ---------------------------------------------------------


class Publisher:
    """Collects messages for :func:`run_publisher`; usable without a broker.

    Retained messages are also remembered per topic, so a reconnect can send the latest value of
    each. When more than ``outbox_size`` messages wait for a broker that is away, the oldest go.
    """

    def __init__(self, prefix: str, outbox_size: int = OUTBOX_SIZE) -> None:
        self.prefix = prefix
        self._outbox: deque[Message] = deque(maxlen=outbox_size)
        self._wakeup = asyncio.Event()
        self._retained: dict[str, Message] = {}

    def topic(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    def publish(self, suffix: str, payload: str, retain: bool = True) -> None:
        """Queue a message; an empty retained payload clears the topic on the broker."""
        message = Message(self.topic(suffix), payload, retain)
        if retain:
            if payload:
                self._retained[message.topic] = message
            else:
                self._retained.pop(message.topic, None)
        self._outbox.append(message)
        self._wakeup.set()

    def alert(self, reason: str, ts: datetime) -> None:
        """Queue an alert; alerts are events, so they are not retained."""
        self.publish("alert", alert_payload(reason, ts), retain=False)

    def retained_snapshot(self) -> list[Message]:
        return list(self._retained.values())

    def drain(self) -> Iterator[Message]:
        """Take the queued messages in order."""
        while self._outbox:
            yield self._outbox.popleft()

    async def wait_for_messages(self) -> None:
        """Return once something is queued."""
        while not self._outbox:
            self._wakeup.clear()
            await self._wakeup.wait()


# --- commands --------------------------------------------------------------


@dataclass(frozen=True)
class CommandHandler:
    """Turns messages on the ``cmd/`` topics into calls on the controller."""

    prefix: str
    set_force_run: Callable[[bool], None]
    replan: Callable[[], None]

    @property
    def topics(self) -> list[str]:
        return [f"{self.prefix}/{FORCE_RUN_COMMAND}", f"{self.prefix}/{REPLAN_COMMAND}"]

    def handle(self, topic: str, payload: bytes | str) -> None:
        """Process one message; unusable ones are logged and skipped."""
        text = payload.decode(errors="replace") if isinstance(payload, bytes) else payload
        if topic == f"{self.prefix}/{FORCE_RUN_COMMAND}":
            match text.strip().lower():
                case "on":
                    self.set_force_run(True)
                case "off":
                    self.set_force_run(False)
                case _:
                    log.warning("Ignoring force_run payload %r (expected on or off)", text)
        elif topic == f"{self.prefix}/{REPLAN_COMMAND}":
            self.replan()


# --- the broker connection -------------------------------------------------


def _client(mqtt: MqttSettings, credentials: tuple[str, str] | None) -> aiomqtt.Client:
    username, password = credentials or (None, None)
    will = aiomqtt.Will(f"{mqtt.topic_prefix}/{AVAILABILITY}", OFFLINE, retain=True)
    return aiomqtt.Client(mqtt.host, mqtt.port, username=username, password=password, will=will)


async def run_publisher(
    publisher: Publisher,
    mqtt: MqttSettings,
    credentials: tuple[str, str] | None,
    commands: CommandHandler,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Keep one broker connection: send the outbox, receive commands; reconnect with backoff.

    On every connection it announces ``online`` and sends the latest retained values first. On
    cancellation it publishes ``offline`` before disconnecting, since a clean disconnect does not
    trigger the last will.
    """
    availability = publisher.topic(AVAILABILITY)
    delay = RECONNECT_FIRST_SECONDS
    while True:
        try:
            async with _client(mqtt, credentials) as client:
                try:
                    for topic in commands.topics:
                        await client.subscribe(topic)
                    await client.publish(availability, ONLINE, retain=True)
                    for message in publisher.retained_snapshot():
                        await client.publish(message.topic, message.payload, retain=True)
                    delay = RECONNECT_FIRST_SECONDS
                    await _serve_connection(client, publisher, commands)
                finally:
                    await _announce_offline(client, availability)
        except aiomqtt.MqttError as error:
            log.warning("MQTT connection to %s:%s lost: %s", mqtt.host, mqtt.port, error)
        await sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)


async def _announce_offline(client: aiomqtt.Client, availability: str) -> None:
    """Best effort: if the connection is already gone, the last will does the job."""
    try:
        async with asyncio.timeout(OFFLINE_PUBLISH_TIMEOUT_SECONDS):
            await client.publish(availability, OFFLINE, retain=True)
    except (aiomqtt.MqttError, TimeoutError):
        pass


async def _serve_connection(
    client: aiomqtt.Client, publisher: Publisher, commands: CommandHandler
) -> None:
    """Send and receive until one side fails; its error is raised."""
    tasks = [
        asyncio.create_task(_send_outbox(client, publisher)),
        asyncio.create_task(_receive_commands(client, commands)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _send_outbox(client: aiomqtt.Client, publisher: Publisher) -> None:
    while True:
        await publisher.wait_for_messages()
        for message in publisher.drain():
            await client.publish(message.topic, message.payload, retain=message.retain)


async def _receive_commands(client: aiomqtt.Client, commands: CommandHandler) -> None:
    async for message in client.messages:
        commands.handle(message.topic.value, message.payload)


def snapshot_messages(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[Message]:
    """The retained state topics as the database last knew them; empty if nothing was ticked."""
    ticks = db.control_log_since(conn, now - status.HISTORY)
    if not ticks:
        return []
    publisher = Publisher(settings.mqtt.topic_prefix)
    publisher.publish("state", state_payload(ticks[-1], status.mode_since(ticks)))
    publisher.publish("relay", relay_text(ticks[-1]))
    if plan := db.latest_plan(conn):
        publisher.publish("plan", plan_payload(plan, ZoneInfo(settings.location.timezone)))
    slot = planner.floor_to_quarter(now)
    if quarters := db.prices_for_range(conn, slot, slot + planner.SLOT).quarters:
        publisher.publish("price/current", quarter_payload(quarters[0]))
    return list(publisher.drain())


async def publish_once(
    mqtt: MqttSettings, credentials: tuple[str, str] | None, messages: Sequence[Message]
) -> None:
    """Connect, publish ``messages`` and disconnect (``heatpump mqtt publish-state``).

    :raises aiomqtt.MqttError: if the broker cannot be reached.
    """
    username, password = credentials or (None, None)
    async with aiomqtt.Client(mqtt.host, mqtt.port, username=username, password=password) as client:
        for message in messages:
            await client.publish(message.topic, message.payload, retain=message.retain)
