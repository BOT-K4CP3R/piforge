"""``RPi.GPIO`` shim backed by the PiForge digital twin.

Same constants, functions, error messages and callback threading model as RPi.GPIO 0.7.x:
callbacks run on one background thread, ``bouncetime`` is in milliseconds, ``wait_for_edge``
``timeout`` in milliseconds, PWM duty cycle in percent.
"""

from __future__ import annotations

import threading
import warnings
import weakref
from collections.abc import Callable
from typing import Any

from piforge.twin.gpio import GPIO_COUNT, PHYS_TO_BCM
from piforge.twin.runtime import get_twin

# Constants (values identical to RPi.GPIO).  # src: RPi.GPIO 0.7.1 source, source/py_gpio.c / c_gpio.h
VERSION = "0.7.1"
BOARD = 10
BCM = 11
OUT = 0
IN = 1
LOW = 0
HIGH = 1
PUD_OFF = 20
PUD_DOWN = 21
PUD_UP = 22
RISING = 31
FALLING = 32
BOTH = 33
HARD_PWM = 43
SERIAL = 40
SPI = 41
I2C = 42
UNKNOWN = -1
RPI_REVISION = 3
RPI_INFO = {"P1_REVISION": 3, "REVISION": "c03111", "TYPE": "Pi 4 Model B", "MANUFACTURER": "Sony",
            "PROCESSOR": "BCM2711", "RAM": "4G"}

_PULL = {PUD_OFF: "none", PUD_UP: "up", PUD_DOWN: "down"}
_lock = threading.RLock()
_mode: int | None = None
_warnings = True
_dirs: dict[int, int] = {}                     # bcm → IN/OUT for channels set up by this module
_pwms: "weakref.WeakValueDictionary[int, PWM]" = weakref.WeakValueDictionary()  # like the C ext: GC stops PWM
_edges: dict[int, "_EdgeDetect"] = {}


def _pi() -> Any:
    return get_twin().pi


def _bcm(channel: Any) -> int:
    if _mode is None:
        raise RuntimeError("Please set pin numbering mode using GPIO.setmode(GPIO.BOARD) or GPIO.setmode(GPIO.BCM)")
    if isinstance(channel, bool) or not isinstance(channel, int):
        raise ValueError("Channel must be an integer or list/tuple of integers")
    if _mode == BCM:
        if 0 <= channel < GPIO_COUNT:
            return channel
    elif channel in PHYS_TO_BCM:
        return PHYS_TO_BCM[channel]
    raise ValueError("The channel sent is invalid on a Raspberry Pi")


def _channels(channel: Any) -> list[Any]:
    return list(channel) if isinstance(channel, (list, tuple)) else [channel]


def setmode(mode: int) -> None:
    """Choose BOARD (physical) or BCM numbering."""
    global _mode
    if mode not in (BOARD, BCM):
        raise ValueError("An invalid mode was passed to setmode()")
    if _mode is not None and mode != _mode:
        raise ValueError("A different mode has already been set!")
    _mode = mode


def getmode() -> int | None:
    """Current numbering mode or None."""
    return _mode


def setwarnings(flag: bool) -> None:
    """Enable/disable RuntimeWarnings."""
    global _warnings
    _warnings = bool(flag)


def setup(channel: Any, direction: int, pull_up_down: int = PUD_OFF, initial: int = -1) -> None:
    """Configure one channel or a list of channels."""
    if direction not in (IN, OUT):
        raise ValueError("An invalid direction was passed to setup()")
    if direction == OUT and pull_up_down != PUD_OFF:
        raise ValueError("pull_up_down parameter is not valid for outputs")
    if pull_up_down not in _PULL:
        raise ValueError("Invalid value for pull_up_down - should be either PUD_OFF, PUD_UP or PUD_DOWN")
    pi = _pi()
    with _lock:
        for ch in _channels(channel):
            bcm = _bcm(ch)
            if _warnings and bcm not in _dirs and pi.mode(bcm) == "output":
                warnings.warn("This channel is already in use, continuing anyway.  "
                              "Use GPIO.setwarnings(False) to disable warnings.", RuntimeWarning, stacklevel=2)
            if direction == OUT:
                if initial != -1:
                    pi.write(bcm, 1 if initial else 0)
                pi.setup(bcm, "output")
            else:
                pi.setup(bcm, "input", pull=_PULL[pull_up_down])
            _dirs[bcm] = direction


