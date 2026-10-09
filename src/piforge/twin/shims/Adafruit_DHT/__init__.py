"""Legacy ``Adafruit_DHT`` shim: ``read_retry(Adafruit_DHT.DHT22, pin)`` → (humidity, temperature)."""

from __future__ import annotations

from typing import Any

from piforge.twin.runtime import get_twin

DHT11 = 11
DHT22 = 22
AM2302 = 22


def read(sensor: int, pin: Any, platform: Any = None) -> tuple[float | None, float | None]:
    """One read attempt: (humidity %RH, temperature °C) or (None, None)."""
    if sensor not in (DHT11, DHT22):
        raise ValueError("Expected DHT11, DHT22, or AM2302 sensor value.")
    dev = get_twin().find_device("dht22", int(getattr(pin, "id", pin)))
    if dev is None:
        return None, None
    t, h = dev.measure()
    if sensor == DHT11:
        t, h = float(round(t)), float(round(h))
    return h, t


def read_retry(sensor: int, pin: Any, retries: int = 15, delay_seconds: float = 2, platform: Any = None
               ) -> tuple[float | None, float | None]:
    """Like :func:`read` with retries (the twin answers first time when the device exists)."""
    return read(sensor, pin, platform)
