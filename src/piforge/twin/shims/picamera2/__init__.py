"""``picamera2`` shim: stills from the twin ``camera`` device (test pattern or a chosen image).

Supported: ``Picamera2()``, ``create_preview/still/video_configuration``, ``configure``, ``start``/
``stop``/``close``, ``capture_array``, ``capture_image``, ``capture_file``, ``capture_metadata``,
``capture_request``, ``switch_mode_and_capture_*``, ``start_and_capture_file``, ``set_controls``.
Pixel formats follow libcamera naming: ``RGB888`` → arrays in **B,G,R** order, ``BGR888`` → R,G,B,
``XBGR8888`` → R,G,B,255, ``XRGB8888`` → B,G,R,255.  # src: Picamera2 manual §4.2.2.2 (image formats)
Video encoding/recording is not simulated.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from piforge.twin.runtime import get_twin

_SENSOR = (4608, 2592)        # src: Camera Module 3 (IMX708) full resolution


class Preview:
    """Preview backends (no window is opened in the twin)."""

    NULL = "NULL"
    DRM = "DRM"
    QT = "QT"
    QTGL = "QTGL"


def _cameras() -> list[Any]:
    return [d for d in get_twin().devices.values() if d.type == "camera"]


class _Request:
    def __init__(self, cam: "Picamera2") -> None:
        self._cam = cam

    def make_array(self, name: str = "main") -> Any:
        return self._cam.capture_array(name)

    def make_image(self, name: str = "main") -> Any:
        return self._cam.capture_image(name)

    def save(self, name: str, file_output: Any, format: str | None = None) -> None:  # noqa: A002
        self._cam.capture_file(file_output, name=name, format=format)

    def get_metadata(self) -> dict:
        return self._cam.capture_metadata()

    def release(self) -> None:
        pass


class Picamera2:
    """The twin's Raspberry Pi camera."""

    def __init__(self, camera_num: int = 0, tuning: Any = None) -> None:
        cams = _cameras()
        if camera_num >= len(cams):
            raise IndexError(f"PiForge twin: no camera #{camera_num} — add a 'camera' device to the twin config")
        self._dev = cams[camera_num]
        self.camera_config: dict | None = None
        self.started = False
        self.controls: dict = {}
        self.sensor_resolution = _SENSOR
        self.camera_properties = {"Model": str(self._dev.params.get("model", "imx708")),
                                  "PixelArraySize": _SENSOR, "Location": 2, "Rotation": 180}

    @staticmethod
    def global_camera_info() -> list[dict]:
        return [{"Model": "imx708", "Location": 2, "Rotation": 180, "Id": f"/base/twin/cam{i}", "Num": i}
                for i, _ in enumerate(_cameras())]

    # -- configuration ------------------------------------------------------------------------
    def _config(self, use_case: str, main: dict | None, default_size: tuple[int, int], default_fmt: str,
                **kw: Any) -> dict:
        m = {"format": default_fmt, "size": default_size}
        m.update(main or {})
        m["size"] = tuple(m["size"])
        cfg = {"use_case": use_case, "main": m, "lores": kw.get("lores"), "raw": kw.get("raw"),
               "transform": kw.get("transform"), "controls": dict(kw.get("controls") or {}),
               "buffer_count": kw.get("buffer_count", 4)}
        return cfg

    def create_preview_configuration(self, main: dict | None = None, **kw: Any) -> dict:
        return self._config("preview", main, (640, 480), "XBGR8888", **kw)

    def create_still_configuration(self, main: dict | None = None, **kw: Any) -> dict:
        return self._config("still", main, _SENSOR, "BGR888", **kw)

    def create_video_configuration(self, main: dict | None = None, **kw: Any) -> dict:
        return self._config("video", main, (1280, 720), "XBGR8888", **kw)

    def configure(self, camera_config: Any = "preview") -> None:
        if isinstance(camera_config, str):
            camera_config = {"preview": self.create_preview_configuration,
                             "still": self.create_still_configuration,
                             "video": self.create_video_configuration}[camera_config]()
        if camera_config is None:
            camera_config = self.create_preview_configuration()
        self.camera_config = camera_config

    def set_controls(self, controls: dict) -> None:
        self.controls.update(controls)

    # -- lifecycle -----------------------------------------------------------------------------
    def start(self, config: Any = None, show_preview: Any = False) -> None:
        if config is not None:
            self.configure(config)
        if self.camera_config is None:
            self.configure("preview")
        self.started = True
        self._dev.set_running(True)

    def start_preview(self, *args: Any, **kwargs: Any) -> None:
        pass

    def stop_preview(self) -> None:
        pass

    def stop(self) -> None:
        self.started = False
        self._dev.set_running(False)

    def close(self) -> None:
        if self.started:
            self.stop()

    def __enter__(self) -> "Picamera2":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- capture -------------------------------------------------------------------------------
    def _stream(self, name: str) -> dict:
        if self.camera_config is None:
            self.configure("preview")
        assert self.camera_config is not None
        stream = self.camera_config.get(name) or self.camera_config["main"]
        return stream

    def _rgb(self, name: str) -> Any:
        stream = self._stream(name)
        return self._dev.frame(tuple(stream["size"]))

    def capture_array(self, name: str = "main", wait: Any = None) -> Any:
        import numpy as np

        rgb = self._rgb(name)
        fmt = str(self._stream(name).get("format", "XBGR8888")).upper()
        self._dev.note_capture()
        if fmt == "RGB888":
            return np.ascontiguousarray(rgb[:, :, ::-1])
        if fmt == "BGR888":
            return rgb
        alpha = np.full(rgb.shape[:2] + (1,), 255, dtype=np.uint8)
        if fmt in ("XRGB8888", "ARGB8888"):
            return np.concatenate([rgb[:, :, ::-1], alpha], axis=2)
        if fmt in ("XBGR8888", "ABGR8888"):
            return np.concatenate([rgb, alpha], axis=2)
        if fmt in ("YUV420", "YUV420P"):
            y = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
            h, w = y.shape
            return np.concatenate([y, np.full((h // 2, w), 128, dtype=np.uint8)], axis=0)
        raise ValueError(f"PiForge twin: pixel format {fmt!r} not supported")

    def capture_image(self, name: str = "main", wait: Any = None) -> Any:
        from PIL import Image

        img = Image.fromarray(self._rgb(name))
        self._dev.note_capture()
        return img

    def capture_file(self, file_output: Any, name: str = "main", format: str | None = None,  # noqa: A002
                     wait: Any = None, signal_function: Any = None) -> dict:
        from PIL import Image

        img = Image.fromarray(self._rgb(name))
        fmt = format
        if isinstance(file_output, (str, Path)):
            path = Path(file_output)
            if fmt is None and path.suffix.lower() in (".jpg", ".jpeg"):
                fmt = "JPEG"
            img.save(path, format=fmt)
            self._dev.note_capture(str(path))
        else:
            img.save(file_output, format=fmt or "JPEG")
            self._dev.note_capture()
        return self.capture_metadata()

    def capture_metadata(self, wait: Any = None) -> dict:
        return {"SensorTimestamp": int(time.monotonic() * 1e9), "ExposureTime": 10000, "AnalogueGain": 1.0,
                "DigitalGain": 1.0, "Lux": 400.0, "ColourTemperature": 5000, **self.controls}

    def capture_request(self, wait: Any = None) -> _Request:
        return _Request(self)

    def switch_mode_and_capture_file(self, camera_config: Any, file_output: Any, name: str = "main",
                                     format: str | None = None, **kw: Any) -> dict:  # noqa: A002
        prev = self.camera_config
        self.configure(camera_config)
        try:
            return self.capture_file(file_output, name=name, format=format)
        finally:
            self.camera_config = prev

    def switch_mode_and_capture_array(self, camera_config: Any, name: str = "main", **kw: Any) -> Any:
        prev = self.camera_config
        self.configure(camera_config)
        try:
            return self.capture_array(name)
        finally:
            self.camera_config = prev

    def start_and_capture_file(self, name: str = "image.jpg", delay: float = 1, show_preview: bool = True,
                               **kw: Any) -> None:
        self.configure("still")
        self.start()
        if delay:
            time.sleep(delay)
        self.capture_file(name)
        self.stop()

    def start_recording(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("PiForge twin: video recording/encoding is not simulated")

    start_encoder = start_recording
