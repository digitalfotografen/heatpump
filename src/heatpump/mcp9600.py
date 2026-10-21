"""MCP9600 thermocouple converter, ported from mcp9600_logger (same chip, same HAT).

Only what the heating-circuit sensor needs: configure, read the hot and cold junction, and the
status flags. The register map is in that project's ``docs/mcp9600_reference.md``.
"""

from dataclasses import dataclass
from enum import IntEnum, IntFlag

from heatpump.i2c import I2CBus

KNOWN_DEVICE_IDS = frozenset({0x40, 0x41})  # MCP9600 family, MCP9601 family
FILTER_COEFFICIENT = 3  # smooths flame flicker and noise without noticeable lag at 60 s sampling


class Register(IntEnum):
    HOT_JUNCTION = 0x00
    COLD_JUNCTION = 0x02
    STATUS = 0x04
    SENSOR_CONFIG = 0x05
    DEVICE_CONFIG = 0x06
    DEVICE_ID = 0x20


class StatusFlag(IntFlag):
    INPUT_RANGE = 0x10
    SHORT_CIRCUIT = 0x20  # MCP9601 only, always 0 on the MCP9600
    TH_UPDATED = 0x40


class ThermocoupleType(IntEnum):
    """The value is the code for Sensor Configuration bits 6-4."""

    K = 0b000
    J = 0b001
    T = 0b010
    N = 0b011
    S = 0b100
    E = 0b101
    B = 0b110
    R = 0b111


# Hot-junction ranges in °C; only types documented in the reference are checked.
TEMPERATURE_RANGES: dict[ThermocoupleType, tuple[float, float]] = {
    ThermocoupleType.K: (-200.0, 1372.0),
}


class Mcp9600NotFoundError(Exception):
    """No MCP9600 answered at the address, or the device ID is wrong."""


@dataclass(frozen=True)
class Reading:
    hot_c: float
    cold_c: float
    status: StatusFlag
    in_range: bool

    @property
    def fault(self) -> bool:
        """True if the hot-junction value should not be trusted (open or shorted thermocouple)."""
        return (
            StatusFlag.INPUT_RANGE in self.status
            or StatusFlag.SHORT_CIRCUIT in self.status
            or not self.in_range
        )


def decode_temperature(data: bytes) -> float:
    """Convert a 16-bit two's-complement temperature register (1/16 °C per LSB) to °C."""
    return int.from_bytes(data[:2], "big", signed=True) / 16


class Mcp9600:
    def __init__(
        self,
        bus: I2CBus,
        address: int,
        thermocouple_type: ThermocoupleType = ThermocoupleType.K,
    ) -> None:
        self.bus = bus
        self.address = address
        self.thermocouple_type = thermocouple_type

    def verify(self) -> None:
        """Check that an MCP960x answers at the address.

        :raises Mcp9600NotFoundError: on a bus error or an unexpected device ID.
        """
        try:
            device_id = self._read(Register.DEVICE_ID, 2)[0]
        except OSError as error:
            raise Mcp9600NotFoundError(
                f"No response from I2C address {self.address:#04x}: {error}"
            ) from error
        if device_id not in KNOWN_DEVICE_IDS:
            raise Mcp9600NotFoundError(
                f"Unexpected device ID {device_id:#04x} at I2C address {self.address:#04x}"
            )

    def configure(self) -> None:
        """Set the thermocouple type and filter; 18-bit resolution, normal mode."""
        sensor_config = self.thermocouple_type << 4 | FILTER_COEFFICIENT
        self.bus.write_register(self.address, Register.SENSOR_CONFIG, bytes([sensor_config]))
        self.bus.write_register(self.address, Register.DEVICE_CONFIG, b"\x00")

    def read(self) -> Reading:
        """Read the status and both junction temperatures, flagging values outside the range.

        :raises OSError: on a bus error.
        """
        with self.bus.transaction():
            status = StatusFlag(self._read(Register.STATUS, 1)[0] & sum(StatusFlag))
            hot_c = decode_temperature(self._read(Register.HOT_JUNCTION, 2))
            cold_c = decode_temperature(self._read(Register.COLD_JUNCTION, 2))
        low, high = TEMPERATURE_RANGES.get(self.thermocouple_type, (float("-inf"), float("inf")))
        return Reading(hot_c, cold_c, status, in_range=low <= hot_c <= high)

    def _read(self, register: Register, length: int) -> bytes:
        return self.bus.read_register(self.address, register, length)
