"""Actuators: hobby servo, 28BYJ-48 stepper on a ULN2003, brushed DC motor on an H-bridge."""

from __future__ import annotations

import math
from typing import Any

from piforge.core.errors import ValidationError
from piforge.twin.devices.base import Device, PropSpec, linked_device, register


@register
class Servo(Device):
    """Hobby servo: pulse width → angle, slewing at ``speed_dps``.

    Defaults follow gpiozero's ``AngularServo`` convention (1.0 ms → ``min_angle`` −90°, 2.0 ms →
    ``max_angle`` +90°) so firmware and twin agree out of the box; override per part
    (e.g. SG90 ≈ 0.5–2.4 ms for 0–180°).
    # src: gpiozero AngularServo defaults (min/max_pulse_width 1/1000, 2/1000 s)
    # src: TowerPro SG90 datasheet — operating speed 0.1 s/60° @ 4.8 V → 600 °/s
    """

    type = "servo"
    label = "Servo"
    pin_roles = ("pin",)
    pin_aliases = {"sig": "pin", "signal": "pin", "s": "pin", "pwm": "pin", "ctrl": "pin", "in": "pin",
                   "control": "pin"}
    defaults = {"min_pulse": 0.001, "max_pulse": 0.002, "min_angle": -90.0, "max_angle": 90.0,
                "speed_dps": 600.0, "initial_angle": 0.0}
    outputs = {"angle": PropSpec("float", -360.0, 360.0, unit="deg", default=0.0, label="Angle"),
               "target_angle": PropSpec("float", -360.0, 360.0, unit="deg", default=0.0, label="Target"),
               "pulse_us": PropSpec("float", 0.0, 3000.0, unit="µs", default=0.0, label="Pulse width"),
               "powered": PropSpec("bool", default=False, label="Receiving pulses")}
    example = {"pins": {"pin": 18}}

    def setup(self) -> None:
        self._angle = float(self.params.get("initial_angle", 0.0))
        self._target = self._angle

    def _pulse(self) -> float | None:
        freq, duty = self.pi.pwm(self.pins["pin"])
        if not freq or not duty:
            return None
        p = duty / freq
        return p if 0.0002 <= p <= 0.003 else None   # outside this a servo ignores the signal

    def _target_for(self, pulse: float) -> float:
        p0, p1 = float(self.params["min_pulse"]), float(self.params["max_pulse"])
        a0, a1 = float(self.params["min_angle"]), float(self.params["max_angle"])
        frac = (pulse - p0) / (p1 - p0) if p1 != p0 else 0.5
        return a0 + max(0.0, min(1.0, frac)) * (a1 - a0)    # mechanical end stops

    def tick(self, t: float, dt: float) -> None:
        p = self._pulse()
        if p is not None:
            self._target = self._target_for(p)
        step = float(self.params.get("speed_dps", 600.0)) * dt
        delta = self._target - self._angle
        self._angle = self._target if abs(delta) <= step else self._angle + math.copysign(step, delta)

    def outputs_state(self) -> dict:
        p = self._pulse()
        target = self._target_for(p) if p is not None else self._target
        return {"angle": round(self._angle, 3), "target_angle": round(target, 3),
                "pulse_us": round(p * 1e6, 2) if p is not None else 0.0, "powered": p is not None}


# Half-step sequence for IN1..IN4.  # src: 28BYJ-48 datasheet, 8-beat drive sequence
HALF_STEP = [(1, 0, 0, 0), (1, 1, 0, 0), (0, 1, 0, 0), (0, 1, 1, 0),
             (0, 0, 1, 0), (0, 0, 1, 1), (0, 0, 0, 1), (1, 0, 0, 1)]
_PHASE = {p: i for i, p in enumerate(HALF_STEP)}


