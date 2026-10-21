"""One shared I2C bus handle with a lock, so the relay and the thermocouple never interleave.

Only register access lives here; what the registers mean is up to the chip drivers.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from typing import Protocol, Self

FIRST_ADDRESS = 0x08  # 0x00-0x07 and 0x78-0x7F are reserved by the I2C specification
LAST_ADDRESS = 0x77


class SMBusLike(Protocol):
    """The part of :class:`smbus2.SMBus` that is used here; tests substitute a fake."""

    def read_i2c_block_data(self, i2c_addr: int, register: int, length: int) -> list[int]: ...

    def write_i2c_block_data(self, i2c_addr: int, register: int, data: list[int]) -> None: ...

    def read_byte(self, i2c_addr: int) -> int: ...

    def close(self) -> None: ...


class I2CBus:
    """Register reads and writes on one bus; every call and every :meth:`transaction` is locked."""

    def __init__(self, smbus: SMBusLike) -> None:
        self._smbus = smbus
        self._lock = threading.RLock()

    @classmethod
    def open(cls, number: int) -> Self:
        """Open ``/dev/i2c-<number>``.

        :raises OSError: if the bus device does not exist or cannot be opened.
        """
        import smbus2  # imported here so the fake bus in tests does not need a device

        return cls(smbus2.SMBus(number))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the bus across several calls, for example a read-modify-write of one register."""
        with self._lock:
            yield

    def read_register(self, address: int, register: int, length: int = 1) -> bytes:
        """:raises OSError: on a bus error or when no device answers."""
        with self._lock:
            return bytes(self._smbus.read_i2c_block_data(address, register, length))

    def write_register(self, address: int, register: int, data: bytes) -> None:
        """:raises OSError: on a bus error or when no device answers."""
        with self._lock:
            self._smbus.write_i2c_block_data(address, register, list(data))

    def probe(self, address: int) -> bool:
        """True if a device acknowledges a one-byte read at ``address``."""
        with self._lock:
            try:
                self._smbus.read_byte(address)
            except OSError:
                return False
            return True

    def scan(self) -> list[int]:
        """All addresses that acknowledge, in ascending order."""
        return [a for a in range(FIRST_ADDRESS, LAST_ADDRESS + 1) if self.probe(a)]

    def close(self) -> None:
        self._smbus.close()


@cache
def shared_bus(number: int) -> I2CBus:
    """The one handle for bus ``number``; the relay and the heating sampler both use it."""
    return I2CBus.open(number)
