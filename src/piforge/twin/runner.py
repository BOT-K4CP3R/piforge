"""Run real firmware against the twin: ``python -m piforge.twin.runner``.

::

    python -m piforge.twin.runner --config twin.json --firmware main.py [--cwd DIR]
                                  (--stdio | --ipc HOST:PORT) [--duration S] [--speed 1.0]

1. opens the protocol channel (spec §5.6) — stdin/stdout or a TCP connection to the parent;
2. captures *all* firmware output (``print``, ``sys.stderr``, and fd-level writes from C code or
   child processes) into ``{"op":"log"}`` messages so nothing raw ever reaches the protocol;
3. builds the :class:`~piforge.twin.runtime.Twin`, puts the hardware shims first on ``sys.path``,
   installs :class:`~piforge.twin.gpiozero_factory.TwinFactory` as gpiozero's pin factory, starts the
   devices (``Device.on_start``, e.g. ``ws_feed``'s WebSocket server) and exports their ``Device.env()``
   variables (e.g. ``MONEY_COUNTER_URL``) into ``os.environ``;
4. sends ``hello``, then runs the firmware with :mod:`runpy` as ``__main__`` in the main thread
   while background threads publish ``state``/``display`` messages (≤ 30 Hz) and apply ``input``s;
5. ``{"op":"stop"}``, ``--duration`` expiry or the parent going away raise ``KeyboardInterrupt`` in
   the firmware (SIGINT, so ``signal.pause()``/``sleep()`` wake up); the runner then reports
   ``{"op":"exit"}`` with the firmware's exit code and, for crashes, the traceback.

The parent (``TwinSession``) escalates to SIGTERM/SIGKILL if the firmware refuses to stop. If the
parent itself dies (channel EOF) and the firmware swallows the KeyboardInterrupt, a watchdog
SIGKILLs the runner's process group after ``PARENT_GONE_GRACE_S`` so no orphan is left behind.
"""

from __future__ import annotations

import argparse
import codecs
import collections
import io
import json
import os
import runpy
import select
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import IO, Any

from piforge.twin import SHIMS_DIR
from piforge.twin.ipc import MessageWriter, connect, exit_msg, is_protocol_warning, iter_messages, log_msg

STATE_HZ = 30.0          # spec §5.6: state messages at ≤ 30 Hz
DISPLAY_HZ = 15.0
HEARTBEAT_S = 1.0
MAX_LOG_LINE = 64 * 1024     # characters; longer output is split into several log messages
PARTIAL_FLUSH_S = 0.25       # fd-level output without a newline is flushed after this much silence
PARENT_GONE_GRACE_S = 2.0    # after the parent disappears, the runner is force-killed after this
MAX_PENDING_EVENTS = 1000
_real_sleep = time.sleep     # immune to --speed time warping

# Top-level names provided by the shims (anything already imported is purged before firmware runs).
SHIM_MODULES = ("RPi", "smbus2", "smbus", "spidev", "board", "busio", "digitalio", "pwmio", "microcontroller",
                "micropython", "neopixel", "picamera2", "libcamera", "adafruit_dht", "Adafruit_DHT",
                "w1thermsensor", "rpi_ws281x", "analogio", "pulseio", "adafruit_blinka")
SIMULATED = ("gpiozero, RPi.GPIO, smbus2/smbus, spidev, board/busio/digitalio/pwmio (Blinka), neopixel, "
             "rpi_ws281x, picamera2, adafruit_dht/Adafruit_DHT, w1thermsensor")


class _LogStream(io.TextIOBase):
    """``sys.stdout``/``sys.stderr`` replacement: complete lines become log messages."""

    def __init__(self, stream: str, writer: MessageWriter, fd: int) -> None:
        super().__init__()
        self._stream, self._writer, self._fd = stream, writer, fd
        self._buf = ""
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]
        return "replace"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._fd

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            raise TypeError(f"write() argument must be str, not {type(s).__name__}")
        with self._lock:
            self._buf += s
            while "\n" in self._buf or len(self._buf) > MAX_LOG_LINE:
                if "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                else:
                    line, self._buf = self._buf[:MAX_LOG_LINE], self._buf[MAX_LOG_LINE:]
                self._writer.send(log_msg(self._stream, line))
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._writer.send(log_msg(self._stream, self._buf))
                self._buf = ""


