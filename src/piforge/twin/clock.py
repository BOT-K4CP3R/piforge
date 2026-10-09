"""Simulation clocks.

The twin runs in real time: :class:`SimClock` maps wall-clock seconds (``time.monotonic``) to
simulated seconds, optionally scaled by ``speed``. :class:`ManualClock` only moves when told to and
makes device models fully deterministic in unit tests.

The original ``time`` functions are captured at import so the runner may later warp the firmware's
view of time (``--speed``) without affecting the twin's own clock.
"""

from __future__ import annotations

import time

from piforge.core.errors import ValidationError

_monotonic = time.monotonic
_sleep = time.sleep


class SimClock:
    """Real-time clock: ``now()`` = seconds since creation × ``speed``."""

    manual: bool = False

    def __init__(self, speed: float = 1.0) -> None:
        if not speed > 0:
            raise ValidationError(f"clock speed must be > 0, got {speed!r}")
        self.speed = float(speed)
        self._t0 = _monotonic()

    def now(self) -> float:
        """Simulated seconds since the clock started."""
        return (_monotonic() - self._t0) * self.speed

    def to_real(self, sim_dt: float) -> float:
        """Convert a simulated duration to wall-clock seconds."""
        return sim_dt / self.speed

    def sleep(self, sim_dt: float) -> None:
        """Block for ``sim_dt`` simulated seconds."""
        if sim_dt > 0:
            _sleep(sim_dt / self.speed)


class ManualClock(SimClock):
    """A clock that only advances via :meth:`advance` / :meth:`set` (deterministic tests)."""

    manual = True

    def __init__(self, start: float = 0.0) -> None:
        self.speed = 1.0
        self._t = float(start)

    def now(self) -> float:
        """Current simulated time (s)."""
        return self._t

    def advance(self, dt: float) -> float:
        """Move time forward by ``dt`` seconds; returns the new time."""
        if dt < 0:
            raise ValidationError(f"cannot move a clock backwards (dt={dt})")
        self._t += dt
        return self._t

    def set(self, t: float) -> None:
        """Jump to absolute time ``t`` (must not go backwards)."""
        if t < self._t:
            raise ValidationError(f"cannot move a clock backwards ({t} < {self._t})")
        self._t = float(t)

    def sleep(self, sim_dt: float) -> None:
        """Sleeping on a manual clock just advances it."""
        if sim_dt > 0:
            self.advance(sim_dt)
