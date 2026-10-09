"""money_counter firmware — split-flap money counter ``000000,00`` on a Raspberry Pi Zero 2 W.

Receives the amount over WebSocket (client or server mode, see ``config.toml``) and counts up to
it on 8 split-flap modules (28BYJ-48 steppers via 4 × 74HCT595 on SPI0 + ULN2003 boards, A3144
Hall sensors for homing). Runs unmodified on the Pi and in the PiForge twin.

On the Pi::

    sudo raspi-config nonint do_spi 0           # enable SPI0
    pip install websockets                       # (python3-websockets on Raspberry Pi OS)
    python3 main.py                              # or the systemd unit from README.md

Log lines: ``HOME …``, ``TARGET 001234,56 (count-up 1.5 s)``, ``SHOW 001234,56`` (settled and
checked), ``RESYNC m<i> err=<n>`` (Hall check found lost steps, corrected), ``DERATE …``, ``VERIFY …``,
``CLAMP …``, ``IGNORED …``, ``WS …``.
"""

from __future__ import annotations

import asyncio
import threading

from moneycounter.app import Controller, log
from moneycounter.config import load
from moneycounter.hardware import GpioHalls, SpiCoils
from moneycounter.motion import Motion


def main() -> None:
    cfg = load()
    m = cfg.motion
    bus = SpiCoils(m.spi_bus, m.spi_device, m.spi_speed_hz)
    halls = GpioHalls(m.hall_pins)
    motion = Motion(steps_per_rev=m.steps_per_rev, flaps=m.flaps, max_pps=m.max_pps, start_pps=m.start_pps,
                    accel=m.accel, hold_ms=m.hold_ms, hall_pins=m.hall_pins, offsets=m.offsets, bus=bus,
                    halls=halls, home_timeout_s=m.home_timeout_s, resync_tolerance=m.resync_tolerance,
                    selftest=m.selftest, log=log)
    stop = threading.Event()
    stepper = threading.Thread(target=motion.run, args=(stop,), name="steppers", daemon=True)
    log(f"money_counter: {len(motion.modules)} modules, {m.start_pps:.0f}→{m.max_pps:.0f} half-steps/s "
        f"(accel {m.accel:.0f}/s²), homing")
    motion.start_homing()
    stepper.start()
    try:
        asyncio.run(Controller(cfg, motion).main())
    finally:
        stop.set()
        stepper.join(1.0)
        motion.release()                # coils off
        bus.close()
        halls.close()


if __name__ == "__main__":
    main()
