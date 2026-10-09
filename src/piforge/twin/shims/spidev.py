"""``spidev`` shim: ``SpiDev().open(bus, device)`` talks to the twin's SPI bus / chip-select.

``xfer``, ``xfer2`` and ``xfer3`` are all one CS-framed transaction (the per-byte CS toggling of
``xfer`` on real kernels is not modelled).
"""

from __future__ import annotations

import errno
from typing import Any

from piforge.twin.bus import SPI_CS_GPIO
from piforge.twin.runtime import get_twin


class SpiDev:
    """Linux spidev device ``/dev/spidev<bus>.<device>``."""

    def __init__(self, bus: int | None = None, client: int | None = None) -> None:
        self._bus: Any = None
        self._cs = 0
        self.mode = 0
        self.bits_per_word = 8
        self.max_speed_hz = 125_000_000
        self.lsbfirst = False
        self.cshigh = False
        self.threewire = False
        self.loop = False
        self.no_cs = False
        if bus is not None and client is not None:
            self.open(bus, client)

    def open(self, bus: int, device: int) -> None:
        """Open ``/dev/spidev{bus}.{device}``."""
        twin = get_twin()
        if bus not in twin.pi.spi or (bus, device) not in SPI_CS_GPIO:
            raise FileNotFoundError(errno.ENOENT, f"No such file or directory: '/dev/spidev{bus}.{device}'")
        self._bus = twin.pi.spi[bus]
        self._cs = device

    def close(self) -> None:
        self._bus = None

    def __enter__(self) -> "SpiDev":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _rev(b: int) -> int:
        return int(f"{b:08b}"[::-1], 2)

    def _xfer(self, values: Any) -> list[int]:
        if self._bus is None:
            raise OSError(errno.EBADF, "Bad file descriptor (SpiDev not open)")
        data = [int(v) & 0xFF for v in values]
        if self.lsbfirst:
            data = [self._rev(b) for b in data]
        out = list(self._bus.transfer(self._cs, bytes(data)))
        return [self._rev(b) for b in out] if self.lsbfirst else out

    def xfer(self, values: Any, speed_hz: int = 0, delay_usecs: int = 0, bits_per_word: int = 0) -> list[int]:
        return self._xfer(values)

    def xfer2(self, values: Any, speed_hz: int = 0, delay_usecs: int = 0, bits_per_word: int = 0) -> list[int]:
        return self._xfer(values)

    def xfer3(self, values: Any, speed_hz: int = 0, delay_usecs: int = 0, bits_per_word: int = 0) -> list[int]:
        return self._xfer(values)

    def readbytes(self, n: int) -> list[int]:
        return self._xfer([0] * n)

    def writebytes(self, values: Any) -> None:
        self._xfer(values)

    def writebytes2(self, values: Any) -> None:
        self._xfer(values)

    def fileno(self) -> int:
        return -1 if self._bus is None else 200 + self._cs
