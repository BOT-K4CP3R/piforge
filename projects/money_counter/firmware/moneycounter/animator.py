"""Count-up animation: which digit each module should show at time ``t`` (pure logic, no hardware).

A new, larger amount starts an *odometer* that runs from the value shown now to the target, easing
out (fast start, slow finish), so the display visibly counts up and the digits settle one after
another. Its duration comes from the mechanics, not from a fixed formula: it is the time the
**farthest-moving digit** needs at full speed (``flap_time(flaps)``, the motion's trapezoid profile:
ramp up, cruise, brake), clamped to ``[count_time_min, count_time_max]``. Every digit therefore
arrives together with the odometer; one cent more takes one flap (≈ 0.3 s).

A split-flap module only turns forward and only so fast (``capacity`` flaps/s, ≈ 4.2 at 850
half-steps/s with 20 flaps). For each module the animator reports

* ``digit`` — the odometer's digit at this place (what the module should show), taken ``lead``
  seconds ahead (the time one flap takes) so a following module arrives when the odometer does;
* ``spin`` — True once that digit changes faster than the module can follow (and at least a whole
  turn of it is still to come): the module then turns at full speed — the scrolling low digits — and
  lands on the *final* digit when the count ends (``final`` and ``time_left`` of the frame). Spinning
  is sticky for the rest of that count.

A smaller amount (or a correction downwards) is not counted down: the target is shown directly and
every module turns *forward* (wrapping 9 → 0) to its new digit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .amount import DIGITS, digits_of

SPIN_MARGIN = 0.8      # a digit "spins" when the odometer needs > 80 % of the module's speed
END_MARGIN_S = 0.05    # the odometer ends this much after the farthest digit could arrive


@dataclass(frozen=True)
class Frame:
    digits: list[int]       # left → right
    spin: list[bool]
    done: bool              # the odometer has reached the target
    value: int              # odometer value (grosze) shown by the digits
    final: list[int]        # the target's digits
    time_left: float        # seconds until the odometer reaches the target


def _default_flap_time(capacity: float) -> Callable[[float], float]:
    return lambda flaps: flaps / capacity


class CountUp:
    def __init__(self, *, count_time_min: float = 0.25, count_time_max: float = 3.0, capacity: float = 4.15,
                 flap_time: Callable[[float], float] | None = None, initial: int = 0) -> None:
        self.count_time_min = count_time_min
        self.count_time_max = count_time_max
        self.capacity = capacity
        self.flap_time = flap_time or _default_flap_time(capacity)
        self.lead = self.flap_time(1)
        self.start = float(initial)
        self.target = int(initial)
        self.t0 = 0.0
        self.duration = 0.0
        self._spinning: set[int] = set()

    # -- timing ------------------------------------------------------------------------------------
    def duration_for(self, start: int, target: int) -> float:
        """Odometer duration for ``start`` → ``target`` (grosze): the farthest digit at full speed."""
        if target <= start:
            return 0.0
        a, b = digits_of(start), digits_of(target)
        far = max((y - x) % 10 for x, y in zip(a, b, strict=True))
        t = self.flap_time(far) + END_MARGIN_S if far else self.count_time_min
        return min(self.count_time_max, max(self.count_time_min, t))

    @staticmethod
    def _ease(u: float) -> float:
        return 1.0 - (1.0 - u) ** 3

    @staticmethod
    def _ease_rate(u: float) -> float:
        return 3.0 * (1.0 - u) ** 2

    def _u(self, now: float) -> float:
        if self.duration <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.t0) / self.duration))

    def value(self, now: float) -> float:
        """Odometer value (grosze, float) at ``now``."""
        u = self._u(now)
        if u >= 1.0:
            return float(self.target)
        return self.start + (self.target - self.start) * self._ease(u)

    def done(self, now: float) -> bool:
        return self._u(now) >= 1.0

    # -- control -----------------------------------------------------------------------------------
    def set_target(self, cents: int, now: float) -> tuple[str, float]:
        """New target; returns ``(mode, duration)`` with mode ``same`` | ``count`` | ``jump``."""
        cents = int(cents)
        if cents == self.target and self.done(now):
            return "same", 0.0
        cur = self.value(now)
        if cents >= math.floor(cur):
            self.start, self.target, self.t0 = cur, cents, now
            self.duration = self.duration_for(int(math.floor(cur + 1e-9)), cents)
            self._spinning = set()
            return "count", self.duration
        self.start, self.target, self.t0, self.duration = float(cents), cents, now, 0.0
        self._spinning = set()
        return "jump", 0.0

    def frame(self, now: float) -> Frame:
        u = self._u(now)
        final = digits_of(self.target)
        if u >= 1.0:
            return Frame(final, [False] * DIGITS, True, self.target, final, 0.0)
        v = self.value(now)
        shown = min(self.target, int(math.floor(v + 1e-9)))
        ahead = min(self.target, int(math.floor(self.value(now + self.lead) + 1e-9)))
        rate = (self.target - self.start) * self._ease_rate(u) / self.duration      # grosze / s
        spin = []
        for j in range(DIGITS):
            place = 10 ** (DIGITS - 1 - j)
            # spin only while a whole turn of this digit (≥ 10 changes) is still to come: a digit
            # that changes a few times only follows (a short spin would cost it a full extra turn)
            remaining = self.target // place - shown // place
            if remaining >= 10 and rate / place > self.capacity * SPIN_MARGIN:
                self._spinning.add(j)
            spin.append(j in self._spinning)
        left = max(0.0, self.t0 + self.duration - now)
        return Frame(digits_of(ahead), spin, False, shown, final, left)
