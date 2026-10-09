"""``picamera2.outputs`` stand-ins: importable, but recording is not simulated."""

from __future__ import annotations

from typing import Any


class Output:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs


class FileOutput(Output):
    pass


class FfmpegOutput(Output):
    pass


class CircularOutput(Output):
    pass
