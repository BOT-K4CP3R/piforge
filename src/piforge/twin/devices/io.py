"""Digital I/O devices: contacts, IR break-beam, PIR, LEDs, buzzer, relay, rotary encoder."""

from __future__ import annotations

import random
from typing import Any, ClassVar

from piforge.twin.devices.base import Device, PropSpec, hexcolor, register

_SIG_ALIASES = {"sig": "pin", "signal": "pin", "s": "pin", "out": "pin", "gpio": "pin", "in": "pin",
                "data": "pin", "din": "pin", "dq": "pin", "io": "pin"}


class _Contact(Device):
    """A mechanical contact between the GPIO and GND (``active_low``) or 3V3.

    Params: ``active_low`` (True: closing pulls the line LOW — use a pull-up), ``bounce_ms``
    (simulated contact bounce after each change; 0 = clean edges), ``normally_closed``.
    """

    contact_input: ClassVar[str] = "pressed"
    pin_roles = ("pin",)
    pin_aliases = dict(_SIG_ALIASES, **{"no": "pin", "nc": "pin", "com": "pin"})
    defaults: ClassVar[dict[str, Any]] = {"active_low": True, "bounce_ms": 0.0, "normally_closed": False}
    example = {"pins": {"pin": 27}}

    def setup(self) -> None:
        self._gen = 0
        self._rng = random.Random(sum(map(ord, self.id)))
        self._closed_now = False
        self._set_closed(self._contact_closed(), bounce=False)

    def _contact_closed(self) -> bool:
        return bool(self._inputs[self.contact_input]) != bool(self.params.get("normally_closed"))

    def _level(self, closed: bool) -> int | None:
        if not closed:
            return None
        return 0 if self.params.get("active_low", True) else 1

    def _set_closed(self, closed: bool, *, bounce: bool, t: float | None = None) -> None:
        self._gen += 1
        gen = self._gen
        pin = self.pins["pin"]
        self._closed_now = closed
        self.pi.drive(pin, self.id, self._level(closed), t=t)
        width = float(self.params.get("bounce_ms") or 0.0) / 1000.0
        if not bounce or width <= 0:
            return
        t0 = self.pi.clock.now() if t is None else t
        times = sorted(self._rng.uniform(0.0, width) for _ in range(2 * self._rng.randint(1, 3)))
        state = closed
        for dt in times:                         # even number of toggles → ends where it started
            state = not state
            at = t0 + dt

            def flip(lv=self._level(state), at=at) -> None:
                if self._gen == gen:
                    self.pi.drive(pin, self.id, lv, t=at)
            self.pi.call_at(at, flip)

    def on_input(self, prop: str, value: Any) -> None:
        if prop == self.contact_input:
            self._set_closed(self._contact_closed(), bounce=True)

    def outputs_state(self) -> dict:
        return {"closed": self._closed_now}


@register
class Button(_Contact):
    """Momentary push button (tact switch)."""

    type = "button"
    label = "Push button"
    inputs = {"pressed": PropSpec("bool", default=False, label="Pressed", widget="momentary")}
    outputs = {"closed": PropSpec("bool", default=False, label="Contact closed")}


@register
class Switch(_Contact):
    """Latching SPST switch (toggle/rocker/slide)."""

    type = "switch"
    label = "Switch"
    contact_input = "on"
    inputs = {"on": PropSpec("bool", default=False, label="On", widget="toggle")}
    outputs = {"closed": PropSpec("bool", default=False, label="Contact closed")}


@register
class LimitSwitch(_Contact):
    """Micro/limit switch; set ``normally_closed`` for NC wiring."""

    type = "limit_switch"
    label = "Limit switch"
    inputs = {"pressed": PropSpec("bool", default=False, label="Actuated", widget="toggle")}
    outputs = {"closed": PropSpec("bool", default=False, label="Contact closed")}


@register
class IRBreakBeam(Device):
    """IR break-beam receiver (open-collector): output pulled LOW while the beam is broken.

    # src: Adafruit IR break beam guide — "if the beam is broken, the sensorState is LOW" (needs pull-up)
    Param ``active_low=False`` models push-pull modules that output HIGH when broken.
    """

    type = "ir_breakbeam"
    label = "IR break-beam"
    pin_roles = ("pin",)
    pin_aliases = _SIG_ALIASES
    defaults = {"active_low": True}
    inputs = {"broken": PropSpec("bool", default=False, label="Beam broken", widget="toggle")}
    outputs = {"output": PropSpec("bool", default=True, label="Receiver output high")}
    example = {"pins": {"pin": 13}}

    def setup(self) -> None:
        self._apply()

    def _apply(self) -> None:
        broken = bool(self._inputs["broken"])
        if self.params.get("active_low", True):
            self.pi.drive(self.pins["pin"], self.id, 0 if broken else None)
        else:
            self.pi.drive(self.pins["pin"], self.id, 1 if broken else 0)

    def on_input(self, prop: str, value: Any) -> None:
        self._apply()

    def outputs_state(self) -> dict:
        return {"output": bool(self.pi.effective(self.pins["pin"]) >= 0.5)}


