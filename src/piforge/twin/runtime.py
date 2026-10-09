"""The running twin: a VirtualPi, its devices, the sim thread and the firmware callback thread.

One :class:`Twin` is active per process (``get_twin()``/``set_twin()``); the runner creates it
before the firmware starts, tests create it directly. Shims and the gpiozero pin factory find it
through :func:`get_twin`.
"""

from __future__ import annotations

import logging
import math
import queue
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.twin.clock import SimClock
from piforge.twin.config import TwinConfig
from piforge.twin.devices import Device, create_device
from piforge.twin.gpio import VirtualPi

log = logging.getLogger(__name__)

_current: "Twin | None" = None
_current_lock = threading.Lock()


def set_twin(twin: "Twin | None") -> None:
    """Make ``twin`` the process-global twin (``None`` clears it)."""
    global _current
    with _current_lock:
        _current = twin


def get_twin() -> "Twin":
    """The process-global twin; raises a clear error outside the runner/tests."""
    twin = _current
    if twin is None:
        raise PiForgeError(
            "No active PiForge twin. Hardware shims (RPi.GPIO, smbus2, board, …) only work inside the "
            "twin runner (`python -m piforge.twin.runner` / `piforge twin run`) or after "
            "piforge.twin.runtime.set_twin(Twin(config)).")
    return twin


class CallbackDispatcher:
    """Runs firmware-library callbacks on one background thread, like RPi.GPIO's / lgpio's own
    callback threads: device models never block on firmware code, and callbacks run in order."""

    def __init__(self, name: str = "twin-gpio-callbacks") -> None:
        self.name = name
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue ``fn(*args)`` for the callback thread."""
        if self._stopped:
            return
        if self._thread is None:
            with self._lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                    self._thread.start()
        self._q.put((fn, args))

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except SystemExit:
                pass
            except BaseException:                     # mirror threading.excepthook: print, keep going
                traceback.print_exc(file=sys.stderr)

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until everything queued so far has run (tests)."""
        done = threading.Event()
        self.submit(done.set)
        return done.wait(timeout)

    def stop(self) -> None:
        """Stop the thread after the queued callbacks."""
        self._stopped = True
        self._q.put(None)


