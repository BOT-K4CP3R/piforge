"""``/ws/twin`` bridge: one TwinSession per server, messages pumped to every websocket client.

Most tests inject a fake session (protocol per spec §5.6) so the bridge logic is tested on its own;
``test_ws_twin_real_firmware`` runs the real digital twin on the fixture gpiozero firmware.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from websockets.sync.client import connect

from .fixture_build import live_server

pytestmark = pytest.mark.timeout(120)


class FakeSession:
    """Mimics ``piforge.twin.session.TwinSession`` (start/stop/running/send_input/poll)."""

    def __init__(self, *, crash_after_start: bool = False) -> None:
        self.started = False
        self.stopped = False
        self.exited = False
        self.inputs: list[tuple] = []
        self._q: list[dict] = []
        self._lock = threading.Lock()
        self._crash = crash_after_start

    def _push(self, msg: dict) -> None:
        with self._lock:
            self._q.append(msg)

    def start(self) -> None:
        self.started = True
        self._push({"op": "hello", "devices": [
            {"id": "SW1", "type": "button", "inputs": {"pressed": {"type": "bool"}}, "outputs": {}},
            {"id": "D1", "type": "led", "inputs": {},
             "outputs": {"brightness": {"type": "float", "min": 0, "max": 1}}}]})
        self._push({"op": "state", "t": 0.0, "pins": {"17": 0, "27": 1},
                    "devices": {"SW1": {"pressed": False}, "D1": {"brightness": 0.0}}})
        if self._crash:
            self._push({"op": "log", "stream": "stderr", "text": "Traceback ...\nZeroDivisionError"})
            self._push({"op": "exit", "code": 1, "error": "ZeroDivisionError: division by zero"})
            self.exited = True

    @property
    def running(self) -> bool:
        return self.started and not self.stopped and not self.exited

    def send_input(self, device: str, prop: str, value: object) -> None:
        self.inputs.append((device, prop, value))
        if device == "SW1" and prop == "pressed":
            on = bool(value)
            self._push({"op": "state", "t": 0.5, "pins": {"17": int(on), "27": int(not on)},
                        "devices": {"SW1": {"pressed": on}, "D1": {"brightness": 1.0 if on else 0.0}}})

    def stop(self, timeout: float = 2.0) -> None:
        if not self.stopped:
            self.stopped = True
            self._push({"op": "exit", "code": 0, "error": None})

    def poll(self) -> list[dict]:
        with self._lock:
            out, self._q = self._q, []
        return out


def _ws_url(base: str) -> str:
    return base.replace("http://", "ws://") + "/ws/twin"


def recv_until(ws, pred: Callable[[dict], bool], timeout: float = 5.0) -> dict:
    """Receive JSON messages until ``pred`` matches; fails after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    seen = []
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"no matching message within {timeout} s; got {seen[-8:]}")
        try:
            msg = json.loads(ws.recv(timeout=left))
        except TimeoutError:
            continue
        seen.append(msg)
        if pred(msg):
            return msg


def is_status(running: bool) -> Callable[[dict], bool]:
    return lambda m: m.get("op") == "status" and m.get("running") is running


def led_on(m: dict) -> bool:
    if m.get("op") != "state":
        return False
    d1 = (m.get("devices") or {}).get("D1") or {}
    return float(d1.get("brightness") or 0) >= 0.99 or d1.get("on") is True or d1.get("value") == 1


@pytest.fixture
def fake_app(fixture_project: Path):
    from piforge.server.app import create_app

    app = create_app(fixture_project)
    made: list[FakeSession] = []

    def factory() -> FakeSession:
        s = FakeSession()
        made.append(s)
        return s

    app.state.piforge.twin.session_factory = factory
    app.state.made = made
    return app


