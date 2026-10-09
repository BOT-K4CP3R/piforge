"""Split-flap digit module: a flap spool turned by a ``stepper_28byj48``, homed by a Hall sensor.

Geometry (all angles in degrees, spool frame):

* spool angle ``angle = (start_angle + direction · 360 · position / steps_per_rev) mod 360`` where
  ``position`` is the stepper's half-step count;
* the magnet reaches the Hall sensor at ``home_angle`` and stays over it for ``hall_window`` degrees
  of forward rotation: while ``(angle − home_angle) mod 360 < hall_window`` the A3144-type open-collector
  output pulls ``hall_pin`` LOW (otherwise it is released and the Pi pull-up reads HIGH). Turning
  forwards, the falling Hall edge is therefore exactly at ``home_angle``;
* ``flaps`` flaps (20 = digits 0–9 twice, 18° each). Flap 0 (digit 0) is fully shown when the spool
  is ``offset_steps`` half-steps past the Hall edge, i.e. "rotate until the Hall edge, then apply
  the module's offset" homes the digit to 0; flap ``k`` shows digit ``k mod 10``.

Flip model: each time the spool crosses a flap boundary the next flap is released and falls in
``flip_time`` seconds (≈ 0.08 s). While it falls ``digit`` is the old digit, ``next_digit`` the one being
revealed and ``flip`` the fall progress 0 → 1; when it lands ``digit`` advances and ``flip`` is 0. If
the spool runs more than one flap ahead, the display jumps so it never lags by more than one flap.
Turning backwards (a real split-flap cannot) shows the spool's flap at once.
# src: split-flap mechanism — flaps hinge on the spool and drop past a stop as it turns (e.g. Solari)
"""

from __future__ import annotations

import math
from typing import Any

from piforge.core.errors import ValidationError
from piforge.twin.devices.base import Device, PropSpec, linked_device, register


@register
class SplitFlap(Device):
    """Split-flap digit (0–9 twice on 20 flaps) driven by a 28BYJ-48; Hall homing output."""

    type = "splitflap"
    label = "Split-flap digit"
    optional_pins = ("hall",)
    pin_aliases = {"out": "hall", "sensor": "hall", "hall_pin": "hall"}
    defaults = {"stepper": None, "flaps": 20, "steps_per_rev": 4096, "home_angle": 0.0, "hall_pin": None,
                "position": 0, "offset_steps": 0, "flip_time": 0.08, "hall_window": 10.0, "start_angle": 90.0,
                "direction": 1}
    outputs = {"angle": PropSpec("float", 0.0, 360.0, unit="deg", default=0.0, label="Spool angle"),
               "digit": PropSpec("int", 0, 9, default=0, label="Digit shown"),
               "next_digit": PropSpec("int", 0, 9, default=1, label="Next digit"),
               "flip": PropSpec("float", 0.0, 1.0, default=0.0, label="Flap fall progress"),
               "hall": PropSpec("bool", default=False, label="Magnet at sensor"),
               "flap": PropSpec("int", 0, None, default=0, label="Flap index"),
               "position": PropSpec("int", 0, None, default=0, label="Module position (0 = leftmost)")}
    example = {"params": {"stepper": "M1", "hall_pin": 4, "position": 0}}

    def setup(self) -> None:
        p = self.params
        if not p.get("stepper"):
            raise ValidationError(f"{self.id} (splitflap): param 'stepper' (a stepper_28byj48 device id) is required")
        try:
            self.flaps = int(p["flaps"])
            self.spr = float(p["steps_per_rev"])
            self.home = float(p["home_angle"])
            self.offset = float(p["offset_steps"])
            self.flip_time = max(1e-3, float(p["flip_time"]))
            self.window = abs(float(p["hall_window"]))
            self.start = float(p["start_angle"])
            self.direction = 1 if float(p["direction"]) >= 0 else -1
            self.module_pos = int(p["position"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{self.id} (splitflap): bad parameter: {exc}") from None
        if self.flaps < 2 or self.spr <= 0:
            raise ValidationError(f"{self.id} (splitflap): flaps must be ≥ 2 and steps_per_rev > 0")
        hall = p.get("hall_pin")
        if hall is not None and "hall" not in self.pins:
            if isinstance(hall, bool) or not isinstance(hall, int) or not 0 <= hall <= 27:
                raise ValidationError(f"{self.id} (splitflap): hall_pin must be a BCM GPIO 0–27, got {hall!r}")
            self.pins["hall"] = hall
        self._stepper: Any = None
        self._steps = 0
        self._hall: bool | None = None
        k = self._target()
        self._shown = k
        self._progress = 0.0

    def link(self, devices: dict[str, Device]) -> None:
        self._stepper = linked_device(self, devices, self.params.get("stepper"), "stepper", ("stepper_28byj48",))
        self._stepper.add_position_listener(self._moved)
        self._steps = int(self._stepper.outputs_state()["position"])
        self._shown, self._progress = self._target(), 0.0
        self._update_hall(None)

    # -- geometry ---------------------------------------------------------------------------------
    def _unwrapped_angle(self) -> float:
        return self.start + self.direction * 360.0 * self._steps / self.spr

    def angle(self) -> float:
        return self._unwrapped_angle() % 360.0

    def _flap_f(self) -> float:
        """Flap count (unwrapped, float) since flap 0 was fully shown."""
        rel_steps = (self._unwrapped_angle() - self.home) * self.spr / 360.0 - self.offset
        return rel_steps / (self.spr / self.flaps)

    def _target(self) -> int:
        return math.floor(self._flap_f() + 1e-9)

    def hall_active(self) -> bool:
        return (self.angle() - self.home) % 360.0 < self.window - 1e-9

    def _update_hall(self, t: float | None) -> None:
        active = self.hall_active()
        if active == self._hall:
            return
        self._hall = active
        if "hall" in self.pins:                                   # open collector: low or released
            self.pi.drive(self.pins["hall"], self.id, 0 if active else None, t=t)

    # -- dynamics ---------------------------------------------------------------------------------
    def _moved(self, position: int, t: float) -> None:
        self._steps = int(position)
        self._update_hall(t)
        k = self._target()
        if k < self._shown:                                       # backwards: no flap animation
            self._shown, self._progress = k, 0.0
        elif k - self._shown > 1:
            self._shown = k - 1

    def tick(self, t: float, dt: float) -> None:
        k = self._target()
        if k < self._shown:
            self._shown, self._progress = k, 0.0
            return
        if k == self._shown:
            self._progress = 0.0
            return
        self._progress += max(0.0, dt) / self.flip_time
        while self._progress >= 1.0 and self._shown < k:
            self._shown += 1
            self._progress -= 1.0
        if self._shown >= k:
            self._progress = 0.0

    def outputs_state(self) -> dict:
        flap = self._shown % self.flaps
        return {"angle": round(self.angle(), 3), "digit": flap % 10, "next_digit": ((flap + 1) % self.flaps) % 10,
                "flip": round(min(1.0, self._progress), 3) if self._target() > self._shown else 0.0,
                "hall": self.hall_active(), "flap": flap, "position": self.module_pos}

    def on_input(self, prop: str, value: Any) -> None:  # pragma: no cover - no inputs
        pass
