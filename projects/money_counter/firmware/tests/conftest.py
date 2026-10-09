"""Off-Pi unit tests of the firmware: ``python -m pytest projects/money_counter/firmware/tests -q``.

No hardware, no PiForge needed (only ``websockets`` for the network tests). ``FakeRig`` simulates the
coil word → 8 rotors (half-step decoding like a real 28BYJ-48) and the Hall sensors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FW = Path(__file__).resolve().parents[1]
if str(FW) not in sys.path:
    sys.path.insert(0, str(FW))

from moneycounter.motion import HALF_STEP, Motion  # noqa: E402

PHASE = {(a | b << 1 | c << 2 | d << 3): i for i, (a, b, c, d) in enumerate(HALF_STEP)}


class FakeRig:
    """8 rotors + Hall sensors. ``true_spr`` may differ from the firmware's steps_per_rev (real
    28BYJ-48 ≈ 4076). The magnet is under the sensor for ``window`` half-steps after ``home``."""

    def __init__(self, n=8, *, true_spr=4096, start=(0,) * 8, offsets=(0,) * 8, window=114, pins=None):
        self.n, self.true_spr, self.window = n, true_spr, window
        self.pos = list(start)                   # rotor half-steps since the Hall edge (mod true_spr)
        self.phase = [None] * n
        self.offsets = list(offsets)
        self.pins = list(pins or range(n))
        self.writes = 0
        self.backwards = 0
        self.word = 0
        self.frozen: set[int] = set()            # stalled rotors: the field turns, they do not

    # CoilBus
    def write(self, word: int) -> None:
        self.writes += 1
        self.word = word
        for i in range(self.n):
            nib = (word >> (4 * i)) & 0xF
            ph = PHASE.get(nib)
            if ph is None:
                continue
            if self.phase[i] is not None and i not in self.frozen:
                d = (ph - self.phase[i]) % 8
                if d == 1:
                    self.pos[i] += 1
                elif d == 7:
                    self.pos[i] -= 1
                    self.backwards += 1
                elif d:
                    raise AssertionError(f"rotor {i}: phase jump {d}")
            self.phase[i] = ph

    def knock(self, i: int, n: int) -> None:
        """Disturbance: rotor ``i`` slips back ``n`` half-steps (lost)."""
        self.pos[i] -= n

    # HallInputs
    def read(self, pin: int) -> int:
        i = self.pins.index(pin)
        return 0 if self.pos[i] % self.true_spr < self.window else 1

    def flap_shown(self, i: int, spr_fw=4096, flaps=20) -> float:
        """Flaps (float) the rotor is past digit 0 (true geometry)."""
        rel = (self.pos[i] - self.offsets[i]) % self.true_spr
        return rel * flaps / self.true_spr


def make_motion(rig: FakeRig, **kw) -> Motion:
    clock = kw.pop("clock", None) or (lambda: 0.0)
    args = dict(steps_per_rev=4096, flaps=20, max_pps=850, start_pps=450, accel=3000, hold_ms=60, hall_pins=rig.pins,
                offsets=rig.offsets, bus=rig, halls=rig, home_timeout_s=1e9, clock=clock, log=lambda m: None)
    args.update(kw)
    return Motion(**args)


def run_until(motion: Motion, cond, max_ticks=400_000, t0=0.0, every=None, on_every=None):
    """Event-driven: tick at each deadline the motion asks for (idle: every 20 ms). ``on_every(t)`` is
    called every ``every`` seconds (the controller's frame loop). Returns the time ``cond`` held."""
    t, nxt_frame = t0, t0
    for _ in range(max_ticks):
        if on_every is not None and t >= nxt_frame:
            on_every(t)
            nxt_frame = t + every
        if cond():
            return t
        nxt = motion.tick(t)
        t_new = nxt if nxt is not None else t + 0.02
        if on_every is not None:
            t_new = min(t_new, nxt_frame)
        t = max(t_new, t + 1e-6)
        if hasattr(motion.clock, "t"):
            motion.clock.t = t
    raise AssertionError("condition not reached")


@pytest.fixture
def rig():
    return FakeRig(start=(4000, 3900, 100, 0, 2048, 4095, 3000, 1234))
