"""Stove monitor: the exhaust temperature from MQTT, with hysteresis and staleness.

The other Pi (mcp9600_logger) publishes JSON to ``stove/temperature`` once a minute,
``{"ts": <UTC ISO 8601>, "hot_c": ..., "cold_c": ..., "delta_c": ..., "fault": bool,
"status": int}``, and a retained ``online`` / ``offline`` to ``stove/availability``.

:class:`StoveMonitor` is a pure state machine. :class:`StoveListener` turns MQTT messages into
monitor updates, and :func:`run_listener` is the aiomqtt loop around it.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import aiomqtt

from heatpump.config import MqttSettings, StoveSettings
from heatpump.models import StoveReading, StoveState

log = logging.getLogger(__name__)

ONLINE = "online"
RECONNECT_FIRST_SECONDS = 5
RECONNECT_MAX_SECONDS = 300


class StovePayloadError(ValueError):
    """A temperature message that cannot be used as a stove reading."""


def parse_payload(raw: bytes | str) -> tuple[datetime, float]:
    """Read (timestamp, hot-junction temperature) from a temperature message.

    :raises StovePayloadError: if the message is not the expected JSON, has no time zone on its
        timestamp, or reports a sensor fault.
    """
    try:
        data = json.loads(raw)
        if data.get("fault"):
            raise StovePayloadError("sensor reports a fault")
        ts = datetime.fromisoformat(data["ts"])
        temperature = float(data["hot_c"])
    except StovePayloadError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise StovePayloadError(f"unusable stove payload {raw!r}: {error}") from error
    if ts.tzinfo is None:
        raise StovePayloadError(f"timestamp {data['ts']!r} has no time zone")
    return ts, temperature


class StoveMonitor:
    """Burning / cold / stale from the latest readings.

    Burning starts at ``burning_above_c`` and ends at ``cold_below_c``. Between the two the state
    stays as it was. After a gap in the feed, or when the feed reports offline, that memory is
    cleared: a stove seen only in the band is treated as cold, because "burning" is what pauses
    the pump.
    """

    def __init__(self, settings: StoveSettings) -> None:
        self._settings = settings
        self._burning = False
        self._available = True
        self.last_reading_at: datetime | None = None
        self.last_temperature: float | None = None

    def update(self, temperature: float, ts: datetime) -> bool:
        """Take a reading; return False if it is not newer than the last one and was ignored."""
        if self.last_reading_at is not None and ts <= self.last_reading_at:
            return False
        if self._feed_was_stale_at(ts):
            self._burning = False
        if temperature >= self._settings.burning_above_c:
            self._burning = True
        elif temperature <= self._settings.cold_below_c:
            self._burning = False
        self.last_reading_at = ts
        self.last_temperature = temperature
        return True

    def set_available(self, available: bool) -> None:
        """Record the availability topic; going offline clears the burning memory."""
        if not available:
            self._burning = False
        self._available = available

    def state(self, now: datetime) -> StoveState:
        """The stove state at ``now``; ``STALE`` when the feed cannot be trusted."""
        if not self._available or self._feed_was_stale_at(now):
            return StoveState.STALE
        return StoveState.BURNING if self._burning else StoveState.COLD

    def _feed_was_stale_at(self, moment: datetime) -> bool:
        if self.last_reading_at is None:
            return True
        return moment - self.last_reading_at > timedelta(seconds=self._settings.stale_after_seconds)


class StoveListener:
    """Feeds MQTT messages to a :class:`StoveMonitor` and reports every accepted reading."""

    def __init__(self, settings: StoveSettings, on_reading: Callable[[StoveReading], None]) -> None:
        self.settings = settings
        self._on_reading = on_reading
        self.monitor = StoveMonitor(settings)

    def handle(self, topic: str, payload: bytes | str) -> None:
        """Process one message; unusable messages are logged and skipped."""
        if topic == self.settings.temperature_topic:
            self._handle_temperature(payload)
        elif topic == self.settings.availability_topic:
            text = payload.decode(errors="replace") if isinstance(payload, bytes) else payload
            self.monitor.set_available(text.strip() == ONLINE)

    def _handle_temperature(self, payload: bytes | str) -> None:
        try:
            ts, temperature = parse_payload(payload)
        except StovePayloadError as error:
            log.warning("Ignoring stove message: %s", error)
            return
        if self.monitor.update(temperature, ts):
            self._on_reading(StoveReading(ts, temperature, self.monitor.state(ts)))


async def run_listener(
    listener: StoveListener,
    mqtt: MqttSettings,
    credentials: tuple[str, str] | None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Subscribe to the stove topics and feed the listener; reconnect forever with backoff."""
    settings = listener.settings
    username, password = credentials or (None, None)
    delay = RECONNECT_FIRST_SECONDS
    while True:
        try:
            async with aiomqtt.Client(
                mqtt.host, mqtt.port, username=username, password=password
            ) as client:
                await client.subscribe(settings.temperature_topic)
                await client.subscribe(settings.availability_topic)
                delay = RECONNECT_FIRST_SECONDS
                async for message in client.messages:
                    listener.handle(message.topic.value, message.payload)
        except aiomqtt.MqttError as error:
            log.warning("MQTT connection to %s:%s lost: %s", mqtt.host, mqtt.port, error)
        await sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)
