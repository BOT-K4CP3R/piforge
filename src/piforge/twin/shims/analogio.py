"""``analogio`` shim: the Raspberry Pi has no analog inputs — explain instead of failing obscurely."""

from __future__ import annotations

from typing import Any


class AnalogIn:
    def __init__(self, pin: Any) -> None:
        raise NotImplementedError("The Raspberry Pi has no analog inputs (analogio.AnalogIn). Use an ADC such "
                                  "as an MCP3008 (twin device 'mcp3008') or ADS1115.")


class AnalogOut:
    def __init__(self, pin: Any) -> None:
        raise NotImplementedError("The Raspberry Pi has no DAC (analogio.AnalogOut); use PWM (pwmio) instead.")