def test_ws_twin_fake_roundtrip(fake_app):
    with live_server(fake_app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        recv_until(ws, is_status(True))
        hello = recv_until(ws, lambda m: m.get("op") == "hello")
        assert [d["id"] for d in hello["devices"]] == ["SW1", "D1"]
        ws.send(json.dumps({"op": "input", "device": "SW1", "prop": "pressed", "value": True}))
        recv_until(ws, led_on)
        assert fake_app.state.made[0].inputs == [("SW1", "pressed", True)]
        ws.send(json.dumps({"op": "stop"}))
        t0 = time.monotonic()
        st = recv_until(ws, is_status(False), timeout=3)
        assert time.monotonic() - t0 < 3
        assert st["error"] is None
        assert fake_app.state.made[0].stopped


def test_ws_twin_start_while_running_restarts(fake_app):
    with live_server(fake_app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        recv_until(ws, lambda m: m.get("op") == "hello")
        ws.send(json.dumps({"op": "start"}))
        recv_until(ws, lambda m: m.get("op") == "hello")
        made = fake_app.state.made
        assert len(made) == 2
        assert made[0].stopped and not made[1].stopped


def test_ws_twin_late_client_gets_hello_and_state(fake_app):
    with live_server(fake_app) as base, connect(_ws_url(base)) as ws1:
        recv_until(ws1, is_status(False))
        ws1.send(json.dumps({"op": "start"}))
        recv_until(ws1, lambda m: m.get("op") == "state")
        with connect(_ws_url(base)) as ws2:
            recv_until(ws2, is_status(True))
            recv_until(ws2, lambda m: m.get("op") == "hello")
            recv_until(ws2, lambda m: m.get("op") == "state")
            # inputs from one client are visible to every client
            ws2.send(json.dumps({"op": "input", "device": "SW1", "prop": "pressed", "value": True}))
            recv_until(ws1, led_on)
            recv_until(ws2, led_on)


def test_ws_twin_last_client_disconnect_stops_session(fake_app):
    with live_server(fake_app) as base:
        with connect(_ws_url(base)) as ws:
            recv_until(ws, is_status(False))
            ws.send(json.dumps({"op": "start"}))
            recv_until(ws, lambda m: m.get("op") == "hello")
        session = fake_app.state.made[0]
        deadline = time.monotonic() + 3
        while not session.stopped and time.monotonic() < deadline:
            time.sleep(0.05)
        assert session.stopped


def test_ws_twin_input_without_session_reports_error(fake_app):
    with live_server(fake_app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "input", "device": "SW1", "prop": "pressed", "value": True}))
        st = recv_until(ws, is_status(False))
        assert "not running" in st["error"]


