"""Fixtures for the digital-twin tests.

The firmware shims (``RPi``, ``smbus2``, ``board``, …) are top-level module names that must never
be importable in a normal process. Tests that need them use the :func:`shims` fixture, which puts
the shims directory first on ``sys.path`` and purges every shim/driver module from
``sys.modules`` before and after the test so nothing leaks into other tests.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import piforge.twin
from piforge.twin.config import DeviceConfig, TwinConfig

SHIMS_DIR = Path(piforge.twin.__file__).resolve().parent / "shims"

# Top-level names provided by the shims plus real drivers that bind to them at import time.
PURGE_TOPLEVEL = {
    "RPi", "smbus2", "smbus", "spidev", "board", "busio", "digitalio", "pwmio",
    "microcontroller", "micropython", "neopixel", "neopixel_write", "picamera2", "libcamera",
    "adafruit_dht", "Adafruit_DHT", "w1thermsensor", "rpi_ws281x", "analogio", "pulseio",
    "bitbangio", "adafruit_blinka",
    "adafruit_bme280", "adafruit_ssd1306", "adafruit_bus_device", "adafruit_framebuf", "RPLCD",
}


def _purge() -> None:
    for name in list(sys.modules):
        if name.split(".", 1)[0] in PURGE_TOPLEVEL:
            del sys.modules[name]


@pytest.fixture
def shims() -> Iterator[Path]:
    """Make the twin shims importable as top-level modules for the duration of one test."""
    _purge()
    sys.path.insert(0, str(SHIMS_DIR))
    try:
        yield SHIMS_DIR
    finally:
        while str(SHIMS_DIR) in sys.path:
            sys.path.remove(str(SHIMS_DIR))
        _purge()


@pytest.fixture
def make_twin() -> Iterator[Callable[..., object]]:
    """Factory fixture: ``make_twin(*device_configs, pulls=..., start=True)`` → active Twin."""
    from piforge.twin.runtime import Twin, set_twin

    made: list = []

    def _make(*devices: DeviceConfig, pulls: dict | None = None, start: bool = True,
              clock=None, board: str = "rpi4b"):
        cfg = TwinConfig(board=board, devices=list(devices), pulls=dict(pulls or {}))
        twin = Twin(cfg, clock=clock)
        set_twin(twin)
        if start:
            twin.start()
        made.append(twin)
        return twin

    yield _make
    for t in made:
        t.stop()
    set_twin(None)


@pytest.fixture
def gpiozero_twin(make_twin) -> Iterator[Callable[..., object]]:
    """Like ``make_twin`` but also installs :class:`TwinFactory` as gpiozero's pin factory."""
    from gpiozero import Device

    from piforge.twin.gpiozero_factory import TwinFactory

    factories: list = []

    def _make(*devices: DeviceConfig, **kw):
        twin = make_twin(*devices, **kw)
        factory = TwinFactory(twin)
        Device.pin_factory = factory
        factories.append(factory)
        return twin

    yield _make
    for f in factories:
        try:
            f.close()
        except Exception:  # pragma: no cover - best effort cleanup
            pass
    Device.pin_factory = None


def _wait_until(pred: Callable[[], object], timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if pred():
                return True
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


@pytest.fixture
def wait_until() -> Callable[..., bool]:
    """``wait_until(pred, timeout=2.0)``: poll ``pred`` until truthy; False on timeout."""
    return _wait_until
