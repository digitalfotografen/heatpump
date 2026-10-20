"""Outdoor temperature from Open-Meteo, stored in ``outdoor_readings``.

Only this module talks to the weather service. The planner and controller read the stored
readings. A failed or stale fetch is informational in this phase: nothing depends on it yet.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, Self

import httpx

from heatpump import db
from heatpump.config import LocationSettings, WeatherSettings
from heatpump.models import TemperatureReading

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE = "open-meteo"
REQUEST_TIMEOUT_SECONDS = 15


class WeatherError(Exception):
    """The weather service could not be reached or did not answer with a temperature."""


class WeatherProvider(Protocol):
    """Anything that can report the current outdoor temperature.

    Trafikverket, or a sensor of our own on MQTT, can replace Open-Meteo by implementing this.
    """

    async def fetch(self) -> TemperatureReading:
        """:raises WeatherError: if no usable temperature could be obtained."""
        ...


def parse_current(data: Any) -> TemperatureReading:
    """Turn an Open-Meteo ``current`` response (requested with ``timezone=UTC``) into a reading.

    :raises WeatherError: if the time or temperature is missing or malformed.
    """
    try:
        current = data["current"]
        ts = datetime.fromisoformat(current["time"]).replace(tzinfo=UTC)
        temperature = float(current["temperature_2m"])
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherError(f"unusable Open-Meteo response: {error!r}") from error
    return TemperatureReading(ts, temperature, SOURCE)


class OpenMeteoProvider:
    """Current temperature at a fixed position from Open-Meteo."""

    def __init__(
        self, latitude: float, longitude: float, client: httpx.AsyncClient | None = None
    ) -> None:
        """:param client: replaces the default HTTP client, for tests."""
        self.latitude = latitude
        self.longitude = longitude
        self._client = client

    @classmethod
    def from_settings(cls, location: LocationSettings) -> Self:
        return cls(location.latitude, location.longitude)

    async def fetch(self) -> TemperatureReading:
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current": "temperature_2m",
            "timezone": "UTC",
        }
        try:
            if self._client is not None:
                response = await self._client.get(OPEN_METEO_URL, params=params)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = await client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as error:
            raise WeatherError(f"Open-Meteo request failed: {error!r}") from error
        except ValueError as error:
            raise WeatherError(f"Open-Meteo did not answer with JSON: {error}") from error
        return parse_current(body)


async def fetch_and_store(
    conn: sqlite3.Connection, provider: WeatherProvider
) -> TemperatureReading:
    """Fetch the current temperature and store it.

    :raises WeatherError: if the fetch fails; nothing is stored then.
    """
    reading = await provider.fetch()
    db.insert_outdoor_reading(conn, reading)
    return reading


def is_stale(reading: TemperatureReading | None, now: datetime, settings: WeatherSettings) -> bool:
    """True if there is no reading or it is older than ``stale_after_minutes``."""
    if reading is None:
        return True
    return now - reading.ts > timedelta(minutes=settings.stale_after_minutes)


async def run_poller(
    provider: WeatherProvider,
    conn: sqlite3.Connection,
    settings: WeatherSettings,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Fetch and store every ``fetch_interval_minutes`` forever; a failed fetch is only logged."""
    while True:
        try:
            reading = await fetch_and_store(conn, provider)
            log.info("Outdoor temperature %.1f °C at %s", reading.temperature, reading.ts)
        except WeatherError as error:
            log.warning("Outdoor temperature not updated: %s", error)
        await sleep(settings.fetch_interval_minutes * 60)