@register
class Stepper28BYJ48(Device):
    """28BYJ-48 unipolar stepper driven through a ULN2003 (IN1..IN4).

    Position is counted in half-steps from the coil pattern sequence (wave, full and half-step
    drive all work): phase change ±1..3 → that many half-steps; a 180° phase jump (±4) is
    ambiguous and recorded as ``TWIN.STEPPER_STALL``. Stepping faster than ``max_pps`` records
    ``TWIN.STEPPER_OVERSPEED`` (a real motor would skip).

    Coils come either from four GPIOs (``pins={"in1".."in4"}``) or, with no pins, from outputs of a
    ``shift_register_74hc595`` chain: ``params={"coil_source": {"device": "SR1", "bits": [b0, b1, b2,
    b3]}}`` where ``b0..b3`` are the chain bit indices (chip i//8, Q i%8) feeding IN1..IN4.
    Other devices may follow the shaft with :meth:`add_position_listener`.

    **Stall model** (opt-in, ``params={"stall_model": True, …}``; off by default, so the counted
    position always equals the commanded one). The rotor then follows the field only as fast as the
    motor can: from rest up to ``start_pps`` (pull-in) at once, faster only at ``max_accel``
    half-steps/s² and never above ``max_pps`` (pull-out). Steps it cannot follow pile up as lag; a
    lag of more than 4 half-steps (180° electrical) makes the rotor drop into the field equilibrium
    one electrical cycle behind — it slips back up to 4 half-steps and loses 8 (``lost_steps``,
    ``TWIN.STEPPER_LOST_STEPS``). A few-step lag absorbs timing jitter; a rotor that arrives early at
    a late step waits there and is braked at 4 × ``max_accel`` (rotor inertia), so a slightly late
    step costs little speed. With the stall model the instantaneous ``TWIN.STEPPER_OVERSPEED``
    warning is replaced by these losses.
    The ``slip`` input (int half-steps) knocks the rotor back that far (a disturbance; lost, no event).
    # src: 28BYJ-48 datasheet — stride angle 5.625°/64 → 4096 half-steps per output revolution
    # src: 28BYJ-48 pull-in rate ≈ 500–1000 pps at 5 V (vendor datasheets)
    # src: est — stall model: pull-out ≈ 900–1000 half-steps/s for a 5 V 28BYJ-48 with a light load
    #   (vendor curves show the pull-out torque collapsing in that range; ``max_pps`` default 1000,
    #   the money counter's light flap spool uses 950), pull-in ≈ 500 pps, max_accel ≈ 5000 half-steps/s² (rotor-side
    #   inertia is tiny; measured hobby ramps reach 1000 pps in ≈ 0.1 s); 180° electrical slip limit
    #   is the textbook stepper stability limit.
    """

    type = "stepper_28byj48"
    label = "Stepper 28BYJ-48 + ULN2003"
    pin_roles = ("in1", "in2", "in3", "in4")
    pin_aliases = {"1": "in1", "2": "in2", "3": "in3", "4": "in4", "ina": "in1", "inb": "in2",
                   "inc": "in3", "ind": "in4"}
    defaults = {"steps_per_rev": 4096, "max_pps": 1000.0, "stall_model": False, "start_pps": 500.0,
                "max_accel": 5000.0}
    inputs = {"slip": PropSpec("int", -4096, 4096, default=0, label="Slip (half-steps knocked back)")}
    outputs = {"position": PropSpec("int", default=0, label="Position (half-steps)"),
               "lost_steps": PropSpec("int", default=0, label="Lost half-steps"),
               "angle": PropSpec("float", unit="deg", default=0.0, label="Shaft angle"),
               "rpm": PropSpec("float", unit="rpm", default=0.0, label="Speed"),
               "energized": PropSpec("bool", default=False, label="Coils energised"),
               "coils": PropSpec("text", default="0000", label="IN1..IN4")}
    example = {"pins": {"in1": 5, "in2": 6, "in3": 13, "in4": 19}}

    def uses_pins(self, pins: dict[str, int]) -> bool:
        return not self.params.get("coil_source")

    def setup(self) -> None:
        self._phase: int | None = None
        self._position = 0
        self._last_step_t: float | None = None
        self._rpm = 0.0
        self._last_pos = 0
        self._overspeed_warned = False
        self._lost_warned = False
        self._lost = 0                 # half-steps the rotor is behind the field (stall model + slip input)
        self._stall = bool(self.params.get("stall_model", False))
        self._v = 0.0                  # rotor speed (half-steps/s), stall model
        self._lag = 0.0                # field ahead of the rotor (half-steps), stall model
        self._dir = 0
        self._t_rotor: float | None = None
        self._pos_listeners: list = []
        self._source: Any = None
        self._source_bits: tuple[int, ...] = ()
        src = self.params.get("coil_source")
        if src:
            bits = src.get("bits") if isinstance(src, dict) else None
            if not isinstance(src, dict) or not src.get("device") or not isinstance(bits, (list, tuple)) \
                    or len(bits) != 4 or not all(isinstance(b, int) and not isinstance(b, bool) and b >= 0
                                                 for b in bits):
                raise ValidationError(f"{self.id} ({self.type}): coil_source must be "
                                      f"{{'device': <shift register id>, 'bits': [b0, b1, b2, b3]}}, got {src!r}")
            if self.pins:
                raise ValidationError(f"{self.id} ({self.type}): use either pins in1..in4 or coil_source, not both")
            self._source_bits = tuple(int(b) for b in bits)
            return
        for role in self.pin_roles:
            self.listen(self.pins[role], self._changed)

    def link(self, devices: dict[str, Device]) -> None:
        src = self.params.get("coil_source")
        if not src:
            return
        sr = linked_device(self, devices, src.get("device"), "coil_source device", ("shift_register_74hc595",))
        n = 8 * int(getattr(sr, "length", 1))
        bad = [b for b in self._source_bits if b >= n]
        if bad:
            raise ValidationError(f"{self.id} ({self.type}): coil_source bits {bad} do not exist on {sr.id} "
                                  f"({n} outputs, bits 0…{n - 1})")
        self._source = sr
        sr.add_bits_listener(self._source_changed)

    def close(self) -> None:
        if self._source is not None:
            self._source.remove_bits_listener(self._source_changed)
        super().close()

    def _source_changed(self, bits: int, t: float) -> None:
        self._changed(-1, 0, t)

    def add_position_listener(self, cb) -> None:
        """``cb(position, t)`` after every counted step (under ``pi.lock``; keep it quick)."""
        self._pos_listeners.append(cb)

    def _pattern(self) -> tuple[int, ...]:
        if self._source_bits:
            if self._source is None:
                return (0, 0, 0, 0)
            bits = self._source.output_bits()
            return tuple((bits >> b) & 1 for b in self._source_bits)
        out = []
        for role in self.pin_roles:
            bcm = self.pins[role]
            out.append(1 if self.pi.mode(bcm) == "output" and self.pi.effective(bcm) >= 0.5 else 0)
        return tuple(out)

    def _changed(self, bcm: int, level: int, t: float) -> None:
        pat = self._pattern()
        phase = _PHASE.get(pat)
        if phase is None:
            return                     # all off (coast) or 3 coils (transitional): rotor holds
        if self._phase is None:
            self._phase = phase
            return
        d = (phase - self._phase) % 8
        if d == 0:
            return
        if d == 4:
            self.pi.record_event("TWIN.STEPPER_STALL", "warning",
                                 f"{self.id}: coil phase jumped 180° — direction ambiguous, step lost",
                                 device=self.id)
            self._phase = phase
            return
        steps = d if d < 4 else d - 8
        self._phase = phase
        lost = self._follow(steps, t) if self._stall else 0
        self._position += steps - lost
        if self._last_step_t is not None and not self._stall:
            dt = t - self._last_step_t
            max_pps = float(self.params.get("max_pps", 1000.0))
            if 0 < dt < abs(steps) / max_pps and not self._overspeed_warned:
                self._overspeed_warned = True
                self.pi.record_event("TWIN.STEPPER_OVERSPEED", "warning",
                                     f"{self.id}: {abs(steps) / dt:.0f} steps/s exceeds ~{max_pps:.0f} "
                                     f"(real motor would skip)", device=self.id)
        self._last_step_t = t
        for cb in list(self._pos_listeners):
            cb(self._position, t)

    # -- stall model ------------------------------------------------------------------------------
    SLIP_LAG = 4.0                 # half-steps: 180° electrical, beyond it the rotor falls back a cycle
    CYCLE = 8                      # half-steps per electrical cycle (one slip loses this many)
    REST_S = 0.02                  # a gap this long: the rotor has stopped on the field
    BRAKE = 4.0                    # holding torque brakes a rotor that waits at the field: 4 × max_accel

    def _follow(self, steps: int, t: float) -> int:
        """Rotor dynamics for ``steps`` new field half-steps at ``t``; returns the half-steps lost."""
        p = self.params
        start, vmax, acc = float(p["start_pps"]), float(p["max_pps"]), float(p["max_accel"])
        direction = 1 if steps > 0 else -1
        dt = None if self._t_rotor is None else max(0.0, t - self._t_rotor)
        self._t_rotor = t
        if dt is None or dt > self.REST_S or direction != self._dir:
            self._v, self._lag, self._dir = 0.0, 0.0, direction        # from rest (it caught up meanwhile)
            dt = dt if dt is not None and dt <= self.REST_S else 0.0
        cap = min(vmax, max(start, self._v + acc * dt))                 # fastest the rotor can go now
        if self._lag <= cap * dt:                                       # it reaches the field within dt
            if self._v > 0 and self._v * dt >= self._lag:               # early: waits there, field brakes it
                wait = dt - self._lag / self._v
                self._v = max(self._lag / dt if dt > 0 else 0.0, self._v - self.BRAKE * acc * wait)
            elif dt > 0:
                self._v = self._lag / dt                                # speeds up just enough
            self._lag = 0.0
        else:                                                           # falls behind: full effort
            self._lag -= cap * dt
            self._v = cap
        self._lag += abs(steps)
        lost = 0
        while self._lag > self.SLIP_LAG:
            self._lag -= self.CYCLE                                     # next equilibrium behind
            lost += self.CYCLE
        if lost:
            self._lag = max(0.0, self._lag)                             # it falls back onto it
            self._v = 0.0                                               # and stalls
            self._lost += lost
            if not self._lost_warned:
                self._lost_warned = True
                self.pi.record_event("TWIN.STEPPER_LOST_STEPS", "warning",
                                     f"{self.id}: rotor could not follow the steps (pull-in {start:.0f}, "
                                     f"pull-out {vmax:.0f} half-steps/s, accel {acc:.0f}/s²) — lost {lost} "
                                     "half-steps", device=self.id)
        return lost * direction

    def on_input(self, prop: str, value: Any) -> None:
        if prop != "slip" or not value:
            return
        n = int(value)
        with self.pi.lock:
            self._lost += n
            self._position -= n
            t = self.pi.clock.now()
            for cb in list(self._pos_listeners):
                cb(self._position, t)

    def tick(self, t: float, dt: float) -> None:
        if dt <= 0:
            return
        spr = float(self.params.get("steps_per_rev", 4096))
        inst = (self._position - self._last_pos) / spr / dt * 60.0
        self._last_pos = self._position
        k = 1.0 - math.exp(-dt / 0.2)
        self._rpm += (inst - self._rpm) * k

    def outputs_state(self) -> dict:
        spr = float(self.params.get("steps_per_rev", 4096))
        pat = self._pattern()
        return {"position": self._position, "lost_steps": self._lost, "angle": round(360.0 * self._position / spr, 4),
                "rpm": round(self._rpm, 3), "energized": any(pat), "coils": "".join(map(str, pat))}


