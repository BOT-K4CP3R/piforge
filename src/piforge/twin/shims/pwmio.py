"""``pwmio`` shim (Blinka API): ``PWMOut`` with 16-bit duty cycle on any twin GPIO."""

from __future__ import annotations

from typing import Any

from piforge.twin.runtime import get_twin


class PWMOut:
    """``PWMOut(pin, *, duty_cycle=0, frequency=500, variable_frequency=False)``."""

    def __init__(self, pin: Any, *, duty_cycle: int = 0, frequency: int = 500,
                 variable_frequency: bool = False) -> None:
        self._bcm = int(getattr(pin, "id", pin))
        self._pi = get_twin().pi
        self._freq = int(frequency)
        self._duty = 0
        self.variable_frequency = variable_frequency
        self._pi.write(self._bcm, 0)
        self._pi.setup(self._bcm, "output")
        self.duty_cycle = duty_cycle

    def __enter__(self) -> "PWMOut":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.deinit()

    def deinit(self) -> None:
        """Stop PWM and release the pin."""
        self._pi.set_pwm(self._bcm, None, None)
        self._pi.setup(self._bcm, "input", pull="none")

    @property
    def duty_cycle(self) -> int:
        return self._duty

    @duty_cycle.setter
    def duty_cycle(self, value: int) -> None:
        if not 0 <= int(value) <= 65535:
            raise ValueError("Invalid duty_cycle value, should be between 0 and 65535")
        self._duty = int(value)
        self._pi.set_pwm(self._bcm, self._freq, self._duty / 65535.0)

    @property
    def frequency(self) -> int:
        return self._freq

    @frequency.setter
    def frequency(self, value: int) -> None:
        if int(value) <= 0:
            raise ValueError("Invalid frequency")
        self._freq = int(value)
        self._pi.set_pwm(self._bcm, self._freq, self._duty / 65535.0)
