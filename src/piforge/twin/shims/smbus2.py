"""``smbus2`` shim backed by the PiForge twin's I2C buses.

Covers the smbus2 0.4/0.5 API: byte/word/block SMBus transactions, ``i2c_msg`` + ``i2c_rdwr``
combined transactions, ``write_quick`` probing. A missing device raises
``OSError(121, "Remote I/O error")`` like Linux ``i2c-dev``.
"""

from __future__ import annotations

import errno
from typing import Any

from piforge.twin.runtime import get_twin

I2C_SMBUS_BLOCK_MAX = 32  # src: linux/i2c.h I2C_SMBUS_BLOCK_MAX
I2C_M_RD = 0x0001


class i2c_msg:  # noqa: N801 - smbus2 API name
    """One message of an ``i2c_rdwr`` combined transaction."""

    def __init__(self, addr: int, flags: int, buf: bytearray) -> None:
        self.addr = addr
        self.flags = flags
        self.buf = buf
        self.len = len(buf)

    @staticmethod
    def read(address: int, length: int) -> "i2c_msg":
        """Message reading ``length`` bytes."""
        return i2c_msg(address, I2C_M_RD, bytearray(length))

    @staticmethod
    def write(address: int, buf: Any) -> "i2c_msg":
        """Message writing ``buf`` (bytes, list of ints or str)."""
        if isinstance(buf, str):
            data = bytearray(buf.encode("latin-1"))
        else:
            data = bytearray(buf)
        return i2c_msg(address, 0, data)

    def __iter__(self) -> Any:
        return iter(bytes(self.buf[: self.len]))

    def __len__(self) -> int:
        return self.len

    def __bytes__(self) -> bytes:
        return bytes(self.buf[: self.len])

    def __repr__(self) -> str:
        return f"i2c_msg({self.addr}, {self.flags}, {bytes(self)!r})"


def _bus_number(bus: Any) -> int:
    if isinstance(bus, int):
        return bus
    s = str(bus)
    tail = s.rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    raise TypeError(f"Unexpected type(bus)={type(bus)}")


