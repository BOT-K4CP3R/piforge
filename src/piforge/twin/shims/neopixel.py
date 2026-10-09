"""``neopixel`` shim (Adafruit CircuitPython NeoPixel API) feeding the twin's ``neopixel`` device."""

from __future__ import annotations

from typing import Any

from piforge.twin.runtime import get_twin

RGB = "RGB"
GRB = "GRB"
RGBW = "RGBW"
GRBW = "GRBW"


def _color(value: Any, bpp: int) -> tuple[int, ...]:
    if isinstance(value, int):
        if bpp == 4:
            return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF, (value >> 24) & 0xFF)
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    vals = tuple(int(v) for v in value)
    if len(vals) == 3 and bpp == 4:
        vals = vals + (0,)
    if len(vals) != bpp or any(not 0 <= v <= 255 for v in vals):
        raise ValueError(f"Expected tuple of length {bpp} with values 0-255, got {value!r}")
    return vals


class NeoPixel:
    """``NeoPixel(pin, n, *, bpp=3, brightness=1.0, auto_write=True, pixel_order=None)``."""

    def __init__(self, pin: Any, n: int, *, bpp: int = 3, brightness: float = 1.0, auto_write: bool = True,
                 pixel_order: str | None = None) -> None:
        self.pin = pin
        self.n = int(n)
        self.bpp = len(pixel_order) if pixel_order else bpp
        self.pixel_order = pixel_order or (GRBW if self.bpp == 4 else GRB)
        self._brightness = min(1.0, max(0.0, float(brightness)))
        self.auto_write = auto_write
        self._buf: list[tuple[int, ...]] = [(0,) * self.bpp for _ in range(self.n)]
        twin = get_twin()
        self._dev = twin.find_device("neopixel", int(getattr(pin, "id", pin)))
        if self._dev is None:
            twin.pi.record_event("TWIN.NO_DEVICE", "warning",
                                 f"NeoPixel on GPIO{getattr(pin, 'id', pin)}: no 'neopixel' device configured")

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._buf[i] for i in range(*index.indices(self.n))]
        if index < 0:
            index += self.n
        if not 0 <= index < self.n:
            raise IndexError("Pixel index out of range")
        return self._buf[index]

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            idx = range(*index.indices(self.n))
            vals = list(value)
            if len(vals) != len(idx):
                raise ValueError("Slice and input sequence size do not match.")
            for i, v in zip(idx, vals):
                self._buf[i] = _color(v, self.bpp)
        else:
            if index < 0:
                index += self.n
            if not 0 <= index < self.n:
                raise IndexError("Pixel index out of range")
            self._buf[index] = _color(value, self.bpp)
        if self.auto_write:
            self.show()

    def fill(self, color: Any) -> None:
        """Set every pixel to ``color``."""
        c = _color(color, self.bpp)
        self._buf = [c] * self.n
        if self.auto_write:
            self.show()

    @property
    def brightness(self) -> float:
        return self._brightness

    @brightness.setter
    def brightness(self, value: float) -> None:
        self._brightness = min(1.0, max(0.0, float(value)))
        if self.auto_write:
            self.show()

    def show(self) -> None:
        """Push the buffer to the strip (twin device)."""
        if self._dev is not None:
            self._dev.set_pixels([c[:3] for c in self._buf], self._brightness)

    def deinit(self) -> None:
        self.fill(0)
        self.show()

    def __enter__(self) -> "NeoPixel":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.deinit()

    def __repr__(self) -> str:
        return "[" + ", ".join(str(c) for c in self._buf) + "]"