def test_ws_twin_bad_messages_keep_connection(fake_app):
    with live_server(fake_app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send("this is not json")
        st = recv_until(ws, lambda m: m.get("op") == "status" and m.get("error"))
        assert "JSON" in st["error"]
        ws.send(json.dumps({"op": "dance"}))
        st = recv_until(ws, lambda m: m.get("op") == "status" and m.get("error"))
        assert "dance" in st["error"]
        ws.send(json.dumps({"op": "start"}))
        recv_until(ws, is_status(True))


def test_ws_twin_factory_error_reported(fixture_project: Path):
    from piforge.core.errors import PiForgeError
    from piforge.server.app import create_app

    app = create_app(fixture_project)

    def factory():
        raise PiForgeError("No firmware configured for this project")

    app.state.piforge.twin.session_factory = factory
    with live_server(app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        st = recv_until(ws, lambda m: m.get("op") == "status" and m.get("error"))
        assert st["running"] is False
        assert "No firmware" in st["error"]


def test_ws_twin_firmware_crash_forwarded(fixture_project: Path):
    from piforge.server.app import create_app

    app = create_app(fixture_project)
    app.state.piforge.twin.session_factory = lambda: FakeSession(crash_after_start=True)
    with live_server(app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        ex = recv_until(ws, lambda m: m.get("op") == "exit")
        assert ex["code"] == 1
        st = recv_until(ws, is_status(False))
        assert "ZeroDivisionError" in st["error"]


def test_default_factory_without_twin_config(tmp_path: Path):
    """No build → starting the twin explains what is missing instead of crashing."""
    from piforge.server.app import create_app

    proj = tmp_path / "no build"
    proj.mkdir()
    app = create_app(proj)
    with live_server(app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        st = recv_until(ws, lambda m: m.get("op") == "status" and m.get("error"))
        assert st["running"] is False
        assert "twin" in st["error"].lower()


def test_ws_twin_real_firmware(fixture_project: Path):
    """Real digital twin: start → hello → press SW1 → LED D1 on → stop → not running within 3 s."""
    pytest.importorskip("piforge.twin.session")
    from piforge.server.app import create_app

    app = create_app(fixture_project)
    with live_server(app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        hello = recv_until(ws, lambda m: m.get("op") == "hello", timeout=30)
        assert {"SW1", "D1", "SERVO1"} <= {d["id"] for d in hello["devices"]}
        recv_until(ws, lambda m: m.get("op") == "state", timeout=10)
        ws.send(json.dumps({"op": "input", "device": "SW1", "prop": "pressed", "value": True}))
        recv_until(ws, led_on, timeout=10)
        ws.send(json.dumps({"op": "stop"}))
        t0 = time.monotonic()
        recv_until(ws, is_status(False), timeout=3)
        assert time.monotonic() - t0 < 3


def test_watched_scenario_run_streams_state(fake_app):
    """POST /api/twin/scenarios/run {"watch": true}: the live twin stops, the scenario's own session is
    streamed (tagged ``scenario``) between ``scenario`` start/end/done messages; start is refused meanwhile."""
    pytest.importorskip("piforge.twin.scenario")
    import httpx

    with live_server(fake_app) as base, connect(_ws_url(base)) as ws:
        recv_until(ws, is_status(False))
        ws.send(json.dumps({"op": "start"}))
        recv_until(ws, is_status(True))
        result: dict = {}

        def post() -> None:
            r = httpx.post(base + "/api/twin/scenarios/run", json={"watch": True}, timeout=120)
            result["status"], result["json"] = r.status_code, r.json()

        th = threading.Thread(target=post)
        th.start()
        recv_until(ws, is_status(False), timeout=10)  # the live session was stopped
        start = recv_until(ws, lambda m: m.get("op") == "scenario", timeout=20)
        assert start["phase"] == "start" and start["name"] == "button_lights_led"
        assert start["index"] == 0 and start["total"] == 1 and start["duration"] == 2.0
        ws.send(json.dumps({"op": "start"}))  # refused while the scenario runs
        refused = recv_until(ws, lambda m: m.get("op") == "status" and m.get("error"), timeout=10)
        assert "scenario" in refused["error"].lower() and refused["running"] is False
        hello = recv_until(ws, lambda m: m.get("op") == "hello", timeout=30)
        assert hello["scenario"] == "button_lights_led"
        lit = recv_until(ws, led_on, timeout=20)
        assert lit["scenario"] == "button_lights_led" and isinstance(lit["t"], (int, float))
        end = recv_until(ws, lambda m: m.get("op") == "scenario" and m.get("phase") == "end", timeout=30)
        assert end["ok"] is True and end["name"] == "button_lights_led"
        done = recv_until(ws, lambda m: m.get("op") == "scenario" and m.get("phase") == "done", timeout=30)
        assert done["ok"] is True and done["error"] is None and done["results"] == [{"name": "button_lights_led", "ok": True}]
        th.join(30)
        assert result["status"] == 200 and result["json"]["ok"] is True
        assert fake_app.state.piforge.twin.scenario_active is False
        ws.send(json.dumps({"op": "start"}))  # allowed again
        recv_until(ws, is_status(True), timeout=10)


def test_watch_twin_sessions_only_reports_this_thread():
    pytest.importorskip("piforge.twin.session")
    import piforge.twin.session as session_mod
    from piforge.server.twin_bridge import watch_twin_sessions
    from piforge.twin.config import TwinConfig

    orig = session_mod.TwinSession
    seen: list = []
    cfg = TwinConfig(board="rpi4b", devices=[])
    with watch_twin_sessions(seen.append):
        a = session_mod.TwinSession(cfg, __file__)
        other: list = []
        t = threading.Thread(target=lambda: other.append(session_mod.TwinSession(cfg, __file__)))
        t.start()
        t.join()
    assert seen == [a] and isinstance(a, orig) and isinstance(other[0], orig)
    assert session_mod.TwinSession is orig
