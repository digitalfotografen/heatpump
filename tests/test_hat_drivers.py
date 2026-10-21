import pytest
from fake_i2c import GPIO, IODIR, OLAT, FakeSMBus

from heatpump.i2c import I2CBus
from heatpump.mcp9600 import (
    Mcp9600,
    Mcp9600NotFoundError,
    Register,
    StatusFlag,
    ThermocoupleType,
    decode_temperature,
)
from heatpump.mcp23017 import Mcp23017, parse_pin
from heatpump.relay import DryRunRelay, Mcp23017Relay, RelayMismatch

EXPANDER = 0x22
THERMOCOUPLE = 0x66


def expander_bus() -> tuple[FakeSMBus, I2CBus]:
    smbus = FakeSMBus(mcp23017={EXPANDER})
    return smbus, I2CBus(smbus)


def test_scan_lists_the_devices_that_answer() -> None:
    bus = I2CBus(FakeSMBus(devices={0x22, 0x48, 0x66}))
    assert bus.scan() == [0x22, 0x48, 0x66]


def test_reading_a_missing_device_raises_oserror() -> None:
    with pytest.raises(OSError):
        I2CBus(FakeSMBus()).read_register(0x22, 0x00)


@pytest.mark.parametrize(
    ("name", "port", "bit"), [("GPA0", 0, 0), ("GPB0", 1, 0), ("GPB7", 1, 7), ("GPA3", 0, 3)]
)
def test_parse_pin(name: str, port: int, bit: int) -> None:
    pin = parse_pin(name)
    assert (pin.port, pin.bit) == (port, bit)


@pytest.mark.parametrize("name", ["GPC0", "GPB8", "B0", "gpb0"])
def test_parse_pin_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValueError, match="pin must be"):
        parse_pin(name)


def test_configure_output_changes_only_the_one_bit() -> None:
    smbus, bus = expander_bus()
    smbus.registers[EXPANDER, IODIR + 1] = b"\xff"
    smbus.registers[EXPANDER, OLAT + 1] = b"\x84"
    Mcp23017(bus, EXPANDER, "GPB0").configure_output()
    assert smbus.registers[EXPANDER, IODIR + 1] == b"\xfe"
    assert smbus.registers[EXPANDER, OLAT + 1] == b"\x84"
    assert smbus.registers[EXPANDER, IODIR] == b"\xff"  # port A untouched
    assert all(register in (IODIR + 1, OLAT + 1) for _, register, _ in smbus.writes)


def test_the_latch_is_set_before_the_pin_becomes_an_output() -> None:
    smbus, bus = expander_bus()
    Mcp23017(bus, EXPANDER, "GPB0").configure_output(initial_high=True)
    assert [register for _, register, _ in smbus.writes] == [OLAT + 1, IODIR + 1]
    assert smbus.registers[EXPANDER, GPIO + 1] == b"\x01"


def test_set_and_read_pin() -> None:
    smbus, bus = expander_bus()
    pin = Mcp23017(bus, EXPANDER, "GPB0")
    pin.configure_output()
    assert pin.is_output()
    pin.set_pin(True)
    assert pin.read_pin()
    assert smbus.registers[EXPANDER, OLAT + 1] == b"\x01"
    pin.set_pin(False)
    assert not pin.read_pin()


def test_set_pin_keeps_the_other_latch_bits() -> None:
    smbus, bus = expander_bus()
    smbus.registers[EXPANDER, OLAT + 1] = b"\x40"
    pin = Mcp23017(bus, EXPANDER, "GPB0")
    pin.configure_output()
    pin.set_pin(True)
    assert smbus.registers[EXPANDER, OLAT + 1] == b"\x41"
    pin.set_pin(False)
    assert smbus.registers[EXPANDER, OLAT + 1] == b"\x40"


def test_relay_writes_and_reads_back() -> None:
    _, bus = expander_bus()
    relay = Mcp23017Relay(Mcp23017(bus, EXPANDER, "GPB0"))
    relay.set(True)
    assert relay.read()
    relay.release()
    assert not relay.read()


def test_relay_read_does_not_touch_the_hardware() -> None:
    smbus, bus = expander_bus()
    Mcp23017Relay(Mcp23017(bus, EXPANDER, "GPB0")).read()
    assert smbus.writes == []