def output(channel: Any, value: Any) -> None:
    """Drive output channel(s); ``value`` may be a list matching ``channel``."""
    chans = _channels(channel)
    vals = list(value) if isinstance(value, (list, tuple)) else [value] * len(chans)
    if len(vals) != len(chans):
        raise RuntimeError("Number of channels != number of values")
    pi = _pi()
    for ch, v in zip(chans, vals):
        bcm = _bcm(ch)
        if _dirs.get(bcm) != OUT:
            raise RuntimeError("The GPIO channel has not been set up as an OUTPUT")
        pi.write(bcm, 1 if v else 0)


def input(channel: int) -> int:  # noqa: A001 - RPi.GPIO API name
    """Read a channel (inputs and outputs)."""
    bcm = _bcm(channel)
    if bcm not in _dirs:
        raise RuntimeError("You must setup() the GPIO channel first")
    return HIGH if _pi().read(bcm) else LOW


def gpio_function(channel: int) -> int:
    """IN, OUT (or HARD_PWM/SPI/I2C on real hardware)."""
    return OUT if _pi().mode(_bcm(channel)) == "output" else IN


def cleanup(channel: Any = None) -> None:
    """Reset channels set up by this program to inputs without pull."""
    global _mode
    pi = _pi()
    with _lock:
        if channel is None:
            targets = list(_dirs)
            if not targets and _warnings:
                warnings.warn("No channels have been set up yet - nothing to clean up!  "
                              "Try cleaning up at the end of your program instead!", RuntimeWarning, stacklevel=2)
        else:
            targets = [_bcm(c) for c in _channels(channel)]
        for bcm in targets:
            if bcm in _edges:
                _edges.pop(bcm).remove()
            pwm = _pwms.pop(bcm, None)
            if pwm is not None:
                pwm.stop()
            pi.set_pwm(bcm, None, None)
            pi.setup(bcm, "input", pull="none")
            _dirs.pop(bcm, None)
        if channel is None:
            _mode = None


# --- edge detection ---------------------------------------------------------------------------
class _EdgeDetect:
    def __init__(self, bcm: int, channel: int, edge: int, bouncetime: int | None) -> None:
        self.bcm, self.channel, self.edge = bcm, channel, edge
        self.bounce = (bouncetime or 0) / 1000.0
        self.callbacks: list[Callable[[int], Any]] = []
        self.flag = False
        self.last: float | None = None
        _pi().add_listener(bcm, self._on_level)

    def _on_level(self, bcm: int, level: int, t: float) -> None:
        if (self.edge == RISING and not level) or (self.edge == FALLING and level):
            return
        if self.bounce and self.last is not None and t - self.last < self.bounce:
            return
        self.last = t
        self.flag = True
        twin = get_twin()
        for cb in list(self.callbacks):
            twin.dispatcher.submit(cb, self.channel)

    def remove(self) -> None:
        _pi().remove_listener(self.bcm, self._on_level)


def _check_edge(edge: int) -> None:
    if edge not in (RISING, FALLING, BOTH):
        raise ValueError("The edge must be set to RISING, FALLING or BOTH")


def add_event_detect(channel: int, edge: int, callback: Callable[[int], Any] | None = None,
                     bouncetime: int | None = None) -> None:
    """Start edge detection; ``callback(channel)`` runs on the callback thread."""
    _check_edge(edge)
    bcm = _bcm(channel)
    if _dirs.get(bcm) != IN:
        raise RuntimeError("You must setup() the GPIO channel as an input first")
    if bouncetime is not None and bouncetime <= 0:
        raise ValueError("Bouncetime must be greater than 0")
    with _lock:
        if bcm in _edges:
            raise RuntimeError("Conflicting edge detection already enabled for this GPIO channel")
        det = _EdgeDetect(bcm, channel, edge, bouncetime)
        if callback is not None:
            det.callbacks.append(callback)
        _edges[bcm] = det


