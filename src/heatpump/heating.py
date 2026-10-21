"""Heating-circuit temperature: the MCP9600 sampled once a minute into ``heating_readings``.

Monitoring only for now. Nothing in the planner or guard depends on it.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from heatpump import db
from heatpump.config import HeatingSettings
from heatpump.i2c import shared_bus
from heatpump.mcp9600 import Mcp9600, ThermocoupleType
from heatpump.models import TemperatureReading

log = logging.getLogger(__name__)


def open_sensor(settings: HeatingSettings) -> Mcp9600:
    """Connect to the MCP9600 on the HAT and configure it.

    :raises OSError: if the I2C bus cannot be opened.
    :raises Mcp9600NotFoundError: if no MCP9600 answers at the configured address.
    """
    sensor = Mcp9600(
        shared_bus(settings.i2c_bus),
        settings.i2c_address,
        ThermocoupleType[settings.thermocouple_type],
    )
    sensor.verify()
    sensor.configure()
    return sensor


def sample(sensor: Mcp9600, now: datetime | None = None) -> TemperatureReading | None:
    """One reading, or ``None`` (logged) if the bus failed or the thermocouple reads a fault.

    A faulty value, such as an open circuit, is never returned as a temperature.
    """
    try:
        reading = sensor.read()
    except OSError as error:
        log.warning("Heating-circuit sensor not read: %s", error)
        return None
    if reading.fault:
        log.warning(
            "Heating-circuit thermocouple fault (status %s, %.1f °C): open circuit?",
            reading.status,
            reading.hot_c,
        )
        return None
    return TemperatureReading(now or datetime.now(UTC), reading.hot_c, reading.cold_c)


async def run_sampler(
    sensor: Mcp9600,
    conn: sqlite3.Connection,
    settings: HeatingSettings,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_sample: Callable[[TemperatureReading], None] = lambda reading: None,
) -> None:
    """Store a reading every ``sample_interval_seconds`` forever and pass it to ``on_sample``."""
    while True:
        if (reading := sample(sensor)) is not None:
            db.insert_heating_reading(conn, reading)
            on_sample(reading)
            log.info("Heating circuit %.1f °C", reading.temperature)
        await sleep(settings.sample_interval_seconds)