@register
class PIR(Device):
    """HC-SR501 PIR motion sensor: push-pull output HIGH while motion, held ``hold_s`` after it.

    # src: HC-SR501 datasheet — time-delay pot ≈ 3 s … 300 s, repeat-trigger ("H") mode
    """

    type = "pir"
    label = "PIR motion sensor"
    pin_roles = ("pin",)
    pin_aliases = _SIG_ALIASES
    defaults = {"hold_s": 3.0}
    inputs = {"motion": PropSpec("bool", default=False, label="Motion", widget="momentary")}
    outputs = {"active": PropSpec("bool", default=False, label="Output high")}
    example = {"pins": {"pin": 4}}

    def setup(self) -> None:
        self._gen = 0
        self._active = False
        self.pi.drive(self.pins["pin"], self.id, 0)

    def on_input(self, prop: str, value: Any) -> None:
        self._gen += 1
        if value:
            self._active = True
            self.pi.drive(self.pins["pin"], self.id, 1)
            return
        gen = self._gen
        at = self.pi.clock.now() + float(self.params.get("hold_s", 3.0))

        def off() -> None:
            if self._gen == gen:
                self._active = False
                self.pi.drive(self.pins["pin"], self.id, 0, t=at)
        self.pi.call_at(at, off)

    def outputs_state(self) -> dict:
        return {"active": self._active}


@register
class LED(Device):
    """LED on a GPIO (``active_high``: GPIO → resistor → LED → GND). Brightness = PWM duty."""

    type = "led"
    label = "LED"
    pin_roles = ("pin",)
    pin_aliases = dict(_SIG_ALIASES, **{"a": "pin", "anode": "pin", "k": "pin", "cathode": "pin"})
    defaults = {"active_high": True, "color": "red"}
    outputs = {"brightness": PropSpec("float", 0.0, 1.0, default=0.0, label="Brightness"),
               "on": PropSpec("bool", default=False, label="On"),
               "color": PropSpec("text", default="red", label="Colour")}
    example = {"pins": {"pin": 17}}

    def _brightness(self) -> float:
        bcm = self.pins["pin"]
        if self.pi.mode(bcm) != "output":
            return 0.0
        v = self.pi.effective(bcm)
        return v if self.params.get("active_high", True) else 1.0 - v

    def outputs_state(self) -> dict:
        b = round(self._brightness(), 4)
        return {"brightness": b, "on": b > 0.0, "color": self.params.get("color", "red")}


@register
class RGBLED(Device):
    """Common-cathode (default) or common-anode RGB LED on three GPIOs."""

    type = "rgb_led"
    label = "RGB LED"
    pin_roles = ("red", "green", "blue")
    pin_aliases = {"r": "red", "g": "green", "b": "blue"}
    defaults = {"common_anode": False}
    outputs = {"red": PropSpec("float", 0.0, 1.0, default=0.0), "green": PropSpec("float", 0.0, 1.0, default=0.0),
               "blue": PropSpec("float", 0.0, 1.0, default=0.0),
               "color": PropSpec("text", default="#000000", label="Colour")}
    example = {"pins": {"red": 5, "green": 6, "blue": 13}}

    def _chan(self, role: str) -> float:
        bcm = self.pins[role]
        if self.pi.mode(bcm) != "output":
            return 0.0
        v = self.pi.effective(bcm)
        return 1.0 - v if self.params.get("common_anode") else v

    def outputs_state(self) -> dict:
        r, g, b = (round(self._chan(c), 4) for c in ("red", "green", "blue"))
        return {"red": r, "green": g, "blue": b, "color": hexcolor(r, g, b)}


