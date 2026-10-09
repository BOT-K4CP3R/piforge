"""``picamera2.encoders`` stand-ins: importable, but video encoding is not simulated."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Quality(Enum):
    VERY_LOW = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    VERY_HIGH = 4


class Encoder:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs


class H264Encoder(Encoder):
    pass


class MJPEGEncoder(Encoder):
    pass


class JpegEncoder(Encoder):
    pass
