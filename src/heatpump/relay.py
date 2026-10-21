"""The EXT relay: a protocol, a dry-run stand-in and the MCP23017 implementation.

High = relay closed = EXT closed = heat pump paused. Only ``controller.py`` (through the guard)
and ``heatpump relay set`` may touch the relay; the latter checks :func:`service_running` first.
"""

import fcntl
import logging
from io import TextIOWrapper
from pathlib import Path
from typing import Protocol, Self

from heatpump.config import RelaySettings
from heatpump.i2c import shared_bus
from heatpump.mcp23017 import Mcp23017

log = logging.getLogger(__name__)


class RelayMismatch(Exception):
    """The pin read back a different level than the one just written."""


class ServiceAlreadyRunning(Exception):
    """Another process already holds the service lock."""


class Relay(Protocol):
    def set(self, paused: bool) -> None:
        """Request paused (relay closed) or running (relay open)."""
        ...

    def read(self) -> bool:
        """True if the relay is paused, as read back from the hardware."""
        ...

    def release(self) -> None:
        """Open the relay; what startup and shutdown call."""
        ...


class DryRunRelay:
    """Remembers the state in memory and logs every change; for the dev machine and tests."""

    def __init__(self) -> None:
        self._paused = False

    def set(self, paused: bool) -> None:
        if paused != self._paused:
            log.info("Dry-run relay: %s", "paused" if paused else "running")
        self._paused = paused

    def read(self) -> bool:
        return self._paused

    def release(self) -> None:
        self.set(False)


class Mcp23017Relay:
    """The relay on one MCP23017 pin; every write is read back from the pin."""

    def __init__(self, pin: Mcp23017) -> None:
        self._pin = pin
        self._configured = False

    def set(self, paused: bool) -> None:
        """Drive the pin, then read it back.

        The pin becomes an output on the first call, already at the requested level.

        :raises RelayMismatch: if the pin does not read back what was written.
        :raises OSError: on a bus error.
        """
        if self._configured:
            self._pin.set_pin(paused)
        else:
            self._pin.configure_output(initial_high=paused)
            self._configured = True
        actual = self._pin.read_pin()
        if actual != paused:
            raise RelayMismatch(f"wrote {paused}, but the pin reads {actual}")

    def read(self) -> bool:
        """Read the pin only; never changes the relay, so it is safe while the service runs."""
        return self._pin.read_pin()

    def release(self) -> None:
        self.set(False)


def create_relay(settings: RelaySettings) -> Relay:
    """The relay chosen in the configuration.

    :raises OSError: if the I2C bus cannot be opened.
    """
    if settings.driver == "dry-run":
        return DryRunRelay()
    bus = shared_bus(settings.i2c_bus)
    return Mcp23017Relay(Mcp23017(bus, settings.i2c_address, settings.pin))


class ServiceLock:
    """An exclusive ``flock`` on a file for as long as the service runs.

    The kernel drops the lock when the process dies, so a crash never leaves a stale lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: TextIOWrapper | None = None

    def acquire(self) -> None:
        """:raises ServiceAlreadyRunning: if another process holds the lock."""
        file = self.path.open("a")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            file.close()
            raise ServiceAlreadyRunning(f"{self.path} is locked by another process") from error
        self._file = file

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def service_running(path: Path) -> bool:
    """True if a process holds the service lock at ``path``."""
    try:
        with path.open("r") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False
    return False
