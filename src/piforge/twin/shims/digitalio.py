"""``digitalio`` shim (Blinka API): ``DigitalInOut`` on twin GPIOs."""

from __future__ import annotations

from typing import Any

from piforge.twin.runtime import get_twin


class _Enum:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


class Direction:
    INPUT = _Enum("digitalio.Direction.INPUT")
    OUTPUT = _Enum("digitalio.Direction.OUTPUT")


class Pull:
    UP = _Enum("digitalio.Pull.UP")
    DOWN = _Enum("digitalio.Pull.DOWN")


class DriveMode:
    PUSH_PULL = _Enum("digitalio.DriveMode.PUSH_PULL")
    OPEN_DRAIN = _Enum("digitalio.DriveMode.OPEN_DRAIN")


def _pull_name(pull: Any) -> str:
    return "up" if pull is Pull.UP else "down" if pull is Pull.DOWN else "none"


class DigitalInOut:
    """A GPIO as digital input or output (starts as input without pull)."""

    def __init__(self, pin: Any) -> None:
        self._bcm = int(getattr(pin, "id", pin))
        self._pi = get_twin().pi
        self._direction = Direction.INPUT
        self._pull: Any = None
        self._drive = DriveMode.PUSH_PULL
        self._value = False
        self._pi.setup(self._bcm, "input", pull="none")

    def __enter__(self) -> "DigitalInOut":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.deinit()

    def deinit(self) -> None:
        """Return the pin to a floating input."""
        self._pi.setup(self._bcm, "input", pull="none")

    def switch_to_output(self, value: bool = False, drive_mode: Any = DriveMode.PUSH_PULL) -> None:
        """Make the pin an output with an initial ``value`` (latched first: no glitch)."""
        self._direction = Direction.OUTPUT
        self._drive = drive_mode
        self._pull = None
        self._set(bool(value))

    def switch_to_input(self, pull: Any = None) -> None:
        """Make the pin an input with optional ``Pull.UP``/``Pull.DOWN``."""
        self._direction = Direction.INPUT
        self._pull = pull
        self._pi.setup(self._bcm, "input", pull=_pull_name(pull))

    def _set(self, value: bool) -> None:
        self._value = value
        if self._drive is DriveMode.OPEN_DRAIN and value:
            self._pi.setup(self._bcm, "input", pull="none")          # released (hi-Z)
            return
        self._pi.write(self._bcm, 1 if value else 0)
        self._pi.setup(self._bcm, "output")

    @property
    def direction(self) -> Any:
        return self._direction

    @direction.setter
    def direction(self, value: Any) -> None:
        if value is Direction.OUTPUT:
            self.switch_to_output()
        elif value is Direction.INPUT:
            self.switch_to_input()
        else:
            raise AttributeError("Not a Direction")

    @property
    def value(self) -> bool:
        if self._direction is Direction.OUTPUT:
            return self._value
        return bool(self._pi.read(self._bcm))

    @value.setter
    def value(self, val: Any) -> None:
        if self._direction is not Direction.OUTPUT:
            raise AttributeError("Cannot set value when direction is input.")
        self._set(bool(val))

    @property
    def pull(self) -> Any:
        if self._direction is Direction.OUTPUT:
            raise AttributeError("Pull not used when direction is output.")
        return self._pull

    @pull.setter
    def pull(self, pul: Any) -> None:
        if self._direction is Direction.OUTPUT:
            raise AttributeError("Pull not used when direction is output.")
        self.switch_to_input(pul)

    @property
    def drive_mode(self) -> Any:
        if self._direction is Direction.INPUT:
            raise AttributeError("Drive mode not used when direction is input.")
        return self._drive

    @drive_mode.setter
    def drive_mode(self, mode: Any) -> None:
        self._drive = mode
        self._set(self._value)
