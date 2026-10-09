"""VirtualPi: the Raspberry Pi's 28 BCM GPIO lines with net-level resolution.

Every line is resolved from its drivers each time something changes:

1. *strong* drivers — the Pi itself when the pin is an output, and devices that push a level
   (``drive(..., weak=False)``); if they disagree that is a short → ``TWIN.CONTENTION`` event and
   the line reads LOW (a GPIO fighting a contact to GND ends up near 0 V);
2. *weak* drivers — physical pull resistors (``set_external_pull``, the 1.8 kΩ pull-ups on
   GPIO2/3, ``drive(..., weak=True)`` e.g. a module's on-board pull-up);
3. the Pi's internal pull (``setup(..., pull=...)``);
4. otherwise the line floats: it keeps its last level and a ``TWIN.FLOATING_INPUT`` warning is
   recorded once (on real hardware it would read noise).

Listeners ``cb(bcm, level, t)`` fire synchronously on every resolved level change, with ``t`` the
simulated time of the edge (for scheduled edges: the *scheduled* time, so pulse widths are exact
regardless of thread latency). A small event scheduler (``call_at``/``call_later``) lets devices
produce timed waveforms (HC-SR04 echo, encoder quadrature, contact bounce); due events are run by
the twin's sim thread and also lazily on every :meth:`VirtualPi.read`, so a polling loop sees
edges exactly when it looks.

All state is guarded by one re-entrant lock (``pi.lock``) shared with the buses and devices.
Listeners must be quick and must not block.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from piforge.core.errors import ValidationError
from piforge.twin.bus import SPI_CS_GPIO, I2CBus, SPIBus
from piforge.twin.clock import SimClock

log = logging.getLogger(__name__)

GPIO_COUNT = 28  # BCM GPIO0–27 on the 40-pin header  # src: raspberrypi.com/documentation GPIO pinout

# Reset-state pulls: GPIO0–8 pull-up, GPIO9–27 pull-down.  # src: BCM2711 ARM Peripherals §5.3, "Pull" column
DEFAULT_PULL = {bcm: ("up" if bcm <= 8 else "down") for bcm in range(GPIO_COUNT)}

# 1.8 kΩ pull-ups to 3V3 on SDA1/SCL1.  # src: Raspberry Pi 4B reduced schematics (R? 1K8 on GPIO2/3)
BOARD_PULLUPS = {2: "up", 3: "up"}

# Physical header pin → BCM.  # src: raspberrypi.com/documentation/computers/raspberry-pi.html (GPIO pinout)
PHYS_TO_BCM = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22, 16: 23, 18: 24, 19: 10,
    21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 27: 0, 28: 1, 29: 5, 31: 6, 32: 12, 33: 13, 35: 19,
    36: 16, 37: 26, 38: 20, 40: 21,
}
BCM_TO_PHYS = {v: k for k, v in PHYS_TO_BCM.items()}

# I2C bus → (SDA, SCL) BCM pins.  # src: raspberrypi.com docs + /boot/overlays/README (i2c3..i2c6 defaults)
I2C_PINS = {0: (0, 1), 1: (2, 3), 3: (4, 5), 4: (6, 7), 5: (12, 13), 6: (22, 23)}

_MODES = {"input": "input", "in": "input", "output": "output", "out": "output"}
__all__ = ["VirtualPi", "GPIO_COUNT", "PHYS_TO_BCM", "BCM_TO_PHYS", "SPI_CS_GPIO", "I2C_PINS"]

_PULLS = {"up": "up", "down": "down", "none": "none", "off": "none", "floating": "none", None: "none"}
MAX_EVENTS = 2000

Listener = Callable[[int, int, float], None]


@dataclass
class _Pin:
    mode: str = "input"
    pull: str = "none"
    out_level: int = 0
    pwm_freq: float | None = None
    pwm_duty: float | None = None
    drives: dict[str, tuple[int, bool]] = field(default_factory=dict)  # source → (level, weak)
    ext_pull: str | None = None
    level: int = 0
    floating: bool = False
    warned_floating: bool = False
    contention: bool = False
    listeners: list[Listener] = field(default_factory=list)


class VirtualPi:
    """A virtual Raspberry Pi GPIO header plus I2C/SPI buses and an event scheduler."""

    def __init__(self, board: str = "rpi4b", clock: SimClock | None = None) -> None:
        self.board = board
        self.clock = clock or SimClock()
        self.lock = threading.RLock()
        self._pins = [_Pin(pull=DEFAULT_PULL[b]) for b in range(GPIO_COUNT)]
        for bcm, pull in BOARD_PULLUPS.items():
            self._pins[bcm].ext_pull = pull
        for bcm, st in enumerate(self._pins):
            st.level, _, _ = self._resolve(st)
        self.events: list[dict] = []
        self.event_listeners: list[Callable[[dict], None]] = []
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()
        self.on_schedule: Callable[[float], None] | None = None
        self.i2c: dict[int, I2CBus] = {n: I2CBus(n, lock=self.lock) for n in (0, 1)}
        self.spi: dict[int, SPIBus] = {n: SPIBus(n, pi=self) for n in (0, 1)}

    # -- helpers ----------------------------------------------------------------------------
    def _check(self, bcm: int) -> _Pin:
        if isinstance(bcm, bool) or not isinstance(bcm, int) or not 0 <= bcm < GPIO_COUNT:
            raise ValidationError(
                f"GPIO{bcm} does not exist on {self.board}: valid BCM numbers are 0–27")
        return self._pins[bcm]

    def i2c_bus(self, num: int) -> I2CBus:
        """Return I2C bus ``num``, creating it on first use (e.g. i2c3–6 overlays on a Pi 4)."""
        with self.lock:
            if num not in self.i2c:
                self.i2c[num] = I2CBus(num, lock=self.lock)
            return self.i2c[num]

    def record_event(self, code: str, level: str, message: str, **data: object) -> dict:
        """Append a twin diagnostic (``TWIN.*``) and notify event listeners.

        Callers often hold ``pi.lock``, so listeners must be quick and non-blocking (the runner
        only queues the event; its publisher thread writes it to the protocol channel).
        """
        ev = {"code": code, "level": level, "message": message, "t": round(self.clock.now(), 6), **data}
        with self.lock:
            self.events.append(ev)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            listeners = list(self.event_listeners)
        for cb in listeners:
            try:
                cb(ev)
            except Exception:  # pragma: no cover - diagnostics must never break the twin
                log.exception("twin event listener failed")
        return ev

    # -- level resolution ---------------------------------------------------------------------
    @staticmethod
    def _pi_level(st: _Pin) -> int:
        if st.pwm_freq is not None and st.pwm_duty is not None:
            return 1 if st.pwm_duty >= 0.5 else 0
        return st.out_level

    def _resolve(self, st: _Pin) -> tuple[int, bool, bool]:
        """→ (level, floating, contention)."""
        strong = [lv for lv, weak in st.drives.values() if not weak]
        if st.mode == "output":
            strong.append(self._pi_level(st))
        if strong:
            if len(set(strong)) > 1:
                return 0, False, True
            return strong[0], False, False
        weak_levels = [lv for lv, weak in st.drives.values() if weak]
        if st.ext_pull is not None:
            weak_levels.append(1 if st.ext_pull == "up" else 0)
        if weak_levels:
            return (weak_levels[0] if len(set(weak_levels)) == 1 else 0), False, False
        if st.pull in ("up", "down"):
            return (1 if st.pull == "up" else 0), False, False
        return st.level, True, False

    def _update(self, bcm: int, t: float | None = None) -> None:
        st = self._pins[bcm]
        level, floating, contention = self._resolve(st)
        st.floating = floating
        if not floating:
            st.warned_floating = False
        if contention and not st.contention:
            sources = ["pi"] if st.mode == "output" else []
            sources += [s for s, (_, weak) in st.drives.items() if not weak]
            self.record_event("TWIN.CONTENTION", "warning",
                              f"GPIO{bcm}: drivers disagree ({', '.join(sources)}) — short circuit; "
                              f"line forced LOW", bcm=bcm, sources=sources)
        st.contention = contention
        if level != st.level:
            st.level = level
            when = self.clock.now() if t is None else t
            for cb in list(st.listeners):
                try:
                    cb(bcm, level, when)
                except Exception as exc:
                    log.exception("GPIO%d listener failed", bcm)
                    self.record_event("TWIN.LISTENER_ERROR", "error", f"GPIO{bcm} listener failed: {exc}",
                                      bcm=bcm)

    # -- Pi side (firmware) -------------------------------------------------------------------
    def setup(self, bcm: int, mode: str, pull: str = "none") -> None:
        """Configure a pin as ``input``/``output`` with internal pull ``up``/``down``/``none``."""
        m = _MODES.get(str(mode).lower())
        if m is None:
            raise ValidationError(f"GPIO mode must be 'input' or 'output', got {mode!r}")
        p = _PULLS.get(pull if pull is None else str(pull).lower())
        if p is None:
            raise ValidationError(f"pull must be 'up', 'down' or 'none', got {pull!r}")
        with self.lock:
            st = self._check(bcm)
            st.mode = m
            st.pull = p
            if m == "input":
                st.pwm_freq = st.pwm_duty = None
            self._update(bcm)

    def set_pull(self, bcm: int, pull: str) -> None:
        """Change only the internal pull of a pin."""
        p = _PULLS.get(pull if pull is None else str(pull).lower())
        if p is None:
            raise ValidationError(f"pull must be 'up', 'down' or 'none', got {pull!r}")
        with self.lock:
            self._check(bcm).pull = p
            self._update(bcm)

    def write(self, bcm: int, level: int) -> None:
        """Set the output latch (takes effect on the line while the pin is an output)."""
        with self.lock:
            st = self._check(bcm)
            st.out_level = 1 if level else 0
            self._update(bcm)

    def read(self, bcm: int) -> int:
        """Resolved logic level of the line (runs due scheduled events first)."""
        with self.lock:
            st = self._check(bcm)
            self.run_due()
            if st.floating and not st.warned_floating and st.mode == "input":
                st.warned_floating = True
                self.record_event("TWIN.FLOATING_INPUT", "warning",
                                  f"GPIO{bcm} is read while floating (no pull and nothing driving it); "
                                  f"real hardware returns noise", bcm=bcm)
            return st.level

    def set_pwm(self, bcm: int, freq: float | None, duty: float | None) -> None:
        """Enable PWM (``freq`` Hz, ``duty`` 0..1) on an output; ``None`` disables it."""
        with self.lock:
            st = self._check(bcm)
            if freq is None:
                st.pwm_freq = st.pwm_duty = None
            else:
                if freq <= 0:
                    raise ValidationError(f"PWM frequency must be > 0 Hz, got {freq}")
                st.pwm_freq = float(freq)
                st.pwm_duty = 0.0 if duty is None else min(1.0, max(0.0, float(duty)))
            self._update(bcm)

    # -- device side ---------------------------------------------------------------------------
    def drive(self, bcm: int, source: str, level: int | None, *, weak: bool = False,
              t: float | None = None) -> None:
        """A device drives the line (``level`` 0/1) or releases it (``None``)."""
        with self.lock:
            st = self._check(bcm)
            if level is None:
                st.drives.pop(source, None)
            else:
                st.drives[source] = (1 if level else 0, bool(weak))
            self._update(bcm, t)

    def set_external_pull(self, bcm: int, pull: str | None) -> None:
        """Declare a physical pull resistor (``"up"``/``"down"``) or remove it (``None``)."""
        with self.lock:
            st = self._check(bcm)
            if pull is None:
                st.ext_pull = BOARD_PULLUPS.get(bcm)
            elif str(pull).lower() in ("up", "down"):
                st.ext_pull = str(pull).lower()
            else:
                raise ValidationError(f"external pull must be 'up' or 'down', got {pull!r}")
            self._update(bcm)

    def add_listener(self, bcm: int, cb: Listener) -> None:
        """Call ``cb(bcm, level, t)`` on every resolved level change of ``bcm``."""
        with self.lock:
            self._check(bcm).listeners.append(cb)

    def remove_listener(self, bcm: int, cb: Listener) -> None:
        """Remove a listener added with :meth:`add_listener` (no error if absent)."""
        with self.lock:
            lst = self._check(bcm).listeners
            if cb in lst:
                lst.remove(cb)

    # -- queries --------------------------------------------------------------------------------
    def mode(self, bcm: int) -> str:
        """``"input"`` or ``"output"``."""
        with self.lock:
            return self._check(bcm).mode

    def pwm(self, bcm: int) -> tuple[float | None, float | None]:
        """(frequency Hz, duty 0..1) of an active PWM output, else (None, None)."""
        with self.lock:
            st = self._check(bcm)
            if st.mode == "output" and st.pwm_freq is not None and st.pwm_duty is not None:
                return st.pwm_freq, st.pwm_duty
            return None, None

    def effective(self, bcm: int) -> float:
        """Average line level 0..1: PWM duty for PWM outputs, otherwise the logic level."""
        with self.lock:
            st = self._check(bcm)
            if st.mode == "output" and st.pwm_freq is not None and st.pwm_duty is not None \
                    and not st.contention:
                return st.pwm_duty
            return float(st.level)

    def snapshot(self) -> dict[int, dict]:
        """``{bcm: {"mode","pull","level","pwm_freq","pwm_duty"}}`` for all 28 lines."""
        with self.lock:
            return {b: {"mode": st.mode, "pull": st.pull, "level": st.level,
                        "pwm_freq": st.pwm_freq if st.mode == "output" else None,
                        "pwm_duty": st.pwm_duty if st.mode == "output" and st.pwm_freq is not None else None}
                    for b, st in enumerate(self._pins)}

    def pin_levels(self) -> dict[str, float | int]:
        """Compact ``{"17": 1, "18": 0.075, …}`` map for state messages (PWM pins report duty)."""
        with self.lock:
            out: dict[str, float | int] = {}
            for b in range(GPIO_COUNT):
                eff = self.effective(b)
                out[str(b)] = int(eff) if eff in (0.0, 1.0) else round(eff, 4)
            return out

    # -- scheduler --------------------------------------------------------------------------------
    def call_at(self, t: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` at simulated time ``t`` (by the sim thread or lazily on a read)."""
        with self.lock:
            heapq.heappush(self._heap, (float(t), next(self._seq), fn))
            notify = self.on_schedule
        if notify is not None:
            notify(float(t))

    def call_later(self, dt: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` ``dt`` simulated seconds from now."""
        self.call_at(self.clock.now() + max(0.0, dt), fn)

    def next_event_time(self) -> float | None:
        """Time of the earliest pending scheduled event, if any."""
        with self.lock:
            return self._heap[0][0] if self._heap else None

    def run_due(self, now: float | None = None) -> int:
        """Run every scheduled event with ``t <= now`` in time order; returns how many ran."""
        n = 0
        with self.lock:
            limit = self.clock.now() if now is None else now
            while self._heap and self._heap[0][0] <= limit:
                _, _, fn = heapq.heappop(self._heap)
                n += 1
                try:
                    fn()
                except Exception as exc:
                    log.exception("scheduled twin event failed")
                    self.record_event("TWIN.DEVICE_ERROR", "error", f"scheduled device event failed: {exc}")
        return n