def _capture_fd(fd: int, stream: str, writer: MessageWriter) -> threading.Thread:
    """Point ``fd`` at a pipe whose lines are forwarded as log messages (C-level / child output).

    Reads in chunks (never "until newline"), so output without newlines — ``\\r`` progress bars,
    binary blobs — can never grow one unbounded line: lines are split at ``MAX_LOG_LINE`` characters
    and a partial line is flushed once the writer has been silent for ``PARTIAL_FLUSH_S``.
    """
    r, w = os.pipe()
    os.dup2(w, fd)
    os.close(w)

    def emit(text: str) -> None:
        writer.send(log_msg(stream, text[:-1] if text.endswith("\r") else text))

    def pump() -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        try:
            while True:
                if buf and not select.select([r], [], [], PARTIAL_FLUSH_S)[0]:
                    emit(buf)                   # idle with a partial line pending
                    buf = ""
                    continue
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                buf += dec.decode(chunk)
                while True:
                    nl = buf.find("\n", 0, MAX_LOG_LINE + 1)
                    if nl >= 0:
                        emit(buf[:nl])
                        buf = buf[nl + 1:]
                    elif len(buf) >= MAX_LOG_LINE:
                        emit(buf[:MAX_LOG_LINE])
                        buf = buf[MAX_LOG_LINE:]
                    else:
                        break
        except OSError:
            pass
        finally:
            buf += dec.decode(b"", final=True)
            if buf:
                emit(buf)
            try:
                os.close(r)
            except OSError:
                pass

    t = threading.Thread(target=pump, name=f"twin-capture-{stream}", daemon=True)
    t.start()
    return t


