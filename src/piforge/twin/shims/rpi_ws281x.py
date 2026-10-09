"""``rpi_ws281x`` shim (``PixelStrip`` / ``Adafruit_NeoPixel`` / ``Color``) feeding a twin ``neopixel`` device."""

from __future__ import annotations

from typing import Any

from piforge.twin.runtime import get_twin

WS2811_STRIP_RGB = 0x100800
WS2811_STRIP_GRB = 0x081000
WS2812_STRIP = WS2811_STRIP_GRB


def Color(red: int, green: int, blue: int, white: int = 0) -> int:  # noqa: N802 - library API
    """Pack a 24/32-bit colour."""
    return (white << 24) | (red << 16) | (green << 8) | blue


class PixelStrip:
    """``PixelStrip(num, pin, freq_hz=800000, dma=10, invert=False, brightness=255, channel=0, strip_type=None)``."""

    def __init__(self, num: int, pin: int, freq_hz: int = 800000, dma: int = 10, invert: bool = False,
                 brightness: int = 255, channel: int = 0, strip_type: Any = None, gamma: Any = None) -> None:
        self._n = int(num)
        self._pin = int(pin)
        self._brightness = int(brightness)
        self._buf = [0] * self._n
        self._dev: Any = None

    def begin(self) -> None:
        twin = get_twin()
        self._dev = twin.find_device("neopixel", self._pin)
        if self._dev is None:
            twin.pi.record_event("TWIN.NO_DEVICE", "warning",
                                 f"PixelStrip on GPIO{self._pin}: no 'neopixel' device configured")

    def show(self) -> None:
        if self._dev is not None:
            self._dev.set_pixels([((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF) for c in self._buf],
                                 self._brightness / 255.0)

    def setPixelColor(self, n: int, color: int) -> None:  # noqa: N802
        self._buf[n] = int(color)

    def setPixelColorRGB(self, n: int, red: int, green: int, blue: int, white: int = 0) -> None:  # noqa: N802
        self._buf[n] = Color(red, green, blue, white)

    def getPixelColor(self, n: int) -> int:  # noqa: N802
        return self._buf[n]

    def getPixels(self) -> list[int]:  # noqa: N802
        return list(self._buf)

    def numPixels(self) -> int:  # noqa: N802
        return self._n

    def setBrightness(self, brightness: int) -> None:  # noqa: N802
        self._brightness = int(brightness)

    def getBrightness(self) -> int:  # noqa: N802
        return self._brightness


Adafruit_NeoPixel = PixelStrip