def test_relay_raises_when_the_pin_does_not_follow() -> None:
    smbus, bus = expander_bus()
    smbus.stuck = True
    relay = Mcp23017Relay(Mcp23017(bus, EXPANDER, "GPB0"))
    with pytest.raises(RelayMismatch):
        relay.set(True)


def test_reinitialising_configures_the_pin_again_on_the_next_set() -> None:
    smbus, bus = expander_bus()
    relay = Mcp23017Relay(Mcp23017(bus, EXPANDER, "GPB0"))
    relay.set(True)
    smbus.writes.clear()
    relay.reinitialise()
    assert smbus.writes == []  # nothing happens until the next write
    relay.set(False)
    assert any(register == IODIR + 1 for _, register, _ in smbus.writes)


def test_dry_run_relay_remembers_its_state() -> None:
    relay = DryRunRelay()
    assert not relay.read()
    relay.set(True)
    assert relay.read()
    relay.release()
    assert not relay.read()


# Values from mcp9600_logger's tests.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\x01\x90", 25.0),
        (b"\x00\x01", 0.0625),
        (b"\x00\x00", 0.0),
        (b"\xff\xf0", -1.0),
        (b"\xff\xff", -0.0625),
        (b"\xc7\xb0", -901.0),
        (b"\x55\xc0", 1372.0),
    ],
)
def test_decode_temperature(raw: bytes, expected: float) -> None:
    assert decode_temperature(raw) == expected


def thermocouple(
    hot: bytes = b"\x01\x90",
    cold: bytes = b"\x01\x80",
    status: int = 0x40,
    device_id: bytes = b"\x40\x10",
    kind: ThermocoupleType = ThermocoupleType.K,
) -> tuple[FakeSMBus, Mcp9600]:
    smbus = FakeSMBus(devices={THERMOCOUPLE})
    for register, value in {
        Register.HOT_JUNCTION: hot,
        Register.COLD_JUNCTION: cold,
        Register.STATUS: bytes([status]),
        Register.DEVICE_ID: device_id,
    }.items():
        smbus.registers[THERMOCOUPLE, register] = value
    return smbus, Mcp9600(I2CBus(smbus), THERMOCOUPLE, kind)


def test_read_returns_both_junctions_and_the_status() -> None:
    _, sensor = thermocouple()
    reading = sensor.read()
    assert (reading.hot_c, reading.cold_c) == (25.0, 24.0)
    assert reading.status == StatusFlag.TH_UPDATED
    assert not reading.fault


@pytest.mark.parametrize("status", [0x10, 0x20])
def test_status_faults_flag_the_reading(status: int) -> None:
    _, sensor = thermocouple(status=status)
    assert sensor.read().fault


@pytest.mark.parametrize(
    ("hot", "in_range"),
    [
        (b"\xf3\x80", True),  # -200 °C
        (b"\xf3\x70", False),  # -201 °C
        (b"\x55\xc0", True),  # 1372 °C
        (b"\x55\xd0", False),  # 1373 °C
    ],
)
def test_type_k_range_check(hot: bytes, in_range: bool) -> None:
    _, sensor = thermocouple(hot=hot)
    assert sensor.read().fault is not in_range


def test_range_check_is_skipped_for_types_without_a_known_range() -> None:
    _, sensor = thermocouple(hot=b"\xc7\xb0", kind=ThermocoupleType.J)  # -901 °C
    assert not sensor.read().fault


def test_configure_writes_type_and_filter() -> None:
    smbus, sensor = thermocouple(kind=ThermocoupleType.J)
    sensor.configure()
    assert smbus.writes == [
        (THERMOCOUPLE, Register.SENSOR_CONFIG, b"\x13"),
        (THERMOCOUPLE, Register.DEVICE_CONFIG, b"\x00"),
    ]


@pytest.mark.parametrize("device_id", [b"\x40\x10", b"\x41\x10"])
def test_verify_accepts_the_mcp960x_family(device_id: bytes) -> None:
    thermocouple(device_id=device_id)[1].verify()


def test_verify_rejects_another_device() -> None:
    with pytest.raises(Mcp9600NotFoundError, match="0x12"):
        thermocouple(device_id=b"\x12\x34")[1].verify()


def test_verify_reports_a_silent_bus() -> None:
    sensor = Mcp9600(I2CBus(FakeSMBus()), THERMOCOUPLE)
    with pytest.raises(Mcp9600NotFoundError, match="0x66"):
        sensor.verify()
