"""Stepping of the 8 split-flap modules: ramped moves, homing, closed-loop Hall checks.

Hardware model (see README): module ``i`` (0 = leftmost) has a 28BYJ-48 whose ULN2003 inputs
IN1…IN4 are the 74HCT595 chain outputs ``4i … 4i+3`` (bit ``b`` = chip ``b // 8``, output Q``b % 8``;
chip 0 is the one whose SER is on MOSI). One SPI transfer of 4 bytes (big-endian 32-bit word)
updates all 32 coils; CE0 (GPIO8) is the latch. Its A3144 Hall switch pulls ``hall_pins[i]`` low
while the spool's magnet passes it.

Positions are counted in half-steps per module, ``0`` = digit 0 (flap 0) fully shown. Flap ``k``
(unwrapped, any integer ≥ 0) sits at :meth:`Motion.steps_for_flap` ``(k)`` and shows digit
``k mod 10``. The modules never turn backwards (a split-flap cannot).

**Motion profile.** Every move is a trapezoid in *distance*: it starts at ``start_pps`` (below the
motor's pull-in rate, so it can start at once), accelerates at ``accel`` half-steps/s² up to the
module's top speed (``max_pps``, below the pull-out rate), and brakes so that it is back at
``start_pps`` on the last step — it stops exactly on the target. All modules run in one timing loop
(:meth:`Motion.run`): each module has its own step deadline, every wake-up steps the modules that are
due and writes **one** 32-bit coil word. The loop wakes at most once per tick (``1 / max_pps``);
modules due within half a tick share that word. Deadlines are absolute, so ordinary jitter does not
accumulate and catching up is limited to one step per tick (never faster than ``max_pps``). A step
more than a whole period late re-starts the schedule from a lower speed (the rotor slowed meanwhile)
and the module accelerates again — host hiccups cost time, not steps.

**Closed loop.** Power-up homing turns slowly (``start_pps``) to the Hall edge; that edge is
position ``-offset``. With ``selftest`` each module then turns once at its top speed and is checked
at the magnet (``SELFTEST m<i> …``); losses there lower its speed (``DERATE``) and repeat the test,
so every module only runs at a speed proven loss-free on it. Every later Hall edge is a check: the
count should be a whole number of turns there.

* small error (≤ ``resync_tolerance``, e.g. the real gear ratio 4076 vs 4096): corrected silently;
* larger error: corrected and logged ``RESYNC m<i> err=<n>`` (``n`` > 0 = the motor lost steps);
  the module re-ramps from ``start_pps``; losses on two checks in a row lower its top speed;
* error > ``steps_per_rev / 10``, or the count passes the magnet's place by 1/10 turn without seeing
  it: the count is not trusted — the module **re-homes** (keeps turning to the next Hall edge, sets
  the position there, ``HOME module i: re-homed``) and then continues to its digit.

A module may not come to rest once its count has reached the magnet's place without the magnet, nor
after a re-home or derate: it first turns on past its magnet (``VERIFY m<i>``), so the position it
stops at has been checked within its last revolution. :meth:`Motion.settled` (→ ``SHOW``) waits for
that. A falling Hall edge counts only after the magnet was away for ``HALL_DEBOUNCE`` half-steps (a
rotor slipping back into the sensor window is no edge). A dead sensor ends in ``FAILED`` (logged),
never in an endless spin.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

# src: 28BYJ-48 datasheet, 8-beat half-step drive sequence for IN1..IN4
HALF_STEP = ((1, 0, 0, 0), (1, 1, 0, 0), (0, 1, 0, 0), (0, 1, 1, 0),
             (0, 0, 1, 0), (0, 0, 1, 1), (0, 0, 0, 1), (1, 0, 0, 1))
DERATE = 0.85            # top speed × this after repeated losses
VERIFY_TRIES = 3         # verify turns before giving up (logged) — never an endless spin
HALL_DEBOUNCE = 16       # half-steps the magnet must have been away before a new edge counts
REST_PERIODS = 4.0       # a step this many periods late: the rotor has (nearly) stopped, ramp again
LATE_DECEL = 8.0         # a step > 1 period late: assume the field braked the rotor at 8 × accel


class CoilBus(Protocol):
    def write(self, word: int) -> None: ...


class HallInputs(Protocol):
    def read(self, pin: int) -> int: ...       # 1 = released (pull-up), 0 = magnet at the sensor


@dataclass
class ModuleState:
    index: int
    hall_pin: int
    offset: int
    cap: float                    # top speed of this module (max_pps, lowered by DERATE)
    steps: int = 0
    goal: int = 0                 # unwrapped flap index to stop at
    phase: int = 0
    energised: bool = False
    idle_since: float = 0.0
    home: str = "seek_high"       # seek_high → seek_edge → [selftest →] done | failed; rehome: seek an edge
    home_t0: float = 0.0
    spin: bool = False
    last_hall: int | None = None
    v: float = 0.0                # speed of the last step (half-steps/s); 0 = at rest
    next_t: float | None = None   # deadline of the next step (None = at rest)
    last_step_t: float = 0.0
    edge_steps: int = 0           # count at the last Hall edge (after correction)
    high_since: int = 0           # count where the magnet last left the sensor
    selftest_pending: bool = False
    booted: bool = False          # power-up homing + self-test finished (or failed)
    selftests: int = 0
    verified: bool = True
    loss_streak: int = 0
    verify_tries: int = 0
    resyncs: int = 0
    max_correction: int = 0
    lost: int = 0                 # half-steps found missing at Hall checks (sum)
    rehomes: int = 0


class Motion:
    def __init__(self, *, steps_per_rev: int, flaps: int, max_pps: float, start_pps: float, accel: float,
                 hold_ms: float, hall_pins, offsets, bus: CoilBus, halls: HallInputs, home_timeout_s: float = 12.0,
                 resync_tolerance: int = 24, selftest: bool = True, reverse: bool = False,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print) -> None:
        self.spr = int(steps_per_rev)
        self.flaps = int(flaps)
        self.max_pps = float(max_pps)
        self.v0 = min(float(start_pps), self.max_pps)
        self.accel = float(accel)
        self.hold = hold_ms / 1000.0
        self.home_timeout = home_timeout_s
        self.tol = int(resync_tolerance)
        self.tick_s = 1.0 / self.max_pps     # loop period: at most one wake-up (one coil word) per tick
        self.selftest = bool(selftest)
        self.direction = -1 if reverse else 1
        self.bus, self.halls, self.clock, self.log = bus, halls, clock, log
        self.modules = [ModuleState(i, int(p), int(o), self.max_pps)
                        for i, (p, o) in enumerate(zip(hall_pins, offsets, strict=True))]
        self.lock = threading.RLock()
        self._wake = threading.Event()
        self._word: int | None = None
        self.homing = False
        self.words = 0                # coil words written (one per tick that changed something)

    # -- flap geometry ----------------------------------------------------------------------------
    def steps_for_flap(self, k: int) -> int:
        rev, j = divmod(int(k), self.flaps)
        return rev * self.spr + math.ceil(j * self.spr / self.flaps - 1e-9)   # never short of the flap edge

    def flap_at_or_after(self, steps: int) -> int:
        """Smallest flap whose position is ≥ ``steps`` (the flap a moving module reaches next)."""
        k = math.floor(steps * self.flaps / self.spr) - 1
        while self.steps_for_flap(k) < steps:
            k += 1
        return k

    @property
    def steps_per_flap(self) -> float:
        return self.spr / self.flaps

    @property
    def capacity(self) -> float:
        """Flaps per second one module turns at top speed."""
        return self.max_pps / self.steps_per_flap

    # -- motion profile (pure) --------------------------------------------------------------------
    def stop_distance(self, v: float) -> int:
        """Half-steps a module moving at ``v`` needs to brake to ``start_pps`` and stop."""
        if v <= self.v0:
            return 0
        return math.ceil((v * v - self.v0 * self.v0) / (2.0 * self.accel))

    def next_speed(self, v: float, remaining: int, cap: float) -> float:
        """Speed of the next step: start, accelerate, cruise or brake (``remaining`` incl. that step)."""
        if v <= 0:
            return self.v0
        if v > cap or remaining <= self.stop_distance(v):
            return max(self.v0, math.sqrt(max(0.0, v * v - 2.0 * self.accel)))
        return min(cap, math.sqrt(v * v + 2.0 * self.accel))

    def travel_time(self, steps: float, cap: float | None = None) -> float:
        """Seconds for a move of ``steps`` half-steps from rest to rest (trapezoid profile)."""
        if steps <= 0:
            return 0.0
        v0, a = self.v0, self.accel
        vmax = self.max_pps if cap is None else cap
        d_ramp = (vmax * vmax - v0 * v0) / (2.0 * a)
        if steps >= 2 * d_ramp:
            return 2.0 * (vmax - v0) / a + (steps - 2 * d_ramp) / vmax
        vp = math.sqrt(v0 * v0 + a * steps)
        return 2.0 * (vp - v0) / a

    def flap_time(self, flaps: float) -> float:
        """Seconds to turn ``flaps`` flaps from rest to rest."""
        return self.travel_time(flaps * self.steps_per_flap)

    def flaps_within(self, seconds: float) -> int:
        """Most whole flaps a module can turn (from rest, to rest) in ``seconds``."""
        if seconds <= 0:
            return 0
        n = int(self.capacity * seconds) + 1
        while n > 0 and self.flap_time(n) > seconds:
            n -= 1
        return n

    # -- commands (any thread) --------------------------------------------------------------------
    def start_homing(self) -> None:
        with self.lock:
            now = self.clock()
            for m in self.modules:
                m.home, m.home_t0, m.spin, m.last_hall = "seek_high", now, False, None
                m.v, m.next_t, m.verified = 0.0, None, True
                m.cap, m.selftests, m.selftest_pending, m.booted = self.max_pps, 0, False, False
            self.homing = True
        self._wake.set()

    def _base(self, m: ModuleState) -> int:
        """First flap a module can still stop on (it is moving: braking distance included)."""
        lead = m.steps + (self.stop_distance(m.v) if m.next_t is not None else 0)
        return self.flap_at_or_after(lead)

    def set_desired(self, i: int, digit: int, spin: bool = False, final: int | None = None,
                    time_left: float = 0.0) -> None:
        """Module ``i`` should show ``digit`` (turning forward the least) — or, spinning, keep turning
        at full speed and land on ``final`` about ``time_left`` seconds from now (whole extra turns
        of 10 flaps fill the time when there is time for them)."""
        with self.lock:
            m = self.modules[i]
            if m.home not in ("done", "failed"):
                m.goal = int(final if (spin and final is not None) else digit)   # digit kept for after re-home
                return
            base = self._base(m)
            m.spin = bool(spin)
            budget = self.flaps_within(max(0.0, time_left))
            if spin:
                end = int(digit if final is None else final)
                nearest = (end - base) % 10
                m.goal = base + nearest + 10 * max(0, (budget - nearest) // 10)
            else:
                digit = int(digit)
                m.goal = base + (digit - base) % 10
                if final is not None and time_left > 0:
                    # following a lagging digit would cost an extra turn after the count ends: go
                    # straight to the final digit instead (the count skips a few values on this module)
                    via = (digit - base) % 10 + (int(final) - digit) % 10
                    if via > budget + 1:
                        m.goal = base + (int(final) - base) % 10
        self._wake.set()

    # -- state (any thread) -----------------------------------------------------------------------
    def homed(self) -> bool:
        """Power-up homing (and self-test turn) finished on every module."""
        with self.lock:
            return all(m.booted for m in self.modules)

    def _at_rest(self, m: ModuleState) -> bool:
        at_goal = m.steps == self.steps_for_flap(m.goal) and m.next_t is None
        return at_goal and (m.home == "failed" or (m.home == "done" and m.verified))

    def settled(self) -> bool:
        """Every module stands on its goal flap and its position has been checked (verified)."""
        with self.lock:
            return all(self._at_rest(m) for m in self.modules)

    def shown_digits(self) -> list[int]:
        """Digit in each window (the last flap reached)."""
        with self.lock:
            out = []
            for m in self.modules:
                k = self.flap_at_or_after(m.steps)
                if self.steps_for_flap(k) > m.steps:
                    k -= 1
                out.append(k % 10)
            return out

    def stats(self) -> dict:
        with self.lock:
            return {"resyncs": sum(m.resyncs for m in self.modules), "lost": sum(m.lost for m in self.modules),
                    "rehomes": sum(m.rehomes for m in self.modules), "words": self.words,
                    "caps": [round(m.cap) for m in self.modules]}

    # -- closed loop ---------------------------------------------------------------------------------
    def _replan(self, m: ModuleState) -> None:
        """After the count was corrected: keep the goal's digit, on a flap it can still stop on."""
        base = self._base(m)
        if self.steps_for_flap(m.goal) < self.steps_for_flap(base):
            m.goal = base + (m.goal - base) % 10

    def _selftest_goal(self, m: ModuleState) -> int:
        """Digit-0 flap just after the next magnet pass (a full turn from the last edge)."""
        j = math.floor((m.edge_steps + m.offset) / self.spr) + 1
        return self.flaps * j

    def _loss(self, m: ModuleState, derate_now: bool) -> None:
        """A check found missing steps: re-ramp; repeated (or severe) → lower the module's top speed."""
        m.v = 0.0 if m.next_t is None else self.v0          # the rotor may be stalled: start again slowly
        m.loss_streak += 1
        if derate_now or m.loss_streak >= 2:
            new = max(self.v0, math.floor(m.cap * DERATE))
            if new < m.cap:
                m.cap = new
                m.verified = False
                self.log(f"DERATE m{m.index} -> {m.cap:.0f} half-steps/s (losing steps)")
            m.loss_streak = 0

    def _rehome(self, m: ModuleState, why: str) -> None:
        if m.home == "selftest" or m.selftest_pending:
            m.selftest_pending = True
            m.selftests += 1
        m.home, m.verified = "rehome", False
        m.rehomes += 1
        self.log(f"RESYNC m{m.index} {why} -> re-home")
        self._loss(m, derate_now=True)

    def _selftest_failed(self, m: ModuleState) -> None:
        """Losses during the power-up turn: slower, and another turn to prove the new speed."""
        m.selftests += 1
        if m.cap <= self.v0 or m.selftests > VERIFY_TRIES + 2:
            m.home, m.booted, m.goal = "done", True, self._selftest_goal(m) - self.flaps
            self._replan(m)
            self.log(f"SELFTEST m{m.index}: still losing steps at {m.cap:.0f} half-steps/s — check the module")
            return
        if m.home == "selftest":
            m.goal = self._selftest_goal(m)

    def _hall_edge(self, m: ModuleState) -> None:
        """Magnet arrived (1 → 0) at a known position: home, re-home or check the count."""
        if m.home == "seek_edge":
            m.steps, m.edge_steps = -m.offset, -m.offset
            self.log(f"HOME module {m.index}: Hall edge found")
            if self.selftest:
                m.home, m.goal = "selftest", self._selftest_goal(m)
            else:
                m.home, m.goal, m.booted = "done", 0, True
            return
        expected = round((m.steps + m.offset) / self.spr) * self.spr - m.offset
        err = m.steps - expected                     # > 0: the motor is behind the count (lost steps)
        if m.home == "rehome":
            m.steps, m.edge_steps = expected, expected
            self.log(f"HOME module {m.index}: re-homed")
            if m.selftest_pending and m.selftests <= VERIFY_TRIES + 2:
                m.home, m.goal = "selftest", self._selftest_goal(m)
            else:
                if m.selftest_pending:
                    self.log(f"SELFTEST m{m.index}: still losing steps — check the module")
                m.home, m.booted, m.selftest_pending = "done", True, False
                self._replan(m)
            return
        if m.home not in ("done", "selftest"):
            return
        if abs(err) > self.spr // 10:
            self._rehome(m, f"err={err:+d}")
            return
        if err:
            m.steps = expected
            m.resyncs += 1
            m.max_correction = max(m.max_correction, abs(err))
        m.edge_steps = expected
        if abs(err) > self.tol:
            m.lost += max(0, err)
            self.log(f"RESYNC m{m.index} err={err:+d}")
            m.verified, m.verify_tries = True, 0            # corrected here (a derate below re-asks a turn)
            self._loss(m, derate_now=m.home == "selftest")
            if m.home == "selftest":
                self._selftest_failed(m)
            else:
                self._replan(m)
        else:
            m.loss_streak = 0
            m.verified, m.verify_tries = True, 0
            if m.home == "selftest":
                m.home, m.selftest_pending, m.booted = "done", False, True
                self.log(f"SELFTEST m{m.index}: one turn at {m.cap:.0f} half-steps/s, no lost steps")

    def _check_overdue(self, m: ModuleState) -> None:
        """The count has reached the next magnet position and the magnet did not come: steps were lost."""
        since = m.steps - m.edge_steps
        if m.home in ("done", "selftest"):
            if since > self.spr + self.spr // 10:
                self._rehome(m, f"missed Hall edge ({since} half-steps since the last)")
            elif since >= self.spr:
                m.verified = False                   # may not rest here: turn on until the magnet comes
        elif m.home == "rehome" and since > 3 * self.spr:
            m.home, m.verified, m.selftest_pending, m.booted = "failed", True, False, True
            m.edge_steps = m.steps
            self._replan(m)
            self.log(f"HOME module {m.index}: FAILED (no Hall edge in 2 turns) — check the magnet/sensor; "
                     "position assumed")

    def _start_verify(self, m: ModuleState) -> None:
        """Turn on past the next magnet position (with margin) and land on the same digit again."""
        if m.verify_tries >= VERIFY_TRIES:
            m.verified = True
            self.log(f"VERIFY m{m.index}: FAILED after {VERIFY_TRIES} turns — check the motor/magnet")
            return
        m.verify_tries += 1
        since = m.steps - m.edge_steps
        edge = m.edge_steps + self.spr * max(1, math.ceil((since + 1) / self.spr) if since < self.spr else 1)
        k = m.goal + 10
        while self.steps_for_flap(k) < edge + self.spr // 10 + self.steps_per_flap / 2:
            k += 10
        m.goal = k
        self.log(f"VERIFY m{m.index}: turning past the magnet")

    # -- stepping ----------------------------------------------------------------------------------
    def _pattern(self, m: ModuleState) -> int:
        if not m.energised:
            return 0
        a, b, c, d = HALF_STEP[m.phase]
        return a | b << 1 | c << 2 | d << 3

    def _wants_to_move(self, m: ModuleState, now: float) -> bool:
        if m.home in ("seek_high", "seek_edge"):
            if now - m.home_t0 > self.home_timeout:
                m.home, m.steps, m.goal, m.edge_steps, m.booted = "failed", 0, 0, 0, True
                m.next_t, m.v = None, 0.0
                self.log(f"HOME module {m.index}: FAILED (no Hall edge in {self.home_timeout:.0f} s) "
                         "— check the magnet/sensor; position assumed")
                return False
            return True
        if m.home == "rehome":
            return True
        at_goal = m.steps >= self.steps_for_flap(m.goal)
        if m.home == "selftest" and at_goal and m.next_t is None:
            self._rehome(m, "no Hall edge at the end of the self-test turn")
            return True
        if m.home == "done" and at_goal and m.next_t is None and not m.verified:
            self._start_verify(m)
        return m.steps < self.steps_for_flap(m.goal)

    def _advance(self, m: ModuleState, now: float) -> None:
        """One half-step of a due module (or energise the held phase first) and its next deadline."""
        if not m.energised:                    # re-energise the held phase first (rotor did not move)
            m.energised = True
            m.v = 0.0
            m.next_t, m.last_step_t = now + 1.0 / self.v0, now
            return
        if m.v > 0 and m.next_t is not None:
            late = now - m.next_t
            if late > REST_PERIODS / m.v:
                m.v, m.next_t = 0.0, None          # far too late: the rotor has (nearly) stopped, ramp again
            elif late > 1.0 / m.v:                 # a whole step late: it slowed down, continue from there
                m.v, m.next_t = max(self.v0, m.v - LATE_DECEL * self.accel * late), None
        m.phase = (m.phase + self.direction) % 8
        m.steps += 1
        if m.home in ("seek_high", "seek_edge"):
            cap, remaining = self.v0, self.spr      # power-up homing: slow, so it can stop at the edge
        elif m.home == "rehome":
            cap, remaining = m.cap, self.spr
        else:
            cap, remaining = m.cap, self.steps_for_flap(m.goal) - m.steps
        if remaining <= 0:
            m.v, m.next_t, m.idle_since = 0.0, None, now
            return
        v = self.next_speed(m.v, remaining, cap)
        period = 1.0 / v
        due = m.next_t if m.next_t is not None else now
        nxt = max(due + period, now + 0.5 * self.tick_s)   # absolute deadlines, at most one step per tick
        m.v, m.next_t, m.last_step_t = v, nxt, now

    def tick(self, now: float) -> float | None:
        """One pass of the timing loop: read the Hall sensors, step every module that is due, write
        the coil word. Returns the time of the next deadline (None when nothing is moving)."""
        nxt: float | None = None
        with self.lock:
            for m in self.modules:
                level = int(self.halls.read(m.hall_pin))
                if level == 1 and m.last_hall != 1:
                    m.high_since = m.steps                  # magnet left the sensor here
                if m.home == "seek_high" and level == 1:
                    m.home = "seek_edge"
                elif m.last_hall == 1 and level == 0 and m.steps - m.high_since >= HALL_DEBOUNCE:
                    self._hall_edge(m)                      # (a rotor slipping back into the window is no edge)
                m.last_hall = level
                self._check_overdue(m)
                if self._wants_to_move(m, now):
                    if m.next_t is None or m.next_t <= now + 0.5 * self.tick_s:   # due within half a tick
                        self._advance(m, now)
                    m.idle_since = now
                    if m.next_t is not None:
                        nxt = m.next_t if nxt is None else min(nxt, m.next_t)
                    else:                               # just arrived: let the next pass decide
                        nxt = now if nxt is None else min(nxt, now)
                else:
                    if m.next_t is not None:            # goal moved behind it: stop here
                        m.v, m.next_t = 0.0, None
                    if m.energised and now - m.idle_since >= self.hold:
                        m.energised = False             # de-energise idle coils
                    elif m.energised:
                        due = m.idle_since + self.hold
                        nxt = due if nxt is None else min(nxt, due)
            if self.homing and all(m.booted for m in self.modules):
                self.homing = False
            word = 0
            for m in self.modules:
                word |= self._pattern(m) << (4 * m.index)
            if word != self._word:
                self.bus.write(word)
                self._word = word
                self.words += 1
        return nxt

    def release(self) -> None:
        """All coils off (shutdown)."""
        with self.lock:
            for m in self.modules:
                m.energised = False
                m.v, m.next_t = 0.0, None
            self.bus.write(0)
            self._word = 0

    def run(self, stop: threading.Event) -> None:
        """Stepping loop (own thread): wake at the earliest step deadline (rounded to the nearest tick,
        never twice within one tick), tick, repeat. Late wake-ups slow the modules down; they never
        produce a burst of steps."""
        while not stop.is_set():
            now = self.clock()
            nxt = self.tick(now)
            if nxt is None:                         # idle: wait for a command (or poll at 50 Hz)
                self._wake.wait(0.02)
                self._wake.clear()
                continue
            wake = max(nxt - 0.5 * self.tick_s, now + self.tick_s)
            while True:
                left = wake - self.clock()
                if left <= 0 or stop.is_set() or self._wake.is_set():
                    break
                time.sleep(min(left, 0.02))
            self._wake.clear()
