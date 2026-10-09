"""Minimal ``libcamera`` Python bindings stand-in (``Transform``, ``ColorSpace``, ``controls``) for picamera2 code."""

from __future__ import annotations

from types import SimpleNamespace


class Transform:
    def __init__(self, hflip: int = 0, vflip: int = 0, transpose: int = 0) -> None:
        self.hflip, self.vflip, self.transpose = bool(hflip), bool(vflip), bool(transpose)

    def __repr__(self) -> str:
        return f"<libcamera.Transform hflip={int(self.hflip)} vflip={int(self.vflip)}>"


class ColorSpace:
    def __init__(self, name: str = "Sycc") -> None:
        self.name = name

    @staticmethod
    def Sycc() -> "ColorSpace":  # noqa: N802
        return ColorSpace("Sycc")

    @staticmethod
    def Smpte170m() -> "ColorSpace":  # noqa: N802
        return ColorSpace("Smpte170m")

    @staticmethod
    def Rec709() -> "ColorSpace":  # noqa: N802
        return ColorSpace("Rec709")


controls = SimpleNamespace(
    AfModeEnum=SimpleNamespace(Manual=0, Auto=1, Continuous=2),
    AeExposureModeEnum=SimpleNamespace(Normal=0, Short=1, Long=2, Custom=3),
    AwbModeEnum=SimpleNamespace(Auto=0, Incandescent=1, Tungsten=2, Fluorescent=3, Indoor=4, Daylight=5,
                                Cloudy=6, Custom=7),
    draft=SimpleNamespace(NoiseReductionModeEnum=SimpleNamespace(Off=0, Fast=1, HighQuality=2)),
)
