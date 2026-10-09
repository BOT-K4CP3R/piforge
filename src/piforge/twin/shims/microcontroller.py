"""``microcontroller`` shim (CircuitPython/Blinka): ``Pin`` objects keyed by BCM number."""

from __future__ import annotations

import time
from types import SimpleNamespace


class Pin:
    """A GPIO pin identified by its BCM number (``id``), like Blinka's ``microcontroller.Pin``."""

    IN, OUT = 0, 1
    LOW, HIGH = 0, 1
    PULL_NONE, PULL_UP, PULL_DOWN = 0, 1, 2

    def __init__(self, bcm_number: int) -> None:
        self.id = int(bcm_number)

    def __repr__(self) -> str:
        return f"board.D{self.id}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Pin) and other.id == self.id

    def __hash__(self) -> int:
        return hash(("Pin", self.id))


pin = SimpleNamespace(**{f"D{i}": Pin(i) for i in range(28)})
"""``microcontroller.pin.D0`` … ``D27``."""


class _CPU:
    """Minimal ``microcontroller.cpu`` (values of a Pi 4 at idle)."""

    frequency = 1_500_000_000      # src: Raspberry Pi 4B product brief — BCM2711 @ 1.5 GHz
    temperature = 45.0
    voltage = 0.85
    uid = bytearray(b"PIFORGE-TWIN")


cpu = _CPU()
cpus = [cpu]


def delay_us(delay: int) -> None:
    """Busy-wait ``delay`` microseconds."""
    time.sleep(delay / 1e6)


def reset() -> None:
    """Not available on a Raspberry Pi."""
    raise NotImplementedError("microcontroller.reset() is not supported on a Raspberry Pi (PiForge twin)")
