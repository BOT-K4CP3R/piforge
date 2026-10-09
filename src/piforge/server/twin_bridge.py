"""Bridge between one digital-twin session (runner subprocess) and the browser websockets.

Protocol on ``/ws/twin`` (spec §5.6):

- client → server: ``{"op":"start"}``, ``{"op":"stop"}``, ``{"op":"input","device","prop","value"}``
- server → client: runner messages (``hello``/``state``/``display``/``log``/``exit``) plus
  ``{"op":"status","running":bool,"error":str|null}``.

One session per server; ``start`` while running restarts it; closing the last client stops it.

Watched scenario runs (``POST /api/twin/scenarios/run`` with ``"watch": true``): the live session is
stopped, and the messages of each scenario's own (headless) session are broadcast too, tagged with
``"scenario": name``, between progress messages
``{"op":"scenario","phase":"start"|"end"|"done", "name", "index", "total", "duration", "ok", ...}``,
so the GUI (3D view, split-flap widget, device cards) shows the scenario while it runs.
All session calls run on one dedicated worker thread (ordered, never concurrent); a pump task
polls the session every ~30 ms and broadcasts what arrived. Consecutive ``state`` messages of one
poll are merged and only the newest ``display`` frame per device is forwarded.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import json
import logging
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import WebSocket

from piforge.core.errors import PiForgeError
from piforge.core.report import jsonable

if TYPE_CHECKING:
    from piforge.server.state import AppState

log = logging.getLogger(__name__)

_LIVE_BRIDGES: weakref.WeakSet[TwinBridge] = weakref.WeakSet()
_watch_local = threading.local()
_swap_lock = threading.Lock()


@contextlib.contextmanager
def watch_twin_sessions(callback: Callable[[Any], None]):  # noqa: ANN201
    """Within the block, every ``TwinSession`` created **on this thread** is passed to ``callback``.

    ``run_scenario`` builds its session itself (``from piforge.twin.session import TwinSession`` at
    call time), so the class is swapped for a subclass that reports new instances. Sessions created
    on other threads meanwhile behave exactly like plain ``TwinSession``s.
    """
    import piforge.twin.session as session_mod

    with _swap_lock:
        orig = session_mod.TwinSession

        class WatchedTwinSession(orig):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                cb = getattr(_watch_local, "callback", None)
                if cb is not None:
                    cb(self)

        WatchedTwinSession.__name__ = WatchedTwinSession.__qualname__ = orig.__name__
        session_mod.TwinSession = WatchedTwinSession
        _watch_local.callback = callback
        try:
            yield
        finally:
            session_mod.TwinSession = orig
            _watch_local.callback = None


class SessionLike(Protocol):
    """What the bridge needs from :class:`piforge.twin.session.TwinSession`."""

    @property
    def running(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...
    def send_input(self, device: str, prop: str, value: Any) -> None: ...
    def poll(self) -> list[dict]: ...


class TwinUnavailableError(PiForgeError):
    """The twin cannot start: no build, no twin config, no firmware or no twin package."""


def merge_state(base: dict | None, new: dict) -> dict:
    """Merge a ``state`` message into an earlier one (pins and per-device dicts are updated)."""
    if base is None:
        return {**new, "pins": dict(new.get("pins") or {}),
                "devices": {k: dict(v) if isinstance(v, dict) else v
                            for k, v in (new.get("devices") or {}).items()}}
    out = {**base, **{k: v for k, v in new.items() if k not in ("pins", "devices")}}
    out["pins"] = {**(base.get("pins") or {}), **(new.get("pins") or {})}
    devices = {k: dict(v) if isinstance(v, dict) else v for k, v in (base.get("devices") or {}).items()}
    for dev, props in (new.get("devices") or {}).items():
        if isinstance(props, dict) and isinstance(devices.get(dev), dict):
            devices[dev].update(props)
        else:
            devices[dev] = dict(props) if isinstance(props, dict) else props
    out["devices"] = devices
    return out


@atexit.register
def _stop_all_sessions() -> None:  # safety net: never leave runner subprocesses behind
    for bridge in list(_LIVE_BRIDGES):
        bridge.kill_now()


class TwinBridge:
    """Owns at most one twin session and fans its messages out to websocket clients."""

    POLL_S = 0.03

    def __init__(self, state: AppState) -> None:
        self.state = state
        #: builds a new session; replaced in tests by a fake
        self.session_factory: Callable[[], SessionLike] = self.default_factory
        self.clients: set[WebSocket] = set()
        self._session: SessionLike | None = None
        self._alive = False
        self._error: str | None = None
        self._pump: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piforge-twin")
        self._hello: dict | None = None
        self._state: dict | None = None
        self._displays: dict[str, dict] = {}
        #: name of the scenario being watched (``None`` when no watched run is active)
        self.scenario: str | None = None
        self.scenario_active = False
        _LIVE_BRIDGES.add(self)

    # ------------------------------------------------------------------------- session factory
    def default_factory(self) -> SessionLike:
        """A real :class:`TwinSession` for ``build/twin/config.json`` + the manifest firmware."""
        st = self.state
        cfg_path = st.file("twin/config.json")
        if not cfg_path.is_file():
            raise TwinUnavailableError(
                "No digital twin configuration (build/twin/config.json) — build the project first.")
        fw = st.firmware_path()
        if fw is None:
            raise TwinUnavailableError(
                "The project has no firmware; add p.firmware('firmware/main.py') in project.py.")
        if not fw.is_file():
            raise TwinUnavailableError(f"Firmware file not found: {fw}")
        try:
            from piforge.twin.config import TwinConfig
            from piforge.twin.session import TwinSession
        except ImportError as exc:
            raise TwinUnavailableError(f"The digital twin package is not available: {exc}") from exc
        config = TwinConfig.from_json(cfg_path.read_text(encoding="utf-8"))
        return TwinSession(config, fw)  # cwd = the firmware's folder, as when run on the Pi

    # ------------------------------------------------------------------------- helpers
    @property
    def running(self) -> bool:
        return self._alive

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    def _status(self, error: str | None = None) -> dict:
        return {"op": "status", "running": self._alive, "error": error if error else self._error}

    @staticmethod
    async def _send(ws: WebSocket, msg: dict) -> bool:
        try:
            await ws.send_text(json.dumps(jsonable(msg)))
            return True
        except Exception:  # noqa: BLE001 — the client went away
            return False

    async def broadcast(self, msg: dict) -> None:
        """Send ``msg`` to every connected client."""
        data = json.dumps(jsonable(msg))
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)

    # ------------------------------------------------------------------------- clients
    async def connect(self, ws: WebSocket) -> None:
        """Accept a client and bring it up to date (status, last hello/state/display frames)."""
        await ws.accept()
        self.clients.add(ws)
        await self._send(ws, self._status())
        if self._alive:
            for msg in (self._hello, self._state, *self._displays.values()):
                if msg:
                    await self._send(ws, msg)

    async def disconnect(self, ws: WebSocket) -> None:
        """Forget a client; the last one leaving stops the session."""
        self.clients.discard(ws)
        if not self.clients and self._session is not None:
            await self.stop()

    async def handle_text(self, ws: WebSocket, text: str) -> None:
        """Dispatch one client message."""
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            await self._send(ws, self._status("Invalid JSON message; expected an object with 'op'."))
            return
        op = msg.get("op")
        if op == "start":
            await self.start()
        elif op == "stop":
            await self.stop()
        elif op == "input":
            await self._input(ws, msg)
        else:
            await self._send(ws, self._status(f"Unknown op {op!r}; use start, stop or input."))

    async def _input(self, ws: WebSocket, msg: dict) -> None:
        device, prop = msg.get("device"), msg.get("prop")
        if not isinstance(device, str) or not isinstance(prop, str):
            await self._send(ws, self._status("An input needs 'device' and 'prop' strings."))
            return
        session = self._session
        if session is None or not self._alive:
            await self._send(ws, self._status("The twin is not running — press Start first."))
            return
        try:
            await self._call(session.send_input, device, prop, msg.get("value"))
        except Exception as exc:  # noqa: BLE001 — unknown device/prop etc. → tell the client
            await self._send(ws, self._status(f"Input {device}.{prop} rejected: {exc}"))

    # ------------------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """(Re)start the twin session (refused while a watched scenario run is active)."""
        if self.scenario_active:
            await self.broadcast(self._status("A scenario run is in progress — wait until it has finished."))
            return
        async with self._lock:
            await self._stop_locked(announce=False)
            self._error, self._hello, self._state, self._displays = None, None, None, {}
            session: SessionLike | None = None
            try:
                session = await self._call(self.session_factory)
                await self._call(session.start)
            except Exception as exc:  # noqa: BLE001 — every start failure goes to the GUI
                log.warning("twin start failed: %s", exc)
                self._error = str(exc) or type(exc).__name__
                if session is not None:
                    with contextlib.suppress(Exception):
                        await self._call(session.stop)
                await self.broadcast(self._status())
                return
            self._session, self._alive = session, True
            await self.broadcast(self._status())
            self._pump = asyncio.ensure_future(self._pump_loop(session))

    async def stop(self) -> None:
        """Stop the session (no-op when stopped); clients get ``status`` running false."""
        async with self._lock:
            await self._stop_locked(announce=True)

    async def _stop_locked(self, *, announce: bool) -> None:
        session, pump = self._session, self._pump
        self._session, self._pump = None, None
        if pump is not None and pump is not asyncio.current_task():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        if session is None:
            return
        try:
            await self._call(session.stop)
        except Exception:  # noqa: BLE001
            log.exception("twin stop failed")
        with contextlib.suppress(Exception):
            await self._forward(await self._call(session.poll))
        self._alive, self._error = False, None
        if announce:
            await self.broadcast(self._status())

    async def _pump_loop(self, session: SessionLike) -> None:
        try:
            while True:
                exit_msg = await self._forward(await self._call(session.poll))
                if exit_msg is None and not await self._call(lambda: session.running):
                    exit_msg = await self._forward(await self._call(session.poll)) or {}
                if exit_msg is not None:
                    await self._ended(session, exit_msg)
                    return
                await asyncio.sleep(self.POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("twin pump failed")
            await self._ended(session, {"error": f"Twin bridge error: {exc}"})

    async def _ended(self, session: SessionLike, exit_msg: dict) -> None:
        """The session finished by itself (firmware returned/crashed, runner died)."""
        if self._session is not session:
            return  # already stopped/replaced by stop()/start()
        self._session, self._pump, self._alive = None, None, False
        code = exit_msg.get("code")
        self._error = exit_msg.get("error") or (
            f"Firmware exited with code {code}" if code not in (0, None) else None)
        with contextlib.suppress(Exception):
            await self._call(session.stop)
        await self.broadcast(self._status())

    async def _forward(self, msgs: list[dict] | None) -> dict | None:
        """Broadcast runner messages; returns the ``exit`` message when one arrived."""
        others: list[dict] = []
        state: dict | None = None
        displays: dict[str, dict] = {}
        exit_msg: dict | None = None
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            op = m.get("op")
            if op == "state":
                state = merge_state(state, m)
            elif op == "display":
                displays[str(m.get("device"))] = m
            elif op == "exit":
                exit_msg = m
            else:
                if op == "hello":
                    self._hello = m
                others.append(m)
        if state is not None:
            self._state = merge_state(self._state, state)
        self._displays.update(displays)
        for m in [*others, *([state] if state else []), *displays.values(),
                  *([exit_msg] if exit_msg else [])]:
            await self.broadcast(m)
        return exit_msg

    # ------------------------------------------------------------------------- watched scenarios
    async def begin_scenarios(self) -> None:
        """Before a watched scenario run: stop the live session, refuse ``start`` until the end."""
        self.scenario_active = True
        if self._session is not None:
            await self.stop()

    async def end_scenarios(self, msg: dict) -> None:
        """After a watched run: broadcast the final ``scenario`` message, allow ``start`` again."""
        self.scenario_active, self.scenario = False, None
        await self.broadcast({"op": "scenario", "phase": "done", **msg})

    def emit_threadsafe(self, loop: asyncio.AbstractEventLoop, msg: dict, timeout: float = 5.0) -> None:
        """Broadcast ``msg`` from a worker thread (waits until it has been sent)."""
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), loop).result(timeout)

    @contextlib.contextmanager
    def watch(self, loop: asyncio.AbstractEventLoop, name: str):  # noqa: ANN201
        """Worker-thread context: stream the messages of the sessions created inside it (one scenario)."""
        pumps: list[Any] = []
        done = threading.Event()
        self.scenario = name

        def on_session(session: Any) -> None:
            pumps.append(asyncio.run_coroutine_threadsafe(self._scenario_pump(session, name, done), loop))

        try:
            with watch_twin_sessions(on_session):
                yield
        finally:
            done.set()  # run_scenario has stopped its session: let the pumps flush and end
            for fut in pumps:
                with contextlib.suppress(Exception):
                    fut.result(10.0)

    async def _scenario_pump(self, session: Any, name: str, done: threading.Event) -> None:
        """Forward a scenario session's messages (tagged with ``scenario``) until it has ended."""
        while True:
            finished = done.is_set() and not session.running
            await self._forward_tagged(session.poll(), name)
            if finished:
                return
            await asyncio.sleep(self.POLL_S)

    async def _forward_tagged(self, msgs: list[dict], name: str) -> None:
        state: dict | None = None
        out: list[dict] = []
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            if m.get("op") == "state":
                state = merge_state(state, m)
                continue
            if state is not None:  # keep the order around non-state messages
                out.append(state)
                state = None
            out.append(m)
        if state is not None:
            out.append(state)
        for m in out:
            await self.broadcast({**m, "scenario": name})

    async def shutdown(self) -> None:
        """Server shutdown: stop the session and release the worker thread."""
        await self.stop()
        for ws in list(self.clients):
            with contextlib.suppress(Exception):
                await ws.close(code=1001)
        self.clients.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        _LIVE_BRIDGES.discard(self)

    def kill_now(self) -> None:
        """Synchronous last-resort stop (interpreter exit)."""
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                session.stop(timeout=1.0)
