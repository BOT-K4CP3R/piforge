"""28BYJ-48 stall model (``stall_model: true``): pull-in / pull-out / acceleration limits lose steps."""

from __future__ import annotations

import random

import pytest

from piforge.twin.clock import ManualClock
from piforge.twin.config import DeviceConfig, TwinConfig
from piforge.twin.runtime import Twin

HALF = ["1000", "1100", "0100", "0110", "0010", "0011", "0001", "1001"]
PINS = (5, 6, 13, 19)


class Drive:
    """One stepper on 4 GPIOs, stepped forward at given instants (ManualClock)."""

    def __init__(self, **params) -> None:
        self.twin = Twin(TwinConfig(devices=[DeviceConfig("M1", "stepper_28byj48", {"in1": 5, "in2": 6, "in3": 13,
                                                                                        "in4": 19}, params=params)]),
                         clock=ManualClock())
        for p in PINS:
            self.twin.pi.setup(p, "output")
        self.phase = 0
        self.commanded = 0
        self._apply()

    def _apply(self) -> None:
        for pin, bit in zip(PINS, HALF[self.phase]):
            self.twin.pi.write(pin, int(bit))

    def step(self, dt: float) -> None:
        self.twin.step(dt)
        self.phase = (self.phase + 1) % 8
        self.commanded += 1
        self._apply()

    def run(self, rates) -> None:
        for r in rates:
            self.step(1.0 / r)

    def state(self) -> dict:
        return self.twin.state()["devices"]["M1"]

    def codes(self) -> list[str]:
        return [e["code"] for e in self.twin.pi.events]


def ramp(v0: float, v1: float, accel: float, n_cruise: int) -> list[float]:
    """Step rates of a trapezoid-ish profile: accelerate v0 → v1 (per distance), cruise n steps."""
    out, v = [], v0
    while v < v1:
        out.append(v)
        v = min(v1, (v * v + 2 * accel) ** 0.5)
    return out + [v1] * n_cruise


STALL = dict(stall_model=True, max_pps=950.0, start_pps=500.0, max_accel=5000.0)


def test_default_has_no_stall_model():
    d = Drive()
    d.step(1.0)
    d.run([2000.0] * 400)                                   # far too fast for a real motor …
    st = d.state()
    assert st["position"] == 401 and st["lost_steps"] == 0  # … but the default model counts every step
    assert "TWIN.STEPPER_LOST_STEPS" not in d.codes()


def test_ramped_move_below_limits_keeps_every_step():
    d = Drive(**STALL)
    d.step(1.0)
    d.run(ramp(500.0, 850.0, 3000.0, 3000))
    st = d.state()
    assert st["lost_steps"] == 0 and st["position"] == d.commanded
    assert not d.codes()


def test_start_above_pull_in_loses_steps_in_whole_electrical_cycles():
    d = Drive(**STALL)
    d.step(1.0)
    d.run([1200.0] * 200)                                   # no ramp, 1200 pps from rest
    st = d.state()
    assert st["lost_steps"] > 0 and st["lost_steps"] % 8 == 0
    assert st["position"] == d.commanded - st["lost_steps"]
    assert "TWIN.STEPPER_LOST_STEPS" in d.codes()


def test_cruise_above_pull_out_loses_steps_even_when_ramped():
    d = Drive(**STALL)
    d.step(1.0)
    d.run(ramp(500.0, 1150.0, 3000.0, 2000))
    assert d.state()["lost_steps"] >= 16


def test_too_steep_ramp_loses_steps():
    d = Drive(**STALL)
    d.step(1.0)
    d.run(ramp(500.0, 900.0, 40000.0, 500))                # 8 × the motor's acceleration
    assert d.state()["lost_steps"] > 0


def test_timing_jitter_at_850_is_tolerated():
    """Python-timed stepping is jittery: each step up to ±35 % of a period off its deadline (and an
    occasional 3-period hiccup, after which the firmware ramps again) must not lose steps at 850."""
    rnd = random.Random(1)
    d = Drive(**STALL)
    d.step(1.0)
    d.run(ramp(500.0, 850.0, 3000.0, 0))
    t_prev, t_now, k = 0.0, 0.0, 0
    for k in range(1, 8001):
        t = k / 850.0 + rnd.uniform(-0.35, 0.35) / 850.0
        d.step(max(1e-5, t - t_prev))
        t_prev = t
    assert d.state()["lost_steps"] == 0
    d.step(3.0 / 850)                                       # hiccup, then re-ramp from start_pps
    d.run(ramp(520.0, 850.0, 3000.0, 2000))
    assert d.state()["lost_steps"] == 0


def test_slip_input_moves_the_rotor_back_without_an_event():
    d = Drive(**STALL)
    d.step(1.0)
    d.run([400.0] * 100)
    seen = []
    d.twin.device("M1").add_position_listener(lambda pos, t: seen.append(pos))
    d.twin.set_input("M1", "slip", 40)
    st = d.state()
    assert st["lost_steps"] == 40 and st["position"] == d.commanded - 40 and seen[-1] == st["position"]
    d.run([400.0] * 10)                                     # keeps stepping from the slipped position
    assert d.state()["position"] == d.commanded - 40
    assert not d.codes()
    with pytest.raises(Exception):
        d.twin.set_input("M1", "slip", 0.5)


def test_slip_works_without_the_stall_model():
    d = Drive()
    d.step(1.0)
    d.run([400.0] * 20)
    d.twin.set_input("M1", "slip", 8)
    assert d.state()["position"] == d.commanded - 8 and d.state()["lost_steps"] == 8
