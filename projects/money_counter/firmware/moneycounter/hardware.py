"""The real I/O: the 74HCT595 chain on SPI0 (spidev) and the Hall inputs (RPi.GPIO).

Imported only by ``main.py`` so the rest of the firmware (and its unit tests) runs anywhere. In the
PiForge twin the same imports resolve to its simulated ``spidev`` / ``RPi.GPIO``.
"""

from __future__ import annotations


class SpiCoils:
    """32 coil outputs = 4 daisy-chained 74HCT595 on SPI0; CE0 (GPIO8) latches after each transfer."""

    def __init__(self, bus: int = 0, device: int = 0, speed_hz: int = 1_000_000) -> None:
        import spidev

        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0

    def write(self, word: int) -> None:
        # first byte ends in the last chip: big-endian puts bit 0 on chip 0, output QA
        self.spi.xfer2(list(int(word).to_bytes(4, "big")))

    def close(self) -> None:
        try:
            self.spi.close()
        except Exception:
            pass


class GpioHalls:
    """A3144 open-collector outputs on GPIOs with the internal pull-ups (magnet → 0)."""

    def __init__(self, pins) -> None:
        import RPi.GPIO as GPIO

        self.GPIO = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        for pin in pins:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def read(self, pin: int) -> int:
        return 1 if self.GPIO.input(pin) else 0

    def close(self) -> None:
        try:
            self.GPIO.cleanup()
        except Exception:
            pass