class Twin:
    """A virtual bench: :class:`VirtualPi` + device models + simulation thread."""

    TICK_HZ = 200.0  # device dynamics update rate (≥ 100 Hz required)

    def __init__(self, config: TwinConfig, *, clock: SimClock | None = None) -> None:
        self.config = config
        self.clock = clock or SimClock()
        self.pi = VirtualPi(config.board, self.clock)
        for bcm, pull in config.pulls.items():
            self.pi.set_external_pull(bcm, pull)
        self.devices: dict[str, Device] = {}
        for dc in config.devices:
            self.devices[dc.id] = create_device(dc, self.pi)
        for dev in self.devices.values():            # cross-device references (coil_source, stepper…)
            dev.link(self.devices)
        self._devices_started = False
        self.log: list[dict] = []
        self.dispatcher = CallbackDispatcher()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._last_tick = self.clock.now()
        self._tick_errors: set[str] = set()
        self.pi.on_schedule = self._wake

    # -- lifecycle ----------------------------------------------------------------------------
    def start(self) -> None:
        """Start the sim thread (no-op for a :class:`~piforge.twin.clock.ManualClock`).

        Devices' :meth:`~piforge.twin.devices.base.Device.on_start` hooks run on the first call
        (also for a ManualClock twin), e.g. ``ws_feed`` starts its WebSocket server."""
        if not self._devices_started:
            self._devices_started = True
            for dev in self.devices.values():
                dev.on_start()
        if self.clock.manual or self._thread is not None:
            return
        self._stop.clear()
        self._last_tick = self.clock.now()
        self._thread = threading.Thread(target=self._loop, name="twin-sim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the sim thread and the callback dispatcher."""
        self._stop.set()
        self._wake(0.0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.dispatcher.stop()
        if self._devices_started:
            self._devices_started = False
            for dev in self.devices.values():
                try:
                    dev.on_stop()
                except Exception:  # pragma: no cover - best effort
                    log.exception("device %s on_stop failed", dev.id)

    def env(self) -> dict[str, str]:
        """Environment variables contributed by the devices (``Device.env()``), merged in config order."""
        out: dict[str, str] = {}
        for dev in self.devices.values():
            out.update({str(k): str(v) for k, v in (dev.env() or {}).items()})
        return out

    @property
    def running(self) -> bool:
        """True while the sim thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def _wake(self, t: float) -> None:
        with self._cond:
            self._cond.notify_all()

    def _loop(self) -> None:
        period = 1.0 / self.TICK_HZ
        next_tick = self.clock.now() + period
        while not self._stop.is_set():
            now = self.clock.now()
            self.pi.run_due(now)
            if now >= next_tick:
                self._tick(now)
                next_tick += period
                if next_tick < now:
                    next_tick = now + period
            nxt = self.pi.next_event_time()
            target = next_tick if nxt is None else min(next_tick, nxt)
            wait = self.clock.to_real(target - self.clock.now())
            if wait > 0:
                with self._cond:
                    if not self._stop.is_set():
                        self._cond.wait(min(wait, 0.05))

    def _tick(self, now: float) -> None:
        dt = now - self._last_tick
        self._last_tick = now
        with self.pi.lock:
            for dev in self.devices.values():
                try:
                    dev.tick(now, dt)
                except Exception as exc:
                    if dev.id not in self._tick_errors:
                        self._tick_errors.add(dev.id)
                        log.exception("device %s tick failed", dev.id)
                        self.pi.record_event("TWIN.DEVICE_ERROR", "error",
                                             f"{dev.id}: model update failed: {exc}", device=dev.id)

    def step(self, dt: float) -> None:
        """Advance a ManualClock twin by ``dt`` s, running scheduled events and device ticks in order."""
        if not dt >= 0 or not math.isfinite(dt):
            raise ValidationError(f"step dt must be a finite number ≥ 0, got {dt!r}")
        if not self.clock.manual:
            raise PiForgeError("Twin.step() needs a ManualClock; a real-time twin advances by itself")
        period = 1.0 / self.TICK_HZ
        end = self.clock.now() + dt
        clock: Any = self.clock
        while True:
            t_tick = self._last_tick + period
            t_ev = self.pi.next_event_time()
            t = t_tick if t_ev is None else min(t_tick, t_ev)
            if t > end + 1e-12:
                break
            clock.set(max(t, clock.now()))
            self.pi.run_due(clock.now())
            if t >= t_tick - 1e-12:
                self._tick(clock.now())
        clock.set(max(end, clock.now()))
        self.pi.run_due(clock.now())

    # -- devices --------------------------------------------------------------------------------
    def device(self, dev_id: str) -> Device:
        """Device model by id (NotFoundError with suggestions otherwise)."""
        dev = self.devices.get(dev_id)
        if dev is None:
            raise NotFoundError("twin device", dev_id, self.devices)
        return dev

    def find_device(self, type: str | None = None, pin: int | None = None) -> Device | None:
        """First device matching ``type`` and/or wired to BCM ``pin`` (used by shims)."""
        for dev in self.devices.values():
            if type is not None and dev.type != type:
                continue
            if pin is not None and pin not in dev.pins.values():
                continue
            return dev
        return None

    def set_input(self, device: str, prop: str, value: Any) -> None:
        """Set a device input (validated); see each device's ``inputs``."""
        dev = self.device(device)
        dev.set_input(prop, value)
        self.log.append({"t": round(self.clock.now(), 4), "device": device, "prop": prop, "value": value})
        if len(self.log) > 1000:
            del self.log[:-1000]

    def state(self) -> dict:
        """``{"t", "pins": {"17": 1, …}, "devices": {id: {…}}}`` (spec §5.6 state payload)."""
        with self.pi.lock:
            self.pi.run_due()
            return {"t": round(self.clock.now(), 4), "pins": self.pi.pin_levels(),
                    "devices": {k: d.state() for k, d in self.devices.items()}}

    def describe(self) -> list[dict]:
        """Device descriptions for the ``hello`` message / GUI widget generation."""
        return [d.describe() for d in self.devices.values()]

    def displays(self) -> list[Device]:
        """Devices that render a picture (SSD1306, LCD…)."""
        return [d for d in self.devices.values() if d.is_display]