@register
class DCMotor(Device):
    """Brushed DC motor behind an H-bridge (L298N / TB6612 / DRV8833).

    drive = (IN1 − IN2) × EN, where each term is the pin's PWM duty (EN = 1 when no ``pwm`` pin).
    IN1 = IN2 = HIGH brakes, both LOW / EN = 0 coasts. Speed follows the drive with a first-order
    lag ``tau``. # src: typical "TT" 1:48 gear motor — ≈200 rpm no-load at 6 V; τ_mech ≈ 0.1–0.2 s
    """

    type = "dc_motor"
    label = "DC motor"
    pin_roles = ("in1", "in2")
    optional_pins = ("pwm",)
    pin_aliases = {"ena": "pwm", "enb": "pwm", "en": "pwm", "enable": "pwm", "pwma": "pwm", "pwmb": "pwm",
                   "ain1": "in1", "ain2": "in2", "bin1": "in1", "bin2": "in2", "ina": "in1", "inb": "in2",
                   "forward": "in1", "fwd": "in1", "backward": "in2", "rev": "in2"}
    defaults = {"max_rpm": 200.0, "tau": 0.15}
    outputs = {"speed": PropSpec("float", -1.0, 1.0, default=0.0, label="Speed (−1…1)"),
               "rpm": PropSpec("float", unit="rpm", default=0.0, label="Speed"),
               "angle": PropSpec("float", unit="deg", default=0.0, label="Shaft angle"),
               "direction": PropSpec("enum", default="coast", choices=("forward", "reverse", "brake", "coast"),
                                     label="Direction")}
    example = {"pins": {"in1": 5, "in2": 6, "pwm": 13}}

    def setup(self) -> None:
        self._speed = 0.0
        self._angle = 0.0

    def _level(self, role: str) -> float:
        bcm = self.pins[role]
        return self.pi.effective(bcm) if self.pi.mode(bcm) == "output" else 0.0

    def _drive(self) -> tuple[float, str]:
        a, b = self._level("in1"), self._level("in2")
        en = self._level("pwm") if "pwm" in self.pins else 1.0
        if en <= 0.0:
            return 0.0, "coast"
        if a >= 0.5 and b >= 0.5:
            return 0.0, "brake"
        drive = (a - b) * en
        if drive > 0:
            return drive, "forward"
        if drive < 0:
            return drive, "reverse"
        return 0.0, "coast"

    def tick(self, t: float, dt: float) -> None:
        if dt <= 0:
            return
        target, mode = self._drive()
        tau = float(self.params.get("tau", 0.15))
        tau = tau / 4 if mode == "brake" else (tau * 3 if mode == "coast" else tau)
        self._speed += (target - self._speed) * (1.0 - math.exp(-dt / max(tau, 1e-4)))
        self._angle += self._speed * float(self.params.get("max_rpm", 200.0)) * 6.0 * dt

    def outputs_state(self) -> dict:
        _, mode = self._drive()
        return {"speed": round(self._speed, 4),
                "rpm": round(self._speed * float(self.params.get("max_rpm", 200.0)), 2),
                "angle": round(self._angle, 2), "direction": mode}

    def on_input(self, prop: str, value: Any) -> None:  # pragma: no cover - no inputs
        pass