class Runner:
    """State of one runner process."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.writer: MessageWriter | None = None
        self.proto_in: IO[bytes] | None = None
        self.sock: Any = None
        self.twin: Any = None
        self.stop_reason: str | None = None
        self._stop_lock = threading.Lock()
        self._pumps: list[threading.Thread] = []
        self._done = threading.Event()
        # Firmware globals / exceptions are kept alive until os._exit: freeing them would run
        # gpiozero __del__ → close() (e.g. Button joins its hold thread for up to hold_time) and
        # would also reset outputs before the final state is published.
        self._keepalive: list[Any] = []
        # Twin diagnostics are often recorded under pi.lock: queue them, the publisher sends them.
        self._events: collections.deque[dict] = collections.deque(maxlen=MAX_PENDING_EVENTS)

    # -- channel ------------------------------------------------------------------------------------
    def open_channel(self) -> None:
        # Buffered streams: BufferedWriter retries partial writes (we SIGINT ourselves on stop).
        if self.args.stdio:
            out = os.fdopen(os.dup(1), "wb")
            self.proto_in = os.fdopen(os.dup(0), "rb")
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)                       # firmware reading stdin gets EOF, not protocol
            os.close(devnull)
        else:
            self.sock = connect(self.args.ipc)
            out = self.sock.makefile("wb")
            self.proto_in = self.sock.makefile("rb")
        self.writer = MessageWriter(out)
        self._pumps = [_capture_fd(1, "stdout", self.writer), _capture_fd(2, "stderr", self.writer)]
        sys.stdout = _LogStream("stdout", self.writer, 1)
        sys.stderr = _LogStream("stderr", self.writer, 2)
        sys.stdin = io.StringIO("")

    def send(self, msg: dict, **kw: Any) -> None:
        if self.writer is not None:
            self.writer.send(msg, **kw)

    # -- stop handling --------------------------------------------------------------------------------
    def request_stop(self, reason: str) -> None:
        """Interrupt the firmware (KeyboardInterrupt in the main thread)."""
        with self._stop_lock:
            if self.stop_reason is not None or self._done.is_set():
                return
            self.stop_reason = reason
        # Thread-directed: a process-directed SIGINT may land on a helper thread and leave a main
        # thread blocked in signal.pause()/sleep() untouched.
        try:
            signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)
        except (AttributeError, OSError, ValueError):  # pragma: no cover - non-POSIX
            os.kill(os.getpid(), signal.SIGINT)

    def _on_sigterm(self, signum: int, frame: Any) -> None:
        self.send(exit_msg(143, "runner terminated (SIGTERM)", "terminated"), blocking=False)
        os._exit(143)

    def _input_loop(self) -> None:
        assert self.proto_in is not None
        for msg in iter_messages(self.proto_in):
            op = msg.get("op")
            if op == "stop":
                self.request_stop("stopped")
            elif op == "input":
                try:
                    self.twin.set_input(str(msg.get("device")), str(msg.get("prop")), msg.get("value"))
                except Exception as exc:
                    self.send(log_msg("twin", f"input rejected: {exc}", level="error", code="TWIN.BAD_INPUT"))
            elif is_protocol_warning(msg):
                self.send(log_msg("twin", f"bad message from parent: {msg.get('text')}",
                                  level="warning", code=msg.get("code")))
            else:
                self.send(log_msg("twin", f"unknown op {op!r} ignored", level="warning", code="TWIN.BAD_OP"))
        self._parent_gone()

    def _parent_gone(self) -> None:
        """Channel EOF: the parent is gone. Stop politely, then make sure we never linger."""
        self.request_stop("parent closed the channel")
        _real_sleep(PARENT_GONE_GRACE_S)         # a well-behaved firmware has os._exit'ed by now
        self._hard_exit()

    def _hard_exit(self) -> None:
        """Firmware swallowed the stop: try a last exit message, then SIGKILL our process group."""
        msg = exit_msg(137, "firmware did not stop after the parent went away; runner killed", "killed")
        t = threading.Thread(target=self.send, args=(msg,), kwargs={"blocking": False}, daemon=True)
        t.start()
        t.join(0.2)                             # never block on a dead peer
        try:
            if os.name == "posix" and os.getpgrp() == os.getpid():   # own group (TwinSession)
                os.killpg(0, signal.SIGKILL)    # also reaps children the firmware spawned
        except OSError:
            pass
        os._exit(137)

    def _on_twin_event(self, ev: dict) -> None:
        """``VirtualPi`` event listener: called under ``pi.lock`` → only queue (never block)."""
        self._events.append(ev)

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self._events.popleft()
            except IndexError:
                return
            self.send(log_msg("twin", f"{ev['code']}: {ev['message']}", level=ev["level"],
                              code=ev["code"], data=ev))

    # -- publishing ------------------------------------------------------------------------------------
    def _publish_loop(self) -> None:
        last_key: str | None = None
        last_sent = 0.0
        disp_versions: dict[str, int] = {}
        disp_sent: dict[str, float] = {}
        period = 1.0 / STATE_HZ
        while not self._done.wait(period):
            now = time.monotonic()
            try:
                self._drain_events()
                last_key, last_sent = self._send_state(last_key, last_sent, now)
                self._send_displays(disp_versions, disp_sent, now)
            except Exception as exc:  # pragma: no cover - never let publishing kill the runner
                self.send(log_msg("twin", f"state publisher error: {exc}", level="error"))

    def _send_state(self, last_key: str | None, last_sent: float, now: float, force: bool = False
                    ) -> tuple[str | None, float]:
        st = self.twin.state()
        key = json.dumps([st["pins"], st["devices"]], sort_keys=True, default=str)
        if force or key != last_key or now - last_sent >= HEARTBEAT_S:
            self.send({"op": "state", **st})
            return key, now
        return last_key, last_sent

    def _send_displays(self, versions: dict[str, int], sent: dict[str, float], now: float,
                       force: bool = False) -> None:
        for dev in self.twin.displays():
            if versions.get(dev.id) == dev.display_version:
                continue
            if not force and now - sent.get(dev.id, 0.0) < 1.0 / DISPLAY_HZ:
                continue
            import base64

            versions[dev.id] = dev.display_version
            sent[dev.id] = now
            w, h, png = dev.render_png()
            self.send({"op": "display", "device": dev.id, "w": w, "h": h,
                       "png_b64": base64.b64encode(png).decode("ascii")})

    # -- environment for the firmware --------------------------------------------------------------------
    def install_shims(self) -> None:
        for name in list(sys.modules):
            if name.split(".", 1)[0] in SHIM_MODULES:
                del sys.modules[name]
        sys.path.insert(0, str(SHIMS_DIR))

    def install_time_warp(self, speed: float) -> None:
        """Scale the firmware's view of time (``--speed``); the twin's own clock is unaffected."""
        if speed == 1.0:
            return
        r_sleep, r_mono, r_time, r_perf = time.sleep, time.monotonic, time.time, time.perf_counter
        m0, w0, p0 = r_mono(), r_time(), r_perf()
        time.sleep = lambda s: r_sleep(max(0.0, s) / speed)                     # type: ignore[assignment]
        time.monotonic = lambda: m0 + (r_mono() - m0) * speed                   # type: ignore[assignment]
        time.time = lambda: w0 + (r_mono() - m0) * speed                        # type: ignore[assignment]
        time.perf_counter = lambda: p0 + (r_perf() - p0) * speed                # type: ignore[assignment]

    def run_firmware(self, fw: Path) -> tuple[int, str | None, str]:
        """Execute the firmware as ``__main__``; returns (code, error text, reason)."""
        sys.argv = [str(fw)]
        sys.path.insert(1, str(fw.parent))
        try:
            self._keepalive.append(runpy.run_path(str(fw), run_name="__main__"))
            return 0, None, "finished"
        except SystemExit as exc:
            self._keepalive.append(exc)
            code = exc.code
            if code is None:
                return 0, None, "sys.exit"
            if isinstance(code, int):
                return code, None, "sys.exit"
            print(code, file=sys.stderr)
            return 1, str(code), "sys.exit"
        except KeyboardInterrupt as exc:
            self._keepalive.append(exc)
            if self.stop_reason is not None:
                return 0, None, "duration" if self.stop_reason == "duration" else "stopped"
            return 130, "KeyboardInterrupt", "interrupted"
        except BaseException as exc:  # noqa: BLE001 - every firmware failure must be reported
            self._keepalive.append(exc)
            text = self.format_error(exc)
            print(text, file=sys.stderr, end="")
            return 1, text, "crash"

    @staticmethod
    def format_error(exc: BaseException) -> str:
        """Traceback without runner/runpy frames, plus a hint for missing modules."""
        tb = exc.__traceback__
        while tb is not None and (tb.tb_frame.f_code.co_filename == __file__
                                  or "runpy" in tb.tb_frame.f_code.co_filename):
            tb = tb.tb_next
        text = "".join(traceback.format_exception(type(exc), exc, tb))
        if isinstance(exc, ImportError):
            name = getattr(exc, "name", None) or ""
            top = name.split(".", 1)[0] if name else "?"
            text += (f"\nPiForge twin: module {top!r} is not available in the PiForge twin "
                     f"(not installed in the twin's Python environment and not simulated). "
                     f"Simulated hardware modules: {SIMULATED}. Install pure-Python dependencies into "
                     f"the PiForge venv, or guard hardware-only imports.\n")
        return text

    # -- main ---------------------------------------------------------------------------------------------
    def main(self) -> int:
        a = self.args
        self.open_channel()
        signal.signal(signal.SIGTERM, self._on_sigterm)
        fw = Path(a.firmware).expanduser().resolve()
        try:
            from piforge.twin.clock import SimClock
            from piforge.twin.config import TwinConfig
            from piforge.twin.runtime import Twin, set_twin

            config = TwinConfig.from_json(Path(a.config).read_text(encoding="utf-8"))
            if not fw.is_file():
                raise FileNotFoundError(f"firmware not found: {fw}")
            if a.cwd and not Path(a.cwd).is_dir():
                raise FileNotFoundError(f"--cwd directory not found: {a.cwd}")
            self.twin = Twin(config, clock=SimClock(a.speed))
        except Exception as exc:  # noqa: BLE001
            return self._finish_safely(2, f"twin setup failed: {exc}", "setup")
        set_twin(self.twin)
        self.twin.pi.event_listeners.append(self._on_twin_event)
        self.twin.start()
        self.install_shims()
        from gpiozero import Device

        from piforge.twin import vsysfs
        from piforge.twin.gpiozero_factory import TwinFactory

        Device.pin_factory = TwinFactory(self.twin)
        vsysfs.install(self.twin)
        if a.cwd:
            os.chdir(a.cwd)
        os.environ.update(self.twin.env())             # devices' env vars (e.g. ws_feed → MONEY_COUNTER_URL)
        self.send({"op": "hello", "devices": self.twin.describe(), "board": self.twin.config.board,
                   "protocol": 1, "pid": os.getpid(), "firmware": str(fw)})
        threading.Thread(target=self._input_loop, name="twin-input", daemon=True).start()
        self._send_state(None, 0.0, time.monotonic(), force=True)
        threading.Thread(target=self._publish_loop, name="twin-publish", daemon=True).start()
        if a.duration is not None:
            timer = threading.Timer(max(0.0, a.duration) / a.speed, self.request_stop, args=("duration",))
            timer.daemon = True
            timer.start()
        self.install_time_warp(a.speed)
        try:
            code, error, reason = self.run_firmware(fw)
        except KeyboardInterrupt:                       # stop arrived between firmware end and here
            code, error, reason = 0, None, self.stop_reason or "stopped"
        return self._finish_safely(code, error, reason)

    def _finish_safely(self, code: int, error: str | None, reason: str) -> int:
        while True:                                     # a late SIGINT must not skip the exit message
            try:
                return self.finish(code, error, reason)
            except KeyboardInterrupt:
                continue

    def finish(self, code: int, error: str | None, reason: str) -> int:
        """Flush output, publish the final state, send ``exit`` and terminate the process."""
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        self._done.set()
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:
                pass
        try:                                            # close our ends of the capture pipes → pumps hit EOF
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
            for t in self._pumps:
                t.join(timeout=0.3)
        except OSError:
            pass
        if self.twin is not None:
            try:
                self._drain_events()
                self._send_state(None, 0.0, time.monotonic(), force=True)
                self._send_displays({}, {}, time.monotonic(), force=True)
            except Exception:
                pass
        code = int(code) if isinstance(code, int) else 1
        self.send(exit_msg(code, error, reason))
        if self.writer is not None:
            self.writer.close()
        if self.sock is not None:
            try:
                self.sock.shutdown(2)
            except OSError:
                pass
        os._exit(code & 0xFF if code >= 0 else 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line interface of the runner."""
    p = argparse.ArgumentParser(prog="python -m piforge.twin.runner",
                                description="Run firmware against the PiForge digital twin.")
    p.add_argument("--config", required=True, help="twin configuration JSON (TwinConfig.to_json)")
    p.add_argument("--firmware", required=True, help="firmware entry script (run as __main__)")
    p.add_argument("--cwd", help="working directory for the firmware (default: current)")
    ch = p.add_mutually_exclusive_group(required=True)
    ch.add_argument("--stdio", action="store_true", help="protocol over stdin/stdout")
    ch.add_argument("--ipc", metavar="HOST:PORT", help="protocol over TCP to a listening parent")
    p.add_argument("--duration", type=float, default=None, help="stop the firmware after S seconds")
    p.add_argument("--speed", type=float, default=1.0, help="time scale (1.0 = real time)")
    args = p.parse_args(argv)
    if not args.speed > 0:
        p.error("--speed must be > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point (never returns normally: the process exits via ``os._exit``)."""
    return Runner(parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
