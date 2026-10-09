"""Parent side of the twin: start a runner subprocess, talk to it, stop it reliably.

Used by the web server (``WS /ws/twin``) and by :func:`piforge.twin.scenario.run_scenario`::

    s = TwinSession(config, Path("firmware/main.py"))
    s.start()
    s.wait_for(lambda m: m["op"] == "hello", 30)
    s.send_input("SW1", "pressed", True)
    s.wait_for(lambda m: m["devices"]["D1"]["brightness"] == 1.0, 2)
    s.stop()                       # ≤ 2 s even if the firmware never yields

Messages follow spec §5.6 (see :mod:`piforge.twin.ipc`). ``stop()`` asks politely
(``{"op":"stop"}`` → KeyboardInterrupt in the firmware), then sends SIGTERM, then SIGKILL to the
runner's whole process group, so firmware that spins, swallows ``KeyboardInterrupt`` or ignores
SIGTERM — and any child processes it spawned — are gone within the timeout.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import piforge
from piforge.core.errors import PiForgeError, ValidationError
from piforge.twin.config import TwinConfig
from piforge.twin.ipc import encode, exit_msg, input_msg, iter_messages, listen_localhost, stop_msg

log = logging.getLogger(__name__)

_PREDICATE_ERRORS = (KeyError, IndexError, TypeError, AttributeError, ValueError)


class TwinSession:
    """One firmware run in a ``python -m piforge.twin.runner`` subprocess.

    ``transport`` is ``"stdio"`` (default; protocol over the child's stdin/stdout) or ``"tcp"``
    (the session listens on 127.0.0.1 and passes ``--ipc HOST:PORT``). ``cwd`` defaults to the
    firmware's directory; ``duration`` stops the firmware after that many seconds; ``speed`` scales
    simulated time.
    """

    def __init__(self, config: TwinConfig, firmware: Path | str, *, cwd: Path | str | None = None,
                 transport: str = "stdio", duration: float | None = None, speed: float = 1.0,
                 python: str | None = None, env: dict[str, str] | None = None,
                 start_timeout: float = 30.0) -> None:
        if transport not in ("stdio", "tcp"):
            raise ValidationError(f"transport must be 'stdio' or 'tcp', got {transport!r}")
        if not speed > 0:
            raise ValidationError(f"speed must be > 0, got {speed!r}")
        self.config = config
        self.firmware = Path(firmware).expanduser().resolve()
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else self.firmware.parent
        self.transport = transport
        self.duration = duration
        self.speed = float(speed)
        self.python = python or sys.executable
        self.extra_env = dict(env or {})
        self.start_timeout = float(start_timeout)

        self._proc: subprocess.Popen | None = None
        self._tmpdir: Path | None = None
        self._out: IO[bytes] | None = None
        self._send_lock = threading.Lock()
        self._connected = threading.Event()
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._threads: list[threading.Thread] = []

        self._cond = threading.Condition()
        self._seq = 0
        self._history: deque[tuple[int, dict]] = deque(maxlen=5000)
        self._inbox: deque[dict] = deque(maxlen=20000)
        self._logs: deque[str] = deque(maxlen=10000)
        self._events: deque[dict] = deque(maxlen=1000)
        self._stderr: deque[str] = deque(maxlen=200)
        self._latest_state: dict = {}
        self._displays: dict[str, dict] = {}
        self._hello: dict | None = None
        self._exit: dict | None = None
        self._finished = False

    # -- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        """Spawn the runner (returns immediately; wait for ``hello`` with :meth:`wait_ready`)."""
        if self._proc is not None:
            raise PiForgeError("twin session already started; create a new TwinSession to run again")
        if not self.firmware.is_file():
            raise PiForgeError(f"firmware not found: {self.firmware}")
        if not self.cwd.is_dir():
            raise PiForgeError(f"working directory not found: {self.cwd}")
        self._tmpdir = Path(tempfile.mkdtemp(prefix="piforge-twin-"))
        cfg = self._tmpdir / "twin.json"
        cfg.write_text(self.config.to_json(), encoding="utf-8")
        argv = [self.python, "-m", "piforge.twin.runner", "--config", str(cfg), "--firmware", str(self.firmware),
                "--cwd", str(self.cwd)]
        if self.duration is not None:
            argv += ["--duration", repr(float(self.duration))]
        if self.speed != 1.0:
            argv += ["--speed", repr(self.speed)]
        env = dict(os.environ)
        src = str(Path(piforge.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        env.update(self.extra_env)
        popen_kw: dict[str, Any] = {"cwd": str(self.cwd), "env": env, "stdout": subprocess.PIPE,
                                    "stderr": subprocess.PIPE}
        if os.name == "posix":
            popen_kw["start_new_session"] = True          # own process group → killpg reaches children
        else:  # pragma: no cover - Windows
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        if self.transport == "stdio":
            self._proc = subprocess.Popen(argv + ["--stdio"], stdin=subprocess.PIPE, **popen_kw)
            self._out = self._proc.stdin
            self._connected.set()
            self._spawn(self._read_protocol, self._proc.stdout, name="twin-session-read")
        else:
            self._server = listen_localhost()
            port = self._server.getsockname()[1]
            self._proc = subprocess.Popen(argv + ["--ipc", f"127.0.0.1:{port}"], stdin=subprocess.DEVNULL,
                                          **popen_kw)
            self._spawn(self._accept, name="twin-session-accept")
            self._spawn(self._read_lines, self._proc.stdout, "runner-stdout", name="twin-session-stdout")
        self._spawn(self._read_lines, self._proc.stderr, "runner-stderr", name="twin-session-stderr")

    def _spawn(self, target: Callable[..., None], *args: Any, name: str) -> None:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _accept(self) -> None:
        assert self._server is not None and self._proc is not None
        srv = self._server
        srv.settimeout(0.2)
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline and self._proc.poll() is None:
            try:
                conn, _ = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._conn = conn
            self._out = conn.makefile("wb")
            self._connected.set()
            try:
                srv.close()
            except OSError:
                pass
            self._read_protocol(conn.makefile("rb"))
            return
        self._reader_finished()

    def _read_protocol(self, stream: IO[bytes]) -> None:
        try:
            for msg in iter_messages(stream):
                self._on_message(msg)
        finally:
            self._reader_finished()

    def _read_lines(self, stream: IO[bytes], label: str) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            with self._cond:
                self._stderr.append(line)
            log.debug("%s: %s", label, line)

    def _on_message(self, msg: dict) -> None:
        with self._cond:
            self._seq += 1
            self._history.append((self._seq, msg))
            self._inbox.append(msg)
            op = msg.get("op")
            if op == "state":
                self._latest_state = msg
            elif op == "hello":
                self._hello = msg
            elif op == "display":
                self._displays[str(msg.get("device"))] = msg
            elif op == "log":
                self._logs.append(str(msg.get("text", "")))
                if msg.get("stream") in ("twin", "protocol") and msg.get("code"):
                    self._events.append(msg)
            elif op == "exit":
                self._exit = msg
            self._cond.notify_all()

    def _reader_finished(self) -> None:
        """Protocol stream closed: make sure an ``exit`` message exists, then wake waiters."""
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
        with self._cond:
            if self._exit is None and proc is not None and proc.poll() is not None:
                rc = proc.returncode
                tail = "\n".join(list(self._stderr)[-20:])
                if rc == 0:                     # e.g. firmware called os._exit(0) itself
                    reason, err = "exited", None
                else:
                    reason = "killed" if rc is not None and rc < 0 else "runner-error"
                    err = f"twin runner ended without an exit message (return code {rc})"
                    if tail:
                        err += f"; stderr tail:\n{tail}"
                self._exit = exit_msg(rc if rc is not None else -1, err, reason)
                self._seq += 1
                self._history.append((self._seq, self._exit))
                self._inbox.append(self._exit)
            self._finished = True
            self._cond.notify_all()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the firmware and runner within ``timeout`` seconds (stop → SIGTERM → SIGKILL)."""
        proc = self._proc
        if proc is None:
            return
        t0 = time.monotonic()
        if proc.poll() is None:
            self._send_stop(min(0.2, 0.2 * timeout))
            if not self._wait_proc(t0 + 0.45 * timeout):
                self._signal(signal.SIGTERM)
                if not self._wait_proc(t0 + 0.75 * timeout):
                    self._signal(signal.SIGKILL)
                    if not self._wait_proc(t0 + timeout + 0.5):  # pragma: no cover - SIGKILL is immediate
                        log.error("twin runner pid %s survived SIGKILL", proc.pid)
        self._close_channel()
        for t in self._threads:
            t.join(timeout=max(0.1, min(1.0, t0 + timeout + 1.0 - time.monotonic())))
        with self._cond:
            if self._exit is None and proc.poll() is not None:
                self._exit = exit_msg(proc.returncode if proc.returncode is not None else -1,
                                      f"twin runner killed after not stopping within {timeout:g} s", "killed")
                self._seq += 1
                self._history.append((self._seq, self._exit))
                self._inbox.append(self._exit)
            self._finished = True
            self._cond.notify_all()
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def _send_stop(self, wait: float) -> None:
        """Send ``stop`` without letting a blocked channel (full pipe, busy ``_send_lock``) eat the
        stop budget: the write runs in a helper thread that dies with the channel at the latest."""
        def send() -> None:
            try:
                self._send(stop_msg(), wait=0.0)
            except PiForgeError:
                pass
        t = threading.Thread(target=send, name="twin-session-stop", daemon=True)
        t.start()
        t.join(wait)

    def _wait_proc(self, deadline: float) -> bool:
        assert self._proc is not None
        try:
            self._proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            return True
        except subprocess.TimeoutExpired:
            return False

    def _signal(self, sig: int) -> None:
        assert self._proc is not None
        if self._proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(self._proc.pid, sig)
            else:  # pragma: no cover - Windows
                self._proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _close_channel(self) -> None:
        for obj in (self._out, self._conn, self._server):
            if obj is not None:
                try:
                    obj.close()
                except OSError:
                    pass
        proc = self._proc
        if proc is not None:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and proc.poll() is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

    def __enter__(self) -> "TwinSession":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def __del__(self) -> None:  # last-resort: never leave an orphaned runner behind
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL) if os.name == "posix" else proc.kill()
            except Exception:
                pass

    # -- talking ---------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """True while the runner process is alive."""
        return self._proc is not None and self._proc.poll() is None

    def _send(self, msg: dict, wait: float | None = None) -> None:
        if not self._connected.wait(self.start_timeout if wait is None else wait):
            raise PiForgeError("twin runner has not connected yet")
        data = encode(msg)
        with self._send_lock:
            out = self._out
            if out is None:
                raise PiForgeError("twin session channel is closed")
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError) as exc:
                raise PiForgeError(f"twin runner is not accepting messages (it may have exited): {exc}") from None

    def send_input(self, device: str, prop: str, value: Any) -> None:
        """Send ``{"op":"input",…}``; invalid device/prop is reported back as a ``twin`` log."""
        if not self.running:
            detail = f" (exit: {self._exit})" if self._exit else ""
            raise PiForgeError(f"twin session is not running{detail}")
        self._send(input_msg(device, prop, value))

    def poll(self) -> list[dict]:
        """All messages received since the previous ``poll()`` (oldest first)."""
        with self._cond:
            out = list(self._inbox)
            self._inbox.clear()
            return out

    def latest_state(self) -> dict:
        """The most recent ``state`` message (``{}`` before the first one)."""
        with self._cond:
            return dict(self._latest_state)

    def wait_for(self, predicate: Callable[[dict], Any], timeout: float) -> dict | None:
        """Return the first message for which ``predicate(msg)`` is truthy, or None on timeout.

        Already-received messages are checked first — all of them except superseded ``state``
        messages (only the latest state counts, so a state condition reflects the present) — then
        every new message in order. Predicates may index freely: KeyError/TypeError/… count as False.
        Returns early (None) once the runner has exited and nothing matched.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            latest_state_seq = max((s for s, m in self._history if m.get("op") == "state"), default=-1)
            for seq, msg in list(self._history):
                if msg.get("op") == "state" and seq != latest_state_seq:
                    continue
                if self._match(predicate, msg):
                    return msg
            seen = self._seq
            while True:
                fresh = [(s, m) for s, m in self._history if s > seen]
                if fresh:
                    seen = fresh[-1][0]
                    for _, msg in fresh:
                        if self._match(predicate, msg):
                            return msg
                if self._finished:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.25))

    @staticmethod
    def _match(predicate: Callable[[dict], Any], msg: dict) -> bool:
        try:
            return bool(predicate(msg))
        except _PREDICATE_ERRORS:
            return False

    def wait_ready(self, timeout: float | None = None) -> dict | None:
        """Wait for ``hello`` (None if the runner exits first or the timeout passes)."""
        msg = self.wait_for(lambda m: m["op"] in ("hello", "exit"),
                            self.start_timeout if timeout is None else timeout)
        return msg if msg is not None and msg.get("op") == "hello" else None

    # -- results -----------------------------------------------------------------------------------
    @property
    def hello(self) -> dict | None:
        """The runner's ``hello`` message (device descriptions)."""
        return self._hello

    @property
    def exit_message(self) -> dict | None:
        """The ``exit`` message (synthesised if the runner had to be killed)."""
        return self._exit

    @property
    def displays(self) -> dict[str, dict]:
        """Latest ``display`` message per device id."""
        with self._cond:
            return dict(self._displays)

    def logs(self) -> list[str]:
        """All log lines received so far (firmware stdout/stderr and twin diagnostics)."""
        with self._cond:
            return list(self._logs)

    def events(self) -> list[dict]:
        """Twin diagnostics (``TWIN.*`` log messages with ``code``/``level``)."""
        with self._cond:
            return list(self._events)

    def stderr_tail(self) -> list[str]:
        """Last lines the runner wrote outside the protocol (startup failures)."""
        with self._cond:
            return list(self._stderr)