class SMBus:
    """``SMBus(bus)`` — ``bus`` is a number or ``/dev/i2c-N`` path."""

    def __init__(self, bus: Any = None, force: bool = False) -> None:
        self.fd: int | None = None
        self.funcs = 0x0EFF0008
        self.address: int | None = None
        self.force = force
        self._pec = 0
        self._bus: Any = None
        if bus is not None:
            self.open(bus)

    def __enter__(self) -> "SMBus":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def open(self, bus: Any) -> None:
        """Open ``/dev/i2c-<bus>``."""
        n = _bus_number(bus)
        twin = get_twin()
        if n not in twin.pi.i2c:
            raise FileNotFoundError(errno.ENOENT, f"No such file or directory: '/dev/i2c-{n}'")
        self._bus = twin.pi.i2c[n]
        self.fd = 100 + n

    def close(self) -> None:
        """Close the bus."""
        self._bus = None
        self.fd = None

    @property
    def pec(self) -> int:
        return self._pec

    @pec.setter
    def pec(self, enable: Any) -> None:
        self._pec = int(bool(enable))

    def enable_pec(self, enable: bool = True) -> None:
        self.pec = enable

    def _b(self, addr: int) -> Any:
        if self._bus is None:
            raise OSError(errno.EBADF, "Bad file descriptor (SMBus not open)")
        if not 0 <= int(addr) <= 0x7F:
            raise ValueError(f"I2C address out of range: {addr}")
        return self._bus

    # -- SMBus transactions --------------------------------------------------------------------
    def write_quick(self, i2c_addr: int, force: bool | None = None) -> None:
        self._b(i2c_addr).write(i2c_addr, b"")

    def read_byte(self, i2c_addr: int, force: bool | None = None) -> int:
        return self._b(i2c_addr).read(i2c_addr, 1)[0]

    def write_byte(self, i2c_addr: int, value: int, force: bool | None = None) -> None:
        self._b(i2c_addr).write(i2c_addr, bytes([value & 0xFF]))

    def read_byte_data(self, i2c_addr: int, register: int, force: bool | None = None) -> int:
        return self._b(i2c_addr).write_read(i2c_addr, bytes([register & 0xFF]), 1)[0]

    def write_byte_data(self, i2c_addr: int, register: int, value: int, force: bool | None = None) -> None:
        self._b(i2c_addr).write(i2c_addr, bytes([register & 0xFF, value & 0xFF]))

    def read_word_data(self, i2c_addr: int, register: int, force: bool | None = None) -> int:
        b = self._b(i2c_addr).write_read(i2c_addr, bytes([register & 0xFF]), 2)
        return b[0] | (b[1] << 8)

    def write_word_data(self, i2c_addr: int, register: int, value: int, force: bool | None = None) -> None:
        self._b(i2c_addr).write(i2c_addr, bytes([register & 0xFF, value & 0xFF, (value >> 8) & 0xFF]))

    def process_call(self, i2c_addr: int, register: int, value: int, force: bool | None = None) -> int:
        b = self._b(i2c_addr).write_read(i2c_addr, bytes([register & 0xFF, value & 0xFF, (value >> 8) & 0xFF]), 2)
        return b[0] | (b[1] << 8)

    def read_block_data(self, i2c_addr: int, register: int, force: bool | None = None) -> list[int]:
        b = self._b(i2c_addr).write_read(i2c_addr, bytes([register & 0xFF]), I2C_SMBUS_BLOCK_MAX + 1)
        n = min(b[0], I2C_SMBUS_BLOCK_MAX)
        return list(b[1:1 + n])

    def write_block_data(self, i2c_addr: int, register: int, data: list[int], force: bool | None = None) -> None:
        if len(data) > I2C_SMBUS_BLOCK_MAX:
            raise ValueError(f"Data length cannot exceed {I2C_SMBUS_BLOCK_MAX} bytes")
        self._b(i2c_addr).write(i2c_addr, bytes([register & 0xFF, len(data)] + [d & 0xFF for d in data]))

    def block_process_call(self, i2c_addr: int, register: int, data: list[int], force: bool | None = None) -> list[int]:
        bus = self._b(i2c_addr)
        b = bus.write_read(i2c_addr, bytes([register & 0xFF, len(data)] + [d & 0xFF for d in data]),
                           I2C_SMBUS_BLOCK_MAX + 1)
        return list(b[1:1 + min(b[0], I2C_SMBUS_BLOCK_MAX)])

    def read_i2c_block_data(self, i2c_addr: int, register: int, length: int, force: bool | None = None) -> list[int]:
        if length > I2C_SMBUS_BLOCK_MAX:
            raise ValueError(f"Desired block length over {I2C_SMBUS_BLOCK_MAX} bytes")
        return list(self._b(i2c_addr).write_read(i2c_addr, bytes([register & 0xFF]), length))

    def write_i2c_block_data(self, i2c_addr: int, register: int, data: list[int], force: bool | None = None) -> None:
        if len(data) > I2C_SMBUS_BLOCK_MAX:
            raise ValueError(f"Data length cannot exceed {I2C_SMBUS_BLOCK_MAX} bytes")
        self._b(i2c_addr).write(i2c_addr, bytes([register & 0xFF] + [d & 0xFF for d in data]))

    def i2c_rdwr(self, *i2c_msgs: i2c_msg) -> None:
        """Combined transaction; a write followed by a read of the same address uses a repeated start."""
        msgs = list(i2c_msgs)
        i = 0
        while i < len(msgs):
            m = msgs[i]
            bus = self._b(m.addr)
            if m.flags & I2C_M_RD:
                m.buf[:] = bus.read(m.addr, m.len)
            elif i + 1 < len(msgs) and msgs[i + 1].flags & I2C_M_RD and msgs[i + 1].addr == m.addr:
                r = msgs[i + 1]
                r.buf[:] = bus.write_read(m.addr, bytes(m), r.len)
                i += 1
            else:
                bus.write(m.addr, bytes(m))
            i += 1
