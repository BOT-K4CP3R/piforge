"""I2C and SPI bus models.

*I2C*: devices attach at a 7-bit address. Transactions are byte strings; an address with no device
behaves like a Linux ``i2c-dev`` NACK: ``OSError(121, "Remote I/O error")`` — exactly what
``smbus2``/Blinka raise on a real Pi.

*SPI*: devices attach at a chip-select index. :meth:`SPIBus.transfer` is a kernel-framed
transaction (``spidev`` / gpiozero hardware SPI): ``begin() → transfer(data) → end()``. Drivers that
toggle chip-select themselves through a GPIO (Blinka ``busio.SPI`` + ``digitalio``) use
:meth:`SPIBus.exchange`, which talks to every device whose CS line is currently driven LOW by the
Pi; ``begin``/``end`` then follow the CS GPIO edges.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from piforge.core.errors import ValidationError

log = logging.getLogger(__name__)

EREMOTEIO = 121  # Linux errno for an I2C NACK  # src: Linux include/uapi/asm-generic/errno.h

# Default chip-select GPIOs: (bus, cs) → BCM.  # src: raspberrypi.com docs (SPI0 CE0/1 = 8/7, SPI1 CE0-2 = 18/17/16)
SPI_CS_GPIO = {(0, 0): 8, (0, 1): 7, (1, 0): 18, (1, 1): 17, (1, 2): 16}


class I2CDevice:
    """Device side of an I2C bus. Override :meth:`on_write` / :meth:`on_read`."""

    def on_write(self, data: bytes) -> None:
        """Master wrote ``data`` (may be empty: an address probe)."""

    def on_read(self, n: int) -> bytes:
        """Master reads ``n`` bytes."""
        return bytes(n)

    def on_write_read(self, data: bytes, n: int) -> bytes:
        """Write then read with a repeated start (default: the two halves in sequence)."""
        self.on_write(data)
        return self.on_read(n)


class RegisterI2CDevice(I2CDevice):
    """Register-file device: first written byte sets the pointer, both directions auto-increment."""

    def __init__(self) -> None:
        self.regs = bytearray(256)
        self.pointer = 0

    def read_register(self, reg: int) -> int:
        """Value of register ``reg`` (override for side effects)."""
        return self.regs[reg]

    def write_register(self, reg: int, value: int) -> None:
        """Store ``value`` in register ``reg`` (override for side effects)."""
        self.regs[reg] = value & 0xFF

    def on_write(self, data: bytes) -> None:
        if not data:
            return
        self.pointer = data[0]
        for b in data[1:]:
            self.write_register(self.pointer, b)
            self.pointer = (self.pointer + 1) & 0xFF

    def on_read(self, n: int) -> bytes:
        out = bytearray()
        for _ in range(n):
            out.append(self.read_register(self.pointer) & 0xFF)
            self.pointer = (self.pointer + 1) & 0xFF
        return bytes(out)


class I2CBus:
    """One I2C bus (e.g. ``/dev/i2c-1`` = SDA1/SCL1 on GPIO2/3)."""

    def __init__(self, num: int, lock: threading.RLock | None = None) -> None:
        self.num = num
        self.lock = lock or threading.RLock()
        self.devices: dict[int, I2CDevice] = {}
        self.stats = {"writes": 0, "reads": 0, "nacks": 0}

    def attach(self, address: int, device: I2CDevice) -> None:
        """Attach ``device`` at 7-bit ``address`` (0x03–0x77)."""
        if not isinstance(address, int) or not 0x03 <= address <= 0x77:
            raise ValidationError(f"I2C address must be 0x03–0x77, got {address!r}")
        with self.lock:
            if address in self.devices:
                raise ValidationError(
                    f"I2C address conflict on bus {self.num}: 0x{address:02x} is already used")
            self.devices[address] = device

    def detach(self, address: int) -> None:
        """Remove the device at ``address`` (no error if absent)."""
        with self.lock:
            self.devices.pop(address, None)

    def _dev(self, address: int) -> I2CDevice:
        dev = self.devices.get(address)
        if dev is None:
            self.stats["nacks"] += 1
            raise OSError(EREMOTEIO, f"Remote I/O error (no I2C device at 0x{address:02x} on bus {self.num})")
        return dev

    def write(self, address: int, data: bytes) -> None:
        """Write transaction ``START addr+W data… STOP``."""
        with self.lock:
            dev = self._dev(address)
            self.stats["writes"] += 1
            dev.on_write(bytes(data))

    def read(self, address: int, n: int) -> bytes:
        """Read transaction of ``n`` bytes."""
        with self.lock:
            dev = self._dev(address)
            self.stats["reads"] += 1
            return _fit(dev.on_read(n), n)

    def write_read(self, address: int, data: bytes, n: int) -> bytes:
        """Write then read with a repeated start (SMBus "read register")."""
        with self.lock:
            dev = self._dev(address)
            self.stats["writes"] += 1
            self.stats["reads"] += 1
            return _fit(dev.on_write_read(bytes(data), n), n)

    def scan(self) -> list[int]:
        """Addresses that ACK, ascending (like ``i2cdetect``)."""
        with self.lock:
            return sorted(self.devices)


def _fit(data: bytes, n: int) -> bytes:
    data = bytes(data)
    return data[:n] if len(data) >= n else data + b"\xff" * (n - len(data))


class SPIDevice:
    """Device side of an SPI bus: full-duplex ``transfer`` framed by ``begin``/``end`` (CS)."""

    def begin(self) -> None:
        """Chip-select asserted."""

    def transfer(self, data: bytes) -> bytes:
        """Clock ``data`` in on MOSI; return the same number of bytes from MISO."""
        return bytes(len(data))

    def end(self) -> None:
        """Chip-select released."""


class SPIBus:
    """One SPI controller (SPI0 / SPI1) with devices on chip-select indices."""

    def __init__(self, num: int, pi: Any = None, lock: threading.RLock | None = None) -> None:
        self.num = num
        self._pi = pi
        self.lock = lock or (pi.lock if pi is not None else threading.RLock())
        self.devices: dict[int, SPIDevice] = {}
        self.cs_gpio: dict[int, int] = {}
        self._selected: set[int] = set()
        self._warned: set[int] = set()

    def attach(self, cs: int, device: SPIDevice, *, cs_gpio: int | None = None) -> None:
        """Attach ``device`` on chip-select ``cs``; ``cs_gpio`` overrides the default CE pin."""
        with self.lock:
            if cs in self.devices:
                raise ValidationError(f"SPI{self.num} chip-select {cs} is already used")
            self.devices[cs] = device
            gpio = cs_gpio if cs_gpio is not None else SPI_CS_GPIO.get((self.num, cs))
            if gpio is not None and self._pi is not None:
                self.cs_gpio[cs] = gpio
                self._pi.add_listener(gpio, lambda bcm, level, t, _cs=cs: self._cs_changed(_cs))

    # -- kernel-framed transfers (spidev, gpiozero hardware SPI) --------------------------------
    def transfer(self, cs: int, data: bytes) -> bytes:
        """One complete transaction on chip-select ``cs``; MISO reads 0 when nothing is attached."""
        with self.lock:
            dev = self.devices.get(cs)
            if dev is None:
                self._warn_empty(cs)
                return bytes(len(data))
            dev.begin()
            try:
                return _fit(dev.transfer(bytes(data)), len(data))
            finally:
                dev.end()

    # -- GPIO chip-select (Blinka busio + digitalio) ---------------------------------------------
    def _is_selected(self, cs: int) -> bool:
        gpio = self.cs_gpio.get(cs)
        if gpio is None or self._pi is None:
            return False
        return self._pi.mode(gpio) == "output" and self._pi.read(gpio) == 0

    def _cs_changed(self, cs: int) -> None:
        with self.lock:
            sel = self._is_selected(cs)
            if sel and cs not in self._selected:
                self._selected.add(cs)
                self.devices[cs].begin()
            elif not sel and cs in self._selected:
                self._selected.discard(cs)
                self.devices[cs].end()

    def exchange(self, data: bytes) -> bytes:
        """Clock ``data`` to every device whose CS GPIO is held LOW by the Pi (no implicit framing)."""
        with self.lock:
            for cs in list(self.devices):
                self._cs_changed(cs)
            active = sorted(self._selected)
            if not active:
                return bytes(len(data))
            out = b""
            for i, cs in enumerate(active):
                res = _fit(self.devices[cs].transfer(bytes(data)), len(data))
                out = res if i == 0 else bytes(a | b for a, b in zip(out, res))
            if len(active) > 1 and self._pi is not None:
                self._pi.record_event("TWIN.SPI_CONTENTION", "warning",
                                      f"SPI{self.num}: several chip-selects active at once {active}",
                                      bus=self.num)
            return out

    def _warn_empty(self, cs: int) -> None:
        if cs in self._warned:
            return
        self._warned.add(cs)
        if self._pi is not None:
            self._pi.record_event("TWIN.SPI_NO_DEVICE", "warning",
                                  f"SPI{self.num} CE{cs}: transfer with no device attached (MISO reads 0)",
                                  bus=self.num, cs=cs)
