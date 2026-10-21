import logging
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_i2c import FakeSMBus

from heatpump import db
from heatpump.config import HeatingSettings
from heatpump.heating import run_sampler, sample
from heatpump.i2c import I2CBus
from heatpump.mcp9600 import Mcp9600, Register
from heatpump.relay import ServiceAlreadyRunning, ServiceLock, service_running

ADDRESS = 0x66
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def sensor(hot: bytes = b"\x01\x90", cold: bytes = b"\x01\x80", status: int = 0x40) -> Mcp9600:
    smbus = FakeSMBus(devices={ADDRESS})
    smbus.registers[ADDRESS, Register.HOT_JUNCTION] = hot
    smbus.registers[ADDRESS, Register.COLD_JUNCTION] = cold
    smbus.registers[ADDRESS, Register.STATUS] = bytes([status])
    return Mcp9600(I2CBus(smbus), ADDRESS)


def test_a_good_sample_carries_the_cold_junction_as_detail() -> None:
    reading = sample(sensor(), NOW)
    assert reading is not None
    assert (reading.ts, reading.temperature, reading.detail) == (NOW, 25.0, 24.0)


def test_an_open_circuit_is_logged_and_not_a_temperature(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert sample(sensor(status=0x10), NOW) is None
    assert "fault" in caplog.text


def test_a_bus_error_is_logged_and_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    broken = Mcp9600(I2CBus(FakeSMBus()), ADDRESS)
    with caplog.at_level(logging.WARNING):
        assert sample(broken, NOW) is None
    assert "not read" in caplog.text


async def test_the_sampler_stores_valid_readings_only(tmp_path: Path) -> None:
    smbus = FakeSMBus(devices={ADDRESS})
    for register, value in {
        Register.HOT_JUNCTION: b"\x01\x90",
        Register.COLD_JUNCTION: b"\x01\x80",
        Register.STATUS: b"\x40",
    }.items():
        smbus.registers[ADDRESS, register] = value
    thermocouple = Mcp9600(I2CBus(smbus), ADDRESS)
    sleeps = 0

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:  # the second round reads an open circuit
            smbus.registers[ADDRESS, Register.STATUS] = b"\x10"
        if sleeps == 2:
            raise StopAsyncIteration

    reported = []
    with closing(db.connect(tmp_path / "heatpump.sqlite3")) as conn:
        db.create_schema(conn)
        with pytest.raises(StopAsyncIteration):
            await run_sampler(
                thermocouple, conn, HeatingSettings(), sleep, on_sample=reported.append
            )
        assert conn.execute("SELECT COUNT(*) FROM heating_readings").fetchone()[0] == 1
        latest = db.latest_heating_reading(conn)
    assert latest is not None
    assert (latest.temperature, latest.detail) == (25.0, 24.0)
    assert [(r.temperature, r.detail) for r in reported] == [(25.0, 24.0)]  # not the faulty one


def test_no_lock_file_means_no_service(tmp_path: Path) -> None:
    assert not service_running(tmp_path / "heatpump.lock")


def test_a_held_lock_means_the_service_runs(tmp_path: Path) -> None:
    path = tmp_path / "heatpump.lock"
    with ServiceLock(path):
        assert service_running(path)
    assert not service_running(path)


def test_a_leftover_lock_file_is_not_a_running_service(tmp_path: Path) -> None:
    path = tmp_path / "heatpump.lock"
    path.touch()
    assert not service_running(path)


def test_the_service_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "heatpump.lock"
    with ServiceLock(path), pytest.raises(ServiceAlreadyRunning):
        ServiceLock(path).acquire()
