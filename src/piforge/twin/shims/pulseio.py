"""``pulseio`` shim: pulse capture/generation is not simulated by the twin (clear error on use)."""

from __future__ import annotations

from typing import Any


class PulseIn:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("pulseio.PulseIn is not simulated by the PiForge twin; DHT sensors are "
                                  "supported through the adafruit_dht shim (twin device 'dht22').")


class PulseOut:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("pulseio.PulseOut is not simulated by the PiForge twin.")
