"""Shim-level devices: WS2812 NeoPixel strip and the Pi camera (picamera2 stub)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from piforge.twin.devices.base import Device, PropSpec, register


@register
class NeoPixel(Device):
    """WS2812/NeoPixel strip on a data GPIO. Fed by the ``neopixel`` / ``rpi_ws281x`` shims
    (the one-wire 800 kHz protocol itself is not bit-simulated)."""

    type = "neopixel"
    label = "NeoPixel strip"
    pin_roles = ("pin",)
    pin_aliases = {"din": "pin", "di": "pin", "data": "pin", "sig": "pin", "in": "pin"}
    defaults = {"count": 8, "order": "GRB"}
    outputs = {"pixels": PropSpec("text", default="", label="Pixels (#rrggbb list)", widget="pixels"),
               "shows": PropSpec("int", 0, None, default=0, label="Frames shown")}
    example = {"pins": {"pin": 18}, "params": {"count": 8}}

    def setup(self) -> None:
        self.count = int(self.params.get("count", 8))
        self._pixels: list[tuple[int, int, int]] = [(0, 0, 0)] * self.count
        self._shows = 0

    def set_pixels(self, colors: list[tuple[int, int, int]], brightness: float = 1.0) -> None:
        """Latch a frame: ``colors`` are (r, g, b) 0..255 before ``brightness`` scaling."""
        with self.pi.lock:
            out = []
            for i in range(self.count):
                r, g, b = (colors[i][:3] if i < len(colors) else (0, 0, 0))
                out.append(tuple(max(0, min(255, int(round(c * brightness)))) for c in (r, g, b)))
            self._pixels = out  # type: ignore[assignment]
            self._shows += 1
            self.display_version += 1

    def outputs_state(self) -> dict:
        return {"pixels": [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in self._pixels], "shows": self._shows}


_PATTERNS = ("bars", "gradient", "checker", "noise", "black", "white")


@register
class Camera(Device):
    """Raspberry Pi camera (CSI, no header pins) served by the ``picamera2`` shim.

    Inputs: ``image`` — path of a picture to "see" (relative paths resolve against the firmware
    working directory), else a generated ``pattern``.
    # src: Raspberry Pi Camera Module 3 — IMX708, 4608×2592 full resolution
    """

    type = "camera"
    label = "Camera"
    defaults = {"sensor_width": 4608, "sensor_height": 2592, "model": "imx708"}
    inputs = {"image": PropSpec("image", default="", label="Scene image (path)", widget="file"),
              "pattern": PropSpec("enum", default="bars", choices=_PATTERNS, label="Test pattern")}
    outputs = {"captures": PropSpec("int", 0, None, default=0, label="Captures"),
               "running": PropSpec("bool", default=False, label="Streaming"),
               "last_file": PropSpec("text", default="", label="Last saved file")}
    example: dict = {}

    def setup(self) -> None:
        self.captures = 0
        self.running = False
        self.last_file = ""
        self._image_cache: tuple[str, Any] | None = None

    def note_capture(self, path: str | None = None) -> None:
        """Called by the shim for every captured frame."""
        with self.pi.lock:
            self.captures += 1
            if path:
                self.last_file = str(path)

    def set_running(self, running: bool) -> None:
        """Called by the shim on ``start()``/``stop()``."""
        with self.pi.lock:
            self.running = bool(running)

    def frame(self, size: tuple[int, int] | None = None) -> Any:
        """Current scene as an RGB ``uint8`` array of shape (h, w, 3); ``size`` = (w, h)."""
        import numpy as np

        w, h = size or (640, 480)
        path = str(self._inputs.get("image") or "")
        if path:
            img = self._load(path)
            if img is not None:
                return np.asarray(img.resize((w, h)).convert("RGB"), dtype=np.uint8)
        return self._pattern(str(self._inputs.get("pattern") or "bars"), w, h)

    def _load(self, path: str) -> Any:
        from PIL import Image

        if self._image_cache and self._image_cache[0] == path:
            return self._image_cache[1]
        try:
            with Image.open(Path(path).expanduser()) as im:
                img = im.convert("RGB")
        except OSError as exc:
            self.pi.record_event("TWIN.CAMERA_IMAGE", "warning",
                                 f"{self.id}: cannot load image {path!r} ({exc}); using test pattern",
                                 device=self.id)
            return None
        self._image_cache = (path, img)
        return img

    @staticmethod
    def _pattern(name: str, w: int, h: int) -> Any:
        import numpy as np

        if name == "black":
            return np.zeros((h, w, 3), dtype=np.uint8)
        if name == "white":
            return np.full((h, w, 3), 255, dtype=np.uint8)
        if name == "noise":
            return np.random.default_rng(0).integers(0, 256, (h, w, 3), dtype=np.uint8)
        if name == "checker":
            yy, xx = np.mgrid[0:h, 0:w]
            c = (((xx // max(1, w // 8)) + (yy // max(1, h // 6))) % 2 * 255).astype(np.uint8)
            return np.repeat(c[:, :, None], 3, axis=2)
        if name == "gradient":
            xs = np.linspace(0, 255, w, dtype=np.float32)
            ys = np.linspace(0, 255, h, dtype=np.float32)
            r = np.broadcast_to(xs[None, :], (h, w))
            g = np.broadcast_to(ys[:, None], (h, w))
            b = 255 - r
            return np.stack([r, g, b], axis=2).astype(np.uint8)
        # SMPTE-like colour bars  # src: SMPTE EG 1-1990 (75 % bars order)
        bars = [(191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0), (191, 0, 191), (191, 0, 0),
                (0, 0, 191)]
        idx = (np.arange(w) * len(bars) // max(1, w)).clip(0, len(bars) - 1)
        row = np.array(bars, dtype=np.uint8)[idx]
        return np.repeat(row[None, :, :], h, axis=0)

    def outputs_state(self) -> dict:
        return {"captures": self.captures, "running": self.running, "last_file": self.last_file}

    def on_input(self, prop: str, value: Any) -> None:
        self._image_cache = None