@register
class Buzzer(Device):
    """Active buzzer (sounds while HIGH) or passive buzzer (``active=False``: tone = PWM frequency).

    # src: typical 12 mm active buzzer datasheets — resonant frequency 2300 ± 300 Hz
    """

    type = "buzzer"
    label = "Buzzer"
    pin_roles = ("pin",)
    pin_aliases = dict(_SIG_ALIASES, **{"+": "pin", "pos": "pin"})
    defaults = {"active": True, "active_high": True, "frequency": 2300.0}
    outputs = {"on": PropSpec("bool", default=False, label="Sounding"),
               "frequency": PropSpec("float", 0.0, 20000.0, unit="Hz", default=0.0, label="Tone")}
    example = {"pins": {"pin": 26}}

    def outputs_state(self) -> dict:
        bcm = self.pins["pin"]
        if self.pi.mode(bcm) != "output":
            return {"on": False, "frequency": 0.0}
        freq, duty = self.pi.pwm(bcm)
        if self.params.get("active", True):
            v = self.pi.effective(bcm)
            on = (v > 0.0) if self.params.get("active_high", True) else (v < 1.0)
            return {"on": on, "frequency": float(self.params.get("frequency", 2300.0)) if on else 0.0}
        on = freq is not None and duty is not None and 0.0 < duty < 1.0
        return {"on": on, "frequency": round(freq, 3) if on and freq else 0.0}


@register
class Relay(Device):
    """Relay module; ``active_high=False`` for the common opto-isolated active-LOW boards."""

    type = "relay"
    label = "Relay"
    pin_roles = ("pin",)
    pin_aliases = _SIG_ALIASES
    defaults = {"active_high": True}
    outputs = {"on": PropSpec("bool", default=False, label="Energised"),
               "switch_count": PropSpec("int", 0, None, default=0, label="Switching cycles")}
    example = {"pins": {"pin": 5}}

    def setup(self) -> None:
        self._count = 0
        self._last = self._on()
        self.listen(self.pins["pin"], self._changed)

    def _on(self) -> bool:
        bcm = self.pins["pin"]
        if self.pi.mode(bcm) != "output":
            return False
        v = self.pi.effective(bcm) >= 0.5
        return v if self.params.get("active_high", True) else not v

    def _changed(self, bcm: int, level: int, t: float) -> None:
        now = self._on()
        if now != self._last:
            self._last = now
            if now:
                self._count += 1

    def outputs_state(self) -> dict:
        return {"on": self._on(), "switch_count": self._count}


@register
class RotaryEncoder(Device):
    """KY-040-style incremental encoder: contacts A/B (and push switch SW) to GND.

    One detent = one full quadrature cycle; clockwise means A leads B (gpiozero convention).
    Input ``steps`` is *relative* (+n clockwise / −n counter-clockwise detents). The module's
    10 kΩ pull-ups are modelled as weak drivers (param ``pullups``).
    """

    type = "rotary_encoder"
    label = "Rotary encoder"
    pin_roles = ("a", "b")
    optional_pins = ("sw",)
    pin_aliases = {"clk": "a", "dt": "b", "s1": "a", "s2": "b", "key": "sw", "button": "sw",
                   "switch": "sw"}
    defaults = {"edge_interval": 0.005, "pullups": True}
    inputs = {"steps": PropSpec("int", -1000, 1000, default=0, label="Rotate (detents)", widget="stepper"),
              "pressed": PropSpec("bool", default=False, label="Push", widget="momentary")}
    outputs = {"position": PropSpec("int", default=0, label="Position (detents)")}
    example = {"pins": {"a": 20, "b": 21, "sw": 16}}
    # (A closed, B closed) along one clockwise detent, starting from the idle detent position
    _CW = [(True, False), (True, True), (False, True), (False, False)]

    def setup(self) -> None:
        self._position = 0
        self._busy_until = 0.0
        self._contact("a", False)
        self._contact("b", False)
        if "sw" in self.pins:
            self._contact("sw", False)

    def _contact(self, role: str, closed: bool, t: float | None = None) -> None:
        bcm = self.pins[role]
        if closed:
            self.pi.drive(bcm, self.id, 0, t=t)
        elif self.params.get("pullups", True):
            self.pi.drive(bcm, self.id, 1, weak=True, t=t)
        else:
            self.pi.drive(bcm, self.id, None, t=t)

    def on_input(self, prop: str, value: Any) -> None:
        if prop == "pressed" and "sw" in self.pins:
            self._contact("sw", bool(value))
            return
        if prop != "steps" or not value:
            return
        step = float(self.params.get("edge_interval", 0.005))
        t = max(self.pi.clock.now(), self._busy_until)
        seq = self._CW if value > 0 else [(b, a) for a, b in self._CW]   # CCW: B leads
        for _ in range(abs(int(value))):
            for i, (a, b) in enumerate(seq):
                t += step

                def apply(a=a, b=b, at=t, last=(i == 3), d=(1 if value > 0 else -1)) -> None:
                    self._contact("a", a, at)
                    self._contact("b", b, at)
                    if last:
                        self._position += d
                self.pi.call_at(t, apply)
        self._busy_until = t

    def outputs_state(self) -> dict:
        return {"position": self._position}
