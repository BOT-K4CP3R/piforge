"""gpiozero pin factory backed by the twin's :class:`~piforge.twin.gpio.VirtualPi`.

Install with ``Device.pin_factory = TwinFactory()`` (the runner does this before the firmware
starts), then unmodified gpiozero code — ``LED``, ``Button``, ``PWMLED``, ``Servo``/``AngularServo``,
``DistanceSensor``, ``MotionSensor``, ``LineSensor``, ``Buzzer``/``TonalBuzzer``, ``Motor``,
``RotaryEncoder``, ``MCP3008`` (hardware SPI0 or bit-banged), … — drives twin devices.

* Every GPIO supports PWM (like the RPi.GPIO/lgpio software PWM backends).
* Edge callbacks run on the twin's callback thread and receive the *simulated edge time* as
  ``ticks``, so pulse widths (HC-SR04 echo) are exact; ``ticks()`` is the twin clock in seconds.
* SPI on the hardware pins (SCLK 11, MOSI 10, MISO 9, CE0 8 / CE1 7) goes to the twin SPI0 bus;
  other pins use gpiozero's software (bit-banged) SPI over twin pins.
"""

from __future__ import annotations

from typing import Any

from gpiozero.exc import PinFixedPull, PinInvalidFunction, PinInvalidPull, PinSetInput
from gpiozero.mixins import SharedMixin
from gpiozero.pins import SPI
from gpiozero.pins.pi import PiFactory, PiPin, spi_port_device
from gpiozero.pins.spi import SPISoftware

from piforge.twin.runtime import Twin, get_twin

# Board → new-style revision code.  # src: raspberrypi.com/documentation/computers/raspberry-pi.html#new-style-revision-codes
REVISIONS = {"rpi4b": 0xC03111, "rpi5": 0xD04170, "rpi3bp": 0xA020D3, "rpizero2w": 0x902120}

_PULL_TO_PI = {"up": "up", "down": "down", "floating": "none"}


class TwinPin(PiPin):
    """One BCM GPIO of the virtual Pi."""

    def __init__(self, factory: "TwinFactory", info: Any) -> None:
        super().__init__(factory, info)
        self._bcm = int(info.name[4:])
        self._pi = factory.twin.pi
        self._function = self._pi.mode(self._bcm)
        self._pull = info.pull or "floating"
        self._state: float | bool = False
        self._freq: float | None = None
        self._bounce: float | None = None
        self._edges = "both"
        self._detect = False
        self._last_edge: float | None = None
        self._pi.add_listener(self._bcm, self._on_level)

    def close(self) -> None:
        self.when_changed = None
        self._pi.remove_listener(self._bcm, self._on_level)
        self._pi.set_pwm(self._bcm, None, None)
        self._pi.setup(self._bcm, "input", pull=_PULL_TO_PI.get(self._pull, "none"))
        self._function = "input"
        self._freq = None

    # -- function / state -----------------------------------------------------------------------
    def _get_function(self) -> str:
        return self._function

    def _set_function(self, value: str) -> None:
        if value not in ("input", "output"):
            raise PinInvalidFunction(f"invalid function {value!r} for pin {self!r}; use input or output")
        self._function = value
        if value == "input":
            self._freq = None
            self._pi.set_pwm(self._bcm, None, None)
        self._pi.setup(self._bcm, value, pull=_PULL_TO_PI.get(self._pull, "none"))

    def output_with_state(self, state: Any) -> None:
        self._state = bool(state)
        self._pi.write(self._bcm, 1 if state else 0)            # latch first: no glitch
        self._set_function("output")

    def input_with_pull(self, pull: str) -> None:
        self._function = "input"
        self._set_pull(pull)
        self._set_function("input")

    def _get_state(self) -> Any:
        if self._function == "output":
            return self._state
        return self._pi.read(self._bcm)

    def _set_state(self, value: Any) -> None:
        if self._function == "input":
            raise PinSetInput(f"cannot set state of pin {self!r}")
        if self._freq is not None:
            duty = max(0.0, min(1.0, float(value)))
            self._state = duty
            self._pi.set_pwm(self._bcm, self._freq, duty)
        else:
            self._state = bool(value)
            self._pi.write(self._bcm, 1 if value else 0)

    # -- pull / PWM ------------------------------------------------------------------------------
    def _get_pull(self) -> str:
        return self._pull

    def _set_pull(self, value: str) -> None:
        if self._function != "input":
            raise PinFixedPull(f"cannot set pull on non-input pin {self!r}")
        if self.info.pull and value != self.info.pull:
            raise PinFixedPull(f"{self!r} has a fixed pull resistor")
        if value not in _PULL_TO_PI:
            raise PinInvalidPull("pull must be floating, up, or down")
        self._pull = value
        self._pi.set_pull(self._bcm, _PULL_TO_PI[value])

    def _get_frequency(self) -> float | None:
        return self._freq

    def _set_frequency(self, value: float | None) -> None:
        if value is None:
            if self._freq is not None:
                self._freq = None
                self._state = False
                self._pi.set_pwm(self._bcm, None, None)
                self._pi.write(self._bcm, 0)
            return
        self._freq = float(value)
        duty = float(self._state) if not isinstance(self._state, bool) else (1.0 if self._state else 0.0)
        self._state = duty
        self._pi.set_pwm(self._bcm, self._freq, duty)

    # -- edges -----------------------------------------------------------------------------------
    def _get_bounce(self) -> float | None:
        return self._bounce

    def _set_bounce(self, value: float | None) -> None:
        self._bounce = None if value is None else float(value)

    def _get_edges(self) -> str:
        return self._edges

    def _set_edges(self, value: str) -> None:
        if value not in ("none", "falling", "rising", "both"):
            raise ValueError(f"invalid edges {value!r}")
        self._edges = value

    def _enable_event_detect(self) -> None:
        self._detect = True

    def _disable_event_detect(self) -> None:
        self._detect = False

    def _on_level(self, bcm: int, level: int, t: float) -> None:
        if not self._detect or self._function != "input" or self._edges == "none":
            return
        if (self._edges == "rising" and not level) or (self._edges == "falling" and level):
            return
        if self._bounce and self._last_edge is not None and t - self._last_edge < self._bounce:
            return
        self._last_edge = t
        self.factory.twin.dispatcher.submit(self._fire, t, level)

    def _fire(self, ticks: float, state: int) -> None:
        ref = self._when_changed               # read once: the firmware may clear it concurrently
        method = ref() if ref is not None else None
        if method is not None:
            method(ticks, state)


