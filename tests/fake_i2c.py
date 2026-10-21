"""A fake smbus for the HAT: registers per device, with an MCP23017 that mirrors OLAT to GPIO."""

IODIR, GPIO, OLAT = 0x00, 0x12, 0x14


class FakeSMBus:
    """In-memory smbus: (address, register) -> bytes; records every write.

    Devices listed in ``mcp23017`` behave like the expander: a pin that is an output reads back
    its latch, unless ``stuck`` is set, in which case GPIO never follows the latch.
    """

    def __init__(self, devices: set[int] | None = None, mcp23017: set[int] | None = None) -> None:
        self.mcp23017 = mcp23017 or set()
        self.devices = (devices or set()) | self.mcp23017
        self.registers: dict[tuple[int, int], bytes] = {}
        for address in self.mcp23017:
            for port in (0, 1):
                self.registers[address, IODIR + port] = b"\xff"  # power-on: all inputs
                self.registers[address, GPIO + port] = b"\x00"
                self.registers[address, OLAT + port] = b"\x00"
        self.writes: list[tuple[int, int, bytes]] = []
        self.stuck = False
        self.closed = False

    def read_i2c_block_data(self, i2c_addr: int, register: int, length: int) -> list[int]:
        if i2c_addr not in self.devices:
            raise OSError(121, "Remote I/O error")
        return list(self.registers[i2c_addr, register][:length])

    def write_i2c_block_data(self, i2c_addr: int, register: int, data: list[int]) -> None:
        if i2c_addr not in self.devices:
            raise OSError(121, "Remote I/O error")
        self.writes.append((i2c_addr, register, bytes(data)))
        self.registers[i2c_addr, register] = bytes(data)
        if i2c_addr in self.mcp23017:
            self._mirror_latch(i2c_addr, register)

    def read_byte(self, i2c_addr: int) -> int:
        if i2c_addr not in self.devices:
            raise OSError(121, "Remote I/O error")
        return 0

    def close(self) -> None:
        self.closed = True

    def _mirror_latch(self, address: int, register: int) -> None:
        if self.stuck or register not in (IODIR, IODIR + 1, OLAT, OLAT + 1):
            return
        port = register % 2
        iodir = self.registers[address, IODIR + port][0]
        olat = self.registers[address, OLAT + port][0]
        inputs = self.registers[address, GPIO + port][0] & iodir
        self.registers[address, GPIO + port] = bytes([inputs | olat & ~iodir & 0xFF])
