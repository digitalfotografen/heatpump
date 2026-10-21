"""MCP23017 16-bit I/O expander: one pin as an output, everything else left alone.

Register map (IOCON.BANK = 0, the power-on default). Port A and port B are two 8-bit ports;
the address of a port's register is the base below plus 0 (A) or 1 (B):

====== ===== =====================================================
base   name  meaning
====== ===== =====================================================
0x00   IODIR 1 = input (power-on default), 0 = output
0x12   GPIO  the level on the pins, as read
0x14   OLAT  the level the output latches drive
====== ===== =====================================================

Every change to a register is a read-modify-write of the one bit, so other pins keep whatever
they were doing.
"""

import re
from dataclasses import dataclass
from enum import IntEnum

from heatpump.i2c import I2CBus

PIN_PATTERN = re.compile(r"^GP([AB])([0-7])$")


class Register(IntEnum):
    IODIR = 0x00
    GPIO = 0x12
    OLAT = 0x14


@dataclass(frozen=True)
class Pin:
    port: int  # 0 = A, 1 = B
    bit: int

    @property
    def mask(self) -> int:
        return 1 << self.bit


def parse_pin(name: str) -> Pin:
    """Turn ``GPB0`` into a :class:`Pin`.

    :raises ValueError: if the name is not GPA0..GPA7 or GPB0..GPB7.
    """
    match = PIN_PATTERN.match(name)
    if match is None:
        raise ValueError(f"pin must be GPA0..GPA7 or GPB0..GPB7, got {name!r}")
    return Pin(port="AB".index(match.group(1)), bit=int(match.group(2)))


class Mcp23017:
    """One pin of an MCP23017 used as a digital output."""

    def __init__(self, bus: I2CBus, address: int, pin: str) -> None:
        self.bus = bus
        self.address = address
        self.pin = parse_pin(pin)

    def configure_output(self, initial_high: bool = False) -> None:
        """Make the pin an output; the latch is set first so the pin never glitches."""
        with self.bus.transaction():
            self._modify(Register.OLAT, set_bit=initial_high)
            self._modify(Register.IODIR, set_bit=False)

    def set_pin(self, high: bool) -> None:
        """Drive the output latch."""
        self._modify(Register.OLAT, set_bit=high)

    def read_pin(self) -> bool:
        """The level on the pin itself (not the latch), so a stuck or shorted pin shows."""
        return bool(self._read(Register.GPIO) & self.pin.mask)

    def is_output(self) -> bool:
        return not self._read(Register.IODIR) & self.pin.mask

    def _register(self, base: Register) -> int:
        return base + self.pin.port

    def _read(self, base: Register) -> int:
        return self.bus.read_register(self.address, self._register(base))[0]

    def _modify(self, base: Register, set_bit: bool) -> None:
        with self.bus.transaction():
            value = self._read(base)
            value = value | self.pin.mask if set_bit else value & ~self.pin.mask & 0xFF
            self.bus.write_register(self.address, self._register(base), bytes([value]))