class TwinHardwareSPI(SPI):
    """gpiozero SPI interface on the twin's hardware SPI0 (``spidev``-style framed transfers)."""

    def __init__(self, clock_pin: Any, mosi_pin: Any, miso_pin: Any, select_pin: Any, *,
                 pin_factory: "TwinFactory") -> None:
        self._port, self._device = spi_port_device(clock_pin, mosi_pin, miso_pin, select_pin)
        self._bus = None
        super().__init__(pin_factory=pin_factory)
        to_reserve = {clock_pin, select_pin} | {p for p in (mosi_pin, miso_pin) if p is not None}
        self.pin_factory.reserve_pins(self, *to_reserve)
        self._bus = pin_factory.twin.pi.spi[self._port]
        self._mode = 0
        self._lsb_first = False
        self._select_high = False
        self._bits = 8
        self._rate = 500000

    def _conflicts_with(self, other: Any) -> bool:
        if isinstance(other, TwinHardwareSPI):
            return (self._port, self._device) == (other._port, other._device)
        return True

    def close(self) -> None:
        self._bus = None
        self.pin_factory.release_all(self)
        super().close()

    @property
    def closed(self) -> bool:
        return self._bus is None

    def __repr__(self) -> str:
        return f"SPI(port={self._port}, device={self._device})" if self._bus else "SPI(closed)"

    def transfer(self, data: Any) -> list[int]:
        self._check_open()
        words = [int(w) & 0xFF for w in data]
        if self._lsb_first:
            words = [int(f"{w:08b}"[::-1], 2) for w in words]
        out = list(self._bus.transfer(self._device, bytes(words)))  # type: ignore[union-attr]
        if self._lsb_first:
            out = [int(f"{w:08b}"[::-1], 2) for w in out]
        return out

    def _get_clock_mode(self) -> int:
        return self._mode

    def _set_clock_mode(self, value: int) -> None:
        self._mode = int(value) & 3

    def _get_lsb_first(self) -> bool:
        return self._lsb_first

    def _set_lsb_first(self, value: bool) -> None:
        self._lsb_first = bool(value)

    def _get_select_high(self) -> bool:
        return self._select_high

    def _set_select_high(self, value: bool) -> None:
        self._select_high = bool(value)

    def _get_bits_per_word(self) -> int:
        return self._bits

    def _set_bits_per_word(self, value: int) -> None:
        if int(value) != 8:
            raise ValueError("the PiForge twin's hardware SPI supports 8 bits per word")
        self._bits = 8

    def _get_rate(self) -> int:
        return self._rate

    def _set_rate(self, value: int) -> None:
        self._rate = int(value)


class TwinHardwareSPIShared(SharedMixin, TwinHardwareSPI):
    @classmethod
    def _shared_key(cls, clock_pin, mosi_pin, miso_pin, select_pin, pin_factory):  # noqa: ANN001,ANN206
        return (clock_pin, select_pin)


class TwinSoftwareSPI(SPISoftware):
    """Bit-banged SPI over twin pins (gpiozero's own implementation)."""


class TwinSoftwareSPIShared(SharedMixin, TwinSoftwareSPI):
    @classmethod
    def _shared_key(cls, clock_pin, mosi_pin, miso_pin, select_pin, pin_factory):  # noqa: ANN001,ANN206
        return (clock_pin, select_pin)


class TwinFactory(PiFactory):
    """gpiozero ``Factory`` whose pins live on the (current or given) :class:`~piforge.twin.runtime.Twin`."""

    def __init__(self, twin: Twin | None = None) -> None:
        super().__init__()
        self._twin = twin
        self.pin_class = TwinPin

    @property
    def twin(self) -> Twin:
        """The twin these pins belong to."""
        return self._twin if self._twin is not None else get_twin()

    def _get_revision(self) -> int:
        return REVISIONS.get(self.twin.config.board, REVISIONS["rpi4b"])

    def _get_spi_class(self, shared: bool, hardware: bool) -> type:
        if hardware:
            return TwinHardwareSPIShared if shared else TwinHardwareSPI
        return TwinSoftwareSPIShared if shared else TwinSoftwareSPI

    def ticks(self) -> float:
        """Twin clock (s)."""
        return self.twin.clock.now()

    def ticks_diff(self, later: float, earlier: float) -> float:
        """Seconds between two :meth:`ticks` values."""
        return later - earlier
