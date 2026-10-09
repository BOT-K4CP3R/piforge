"""``adafruit_dht`` shim: ``DHT22(board.D4).temperature`` / ``.humidity`` from the twin ``dht22`` device.

(The legacy ``Adafruit_DHT`` API lives in the ``Adafruit_DHT/`` package next to this file — two
names that differ only in case cannot both be ``.py`` files on macOS' case-insensitive disks.)
"""

from __future__ import annotations

from typing import Any

from piforge.twin.runtime import get_twin


class DHTBase:
    """Common DHT API (``measure()``, ``temperature``, ``humidity``, ``exit()``)."""

    def __init__(self, pin: Any, use_pulseio: bool = True) -> None:
        self.pin = pin
        self._bcm = int(getattr(pin, "id", pin))
        self._temperature: float | None = None
        self._humidity: float | None = None

    def measure(self) -> None:
        """Read the sensor (raises RuntimeError like the real driver when nothing answers)."""
        dev = get_twin().find_device("dht22", self._bcm)
        if dev is None:
            raise RuntimeError("DHT sensor not found, check wiring")
        self._temperature, self._humidity = dev.measure()

    @property
    def temperature(self) -> float | None:
        self.measure()
        return self._temperature

    @property
    def humidity(self) -> float | None:
        self.measure()
        return self._humidity

    def exit(self) -> None:
        """Release the pin."""

    deinit = exit


class DHT22(DHTBase):
    """DHT22 / AM2302."""


class DHT21(DHTBase):
    """DHT21 / AM2301."""


class DHT11(DHTBase):
    """DHT11 has 1 °C / 1 %RH resolution."""

    def measure(self) -> None:
        super().measure()
        if self._temperature is not None:
            self._temperature = float(round(self._temperature))
            self._humidity = float(round(self._humidity or 0.0))