def remove_event_detect(channel: int) -> None:
    """Stop edge detection on a channel."""
    bcm = _bcm(channel)
    with _lock:
        det = _edges.pop(bcm, None)
        if det is not None:
            det.remove()


def add_event_callback(channel: int, callback: Callable[[int], Any]) -> None:
    """Add another callback to an existing edge detection."""
    bcm = _bcm(channel)
    if bcm not in _edges:
        raise RuntimeError("Add event detection using add_event_detect first before adding a callback")
    _edges[bcm].callbacks.append(callback)


def event_detected(channel: int) -> bool:
    """True once per detected edge since the last call."""
    det = _edges.get(_bcm(channel))
    if det is None:
        return False
    hit, det.flag = det.flag, False
    return hit


def wait_for_edge(channel: int, edge: int, bouncetime: int | None = None, timeout: int | None = None) -> int | None:
    """Block until an edge (returns the channel) or ``timeout`` ms (returns None)."""
    _check_edge(edge)
    bcm = _bcm(channel)
    if _dirs.get(bcm) != IN:
        raise RuntimeError("You must setup() the GPIO channel as an input first")
    if bcm in _edges:
        raise RuntimeError("Conflicting edge detection for this GPIO channel")
    if timeout is not None and timeout <= 0:
        raise ValueError("Timeout must be greater than 0")
    hit = threading.Event()

    def on(b: int, level: int, t: float) -> None:
        if (edge == RISING and not level) or (edge == FALLING and level):
            return
        hit.set()

    pi = _pi()
    pi.add_listener(bcm, on)
    try:
        ok = hit.wait(None if timeout is None else timeout / 1000.0)
    finally:
        pi.remove_listener(bcm, on)
    return channel if ok else None


# --- PWM ------------------------------------------------------------------------------------------
class PWM:
    """Software PWM: ``PWM(channel, frequency)``; duty cycle 0..100 %."""

    def __init__(self, channel: int, frequency: float) -> None:
        bcm = _bcm(channel)
        if _dirs.get(bcm) != OUT:
            raise RuntimeError("You must setup() the GPIO channel as an output first")
        if frequency <= 0.0:
            raise ValueError("frequency must be greater than 0.0")
        with _lock:
            if bcm in _pwms:
                raise RuntimeError("A PWM object already exists for this GPIO channel")
            _pwms[bcm] = self
        self._bcm = bcm
        self._freq = float(frequency)
        self._dc = 0.0
        self._running = False

    @staticmethod
    def _check_dc(dc: float) -> float:
        if not 0.0 <= dc <= 100.0:
            raise ValueError("dutycycle must have a value from 0.0 to 100.0")
        return float(dc)

    def start(self, dutycycle: float) -> None:
        self._dc = self._check_dc(dutycycle)
        self._running = True
        _pi().set_pwm(self._bcm, self._freq, self._dc / 100.0)

    def ChangeDutyCycle(self, dutycycle: float) -> None:  # noqa: N802 - RPi.GPIO API name
        self._dc = self._check_dc(dutycycle)
        if self._running:
            _pi().set_pwm(self._bcm, self._freq, self._dc / 100.0)

    def ChangeFrequency(self, frequency: float) -> None:  # noqa: N802 - RPi.GPIO API name
        if frequency <= 0.0:
            raise ValueError("frequency must be greater than 0.0")
        self._freq = float(frequency)
        if self._running:
            _pi().set_pwm(self._bcm, self._freq, self._dc / 100.0)

    def stop(self) -> None:
        if self._running:
            self._running = False
            pi = _pi()
            pi.set_pwm(self._bcm, None, None)
            pi.write(self._bcm, 0)

    def __del__(self) -> None:
        try:
            self.stop()
            with _lock:
                if _pwms.get(self._bcm) is self:
                    del _pwms[self._bcm]
        except Exception:
            pass
