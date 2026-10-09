"""Digital twin: a virtual Raspberry Pi that runs real, unmodified Python firmware.

Building blocks (all import-light; nothing here pulls in the CAD kernel):

- :mod:`piforge.twin.config`        ``TwinConfig`` / ``DeviceConfig`` (JSON round-trip)
- :mod:`piforge.twin.clock`         real-time ``SimClock`` and deterministic ``ManualClock``
- :mod:`piforge.twin.gpio`          ``VirtualPi``: BCM 0–27, pulls, drivers, contention, PWM, scheduler
- :mod:`piforge.twin.bus`           I2C / SPI bus models
- :mod:`piforge.twin.devices`       device models (buttons, LEDs, servos, BME280, SSD1306, …)
- :mod:`piforge.twin.runtime`       ``Twin`` (pi + devices + sim thread), ``get_twin``/``set_twin``
- :mod:`piforge.twin.gpiozero_factory`  gpiozero pin factory backed by the twin
- :mod:`piforge.twin.runner`        ``python -m piforge.twin.runner`` — runs firmware in a subprocess
- :mod:`piforge.twin.session`       ``TwinSession``: start/stop/talk to a runner (server, scenarios)
- :mod:`piforge.twin.scenario`      scripted scenarios → pass/fail :class:`~piforge.core.report.Report`
- :mod:`piforge.twin.from_circuit`  ``twin_config_from_circuit`` (auto-wiring from ``piforge.elec``)

``shims/`` holds top-level stand-ins for hardware modules (``RPi.GPIO``, ``smbus2``, ``spidev``,
``board``/``busio``/``digitalio`` …). That directory is only put on ``sys.path`` inside the runner
(and in tests), never in a normal process.
"""

from __future__ import annotations

from pathlib import Path

SHIMS_DIR: Path = Path(__file__).resolve().parent / "shims"
"""Directory with the top-level shim modules (only placed on ``sys.path`` inside the runner)."""

__all__ = ["SHIMS_DIR"]
