"""``busio`` shim (Blinka API): ``I2C`` and ``SPI`` on the twin's buses.

Chip-select is a plain ``digitalio`` GPIO driven by the driver (adafruit_bus_device); the twin's
SPI bus routes bytes to whichever device's CS line the Pi currently holds LOW.
"""

from __future__ import annotations

import threading
from typing import Any

from piforge.twin.gpio import I2C_PINS
from piforge.twin.runtime import get_twin

_I2C_BY_PINS = {(scl, sda): n for n, (sda, scl) in I2C_PINS.items()}
_SPI_BY_CLOCK = {11: 0, 21: 1}          # SPI0 SCLK = GPIO11, SPI1 SCLK = GPIO21


class _Lockable:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locked = False

    def try_lock(self) -> bool:
        """Acquire the bus without blocking; True on success."""
        if self._lock.acquire(blocking=False):
            self._locked = True
            return True
        return False

    def unlock(self) -> None:
        """Release the bus."""
        if self._locked:
            self._locked = False
            self._lock.release()

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.deinit()

    def deinit(self) -> None:
        """Release resources (no-op in the twin)."""


def _slice(buf: Any, start: int, end: int | None) -> bytes:
    return bytes(buf[start: len(buf) if end is None else end])


class I2C(_Lockable):
    """``busio.I2C(scl, sda, frequency=100000)``."""

    def __init__(self, scl: Any, sda: Any, *, frequency: int = 100000, timeout: int = 255) -> None:
        super().__init__()
        key = (getattr(scl, "id", scl), getattr(sda, "id", sda))
        n = _I2C_BY_PINS.get(key)
        if n is None:
            raise ValueError(f"No Hardware I2C on (scl,sda)={key}\nValid I2C ports: "
                             f"{sorted((n, scl_, sda_) for (scl_, sda_), n in _I2C_BY_PINS.items())}")
        self.bus_number = n
        self._bus = get_twin().pi.i2c_bus(n)
        self.frequency = frequency

    def scan(self) -> list[int]:
        """Addresses that ACK."""
        return self._bus.scan()

    def writeto(self, address: int, buffer: Any, *, start: int = 0, end: int | None = None) -> None:
        self._bus.write(address, _slice(buffer, start, end))

    def readfrom_into(self, address: int, buffer: Any, *, start: int = 0, end: int | None = None) -> None:
        end = len(buffer) if end is None else end
        buffer[start:end] = self._bus.read(address, end - start)

    def writeto_then_readfrom(self, address: int, buffer_out: Any, buffer_in: Any, *, out_start: int = 0,
                              out_end: int | None = None, in_start: int = 0, in_end: int | None = None,
                              stop: bool = False) -> None:
        in_end = len(buffer_in) if in_end is None else in_end
        buffer_in[in_start:in_end] = self._bus.write_read(address, _slice(buffer_out, out_start, out_end),
                                                          in_end - in_start)


class SPI(_Lockable):
    """``busio.SPI(clock, MOSI=None, MISO=None)``."""

    def __init__(self, clock: Any, MOSI: Any = None, MISO: Any = None, half_duplex: bool = False) -> None:  # noqa: N803
        super().__init__()
        clk = getattr(clock, "id", clock)
        n = _SPI_BY_CLOCK.get(clk)
        if n is None:
            raise ValueError(f"No Hardware SPI on (SCLK, MOSI, MISO)=({clk}, {getattr(MOSI, 'id', MOSI)}, "
                             f"{getattr(MISO, 'id', MISO)})\nValid SPI ports: [(0, 11, 10, 9), (1, 21, 20, 19)]")
        self._bus = get_twin().pi.spi[n]
        self._baudrate = 100000
        self._polarity = 0
        self._phase = 0
        self._bits = 8

    def configure(self, *, baudrate: int = 100000, polarity: int = 0, phase: int = 0, bits: int = 8) -> None:
        """Set clock speed/mode (stored; the twin is timing-free)."""
        self._baudrate, self._polarity, self._phase, self._bits = baudrate, polarity, phase, bits

    @property
    def frequency(self) -> int:
        return self._baudrate

    def write(self, buffer: Any, *, start: int = 0, end: int | None = None) -> None:
        self._bus.exchange(_slice(buffer, start, end))

    def readinto(self, buffer: Any, *, start: int = 0, end: int | None = None, write_value: int = 0) -> None:
        end = len(buffer) if end is None else end
        buffer[start:end] = self._bus.exchange(bytes([write_value & 0xFF]) * (end - start))

    def write_readinto(self, buffer_out: Any, buffer_in: Any, *, out_start: int = 0, out_end: int | None = None,
                       in_start: int = 0, in_end: int | None = None) -> None:
        data = _slice(buffer_out, out_start, out_end)
        in_end = len(buffer_in) if in_end is None else in_end
        res = self._bus.exchange(data)
        buffer_in[in_start:in_end] = res[: in_end - in_start]


class UART:
    """Serial ports are not simulated."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("busio.UART is not simulated by the PiForge twin")
