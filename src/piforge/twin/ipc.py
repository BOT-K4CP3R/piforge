"""Runner ⇄ server protocol (spec §5.6): one JSON object per line, UTF-8.

server → runner::

    {"op":"input","device":"SW1","prop":"pressed","value":true}
    {"op":"stop"}

runner → server::

    {"op":"hello","devices":[{id,type,inputs:{prop:{type,min,max,unit,default,…}},outputs:{…}},…],
     "board":"rpi4b","protocol":1,"pid":…}
    {"op":"state","t":1.23,"pins":{"17":1,…},"devices":{"D1":{"brightness":1.0},…}}     (≤ 30 Hz)
    {"op":"display","device":"OLED1","w":128,"h":64,"png_b64":"…"}                     (on change)
    {"op":"log","stream":"stdout"|"stderr"|"twin","text":"…"}  (twin diagnostics add "level","code")
    {"op":"exit","code":0,"error":null,"reason":"finished"|"stopped"|"duration"|"crash"|"sys.exit"|…}

Transports: the runner's stdin/stdout (``--stdio``) or a TCP connection to ``127.0.0.1:<port>``
(``--ipc``, the parent listens). Extra keys are allowed; consumers must ignore unknown keys.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from typing import IO, Any

from piforge.core.errors import PiForgeError
from piforge.core.report import jsonable

PROTOCOL_VERSION = 1
RUNNER_OPS = frozenset({"hello", "state", "display", "log", "exit"})
SERVER_OPS = frozenset({"input", "stop"})
MAX_LINE = 16 * 1024 * 1024        # a 1080p display PNG in base64 fits comfortably


class ProtocolError(PiForgeError):
    """A line that is not a valid protocol message."""


def encode(msg: dict) -> bytes:
    """Serialise one message as a JSON line (non-finite floats become null)."""
    return (json.dumps(jsonable(msg), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict:
    """Parse one JSON line into a message dict with a string ``op``."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc}: {line[:200]!r}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("op"), str):
        raise ProtocolError(f"message must be an object with a string 'op': {line[:200]!r}")
    return msg


class MessageWriter:
    """Thread-safe JSON-lines writer; never raises once the peer is gone (returns False)."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.closed = False

    def send(self, msg: dict, *, blocking: bool = True) -> bool:
        """Write ``msg``; False if the channel is closed (or busy when ``blocking=False``).

        ``blocking=False`` is meant for signal handlers / watchdog threads: it never waits for the
        lock (a KeyboardInterrupt cannot hit those contexts between acquire and release).
        """
        if self.closed:
            return False
        data = encode(msg)
        if blocking:
            with self._lock:                    # `with`: an async KeyboardInterrupt cannot leak the lock
                return self._write(data)
        if not self._lock.acquire(blocking=False):
            return False
        try:
            return self._write(data)
        finally:
            self._lock.release()

    def _write(self, data: bytes) -> bool:
        try:
            self._stream.write(data)
            self._stream.flush()
            return True
        except (OSError, ValueError):
            self.closed = True
            return False

    def close(self) -> None:
        """Close the underlying stream."""
        with self._lock:
            self.closed = True
            try:
                self._stream.close()
            except OSError:
                pass


def _protocol_warning(code: str, text: str) -> dict:
    return {"op": "log", "stream": "protocol", "level": "warning", "code": code, "text": text}


def iter_messages(stream: IO[bytes], max_line: int = MAX_LINE) -> Iterator[dict]:
    """Yield messages from a binary line stream until EOF.

    Malformed lines become ``{"op":"log","stream":"protocol","code":"TWIN.BAD_MESSAGE",…}`` and a
    line longer than ``max_line`` is discarded up to its newline with one
    ``TWIN.LINE_TOO_LONG`` warning, so nothing is silently lost and a giant line can never be
    split into garbage "messages".
    """
    while True:
        dropped = 0
        try:
            line = stream.readline(max_line)
            if line and len(line) >= max_line and not line.endswith(b"\n"):
                dropped = len(line)
                while True:                     # skip the rest of the over-long line
                    more = stream.readline(max_line)
                    dropped += len(more)
                    if not more or more.endswith(b"\n"):
                        break
        except (OSError, ValueError):
            return
        if dropped:
            yield _protocol_warning("TWIN.LINE_TOO_LONG",
                                    f"discarded an over-long protocol line ({dropped} bytes > {max_line})")
            continue
        if not line:
            return
        if not line.strip():
            continue
        try:
            msg = decode(line)
        except ProtocolError as exc:
            msg = _protocol_warning("TWIN.BAD_MESSAGE", str(exc))
        yield msg


def is_protocol_warning(msg: dict) -> bool:
    """True for the warnings :func:`iter_messages` synthesises (not sent by the peer)."""
    return (msg.get("op") == "log" and msg.get("stream") == "protocol"
            and msg.get("code") in ("TWIN.BAD_MESSAGE", "TWIN.LINE_TOO_LONG"))


# --- message constructors -----------------------------------------------------------------------
def input_msg(device: str, prop: str, value: Any) -> dict:
    """``{"op":"input",…}``."""
    return {"op": "input", "device": device, "prop": prop, "value": value}


def stop_msg() -> dict:
    """``{"op":"stop"}``."""
    return {"op": "stop"}


def log_msg(stream: str, text: str, **extra: Any) -> dict:
    """``{"op":"log","stream":…,"text":…}``."""
    return {"op": "log", "stream": stream, "text": text, **extra}


def exit_msg(code: int, error: str | None, reason: str) -> dict:
    """``{"op":"exit","code":…,"error":…,"reason":…}``."""
    return {"op": "exit", "code": int(code), "error": error, "reason": reason}


# --- TCP helpers ------------------------------------------------------------------------------------
def listen_localhost() -> socket.socket:
    """A listening TCP socket on 127.0.0.1 with an OS-assigned port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv


def connect(host_port: str, timeout: float = 10.0) -> socket.socket:
    """Connect to ``HOST:PORT`` (the runner side of ``--ipc``)."""
    host, _, port = host_port.rpartition(":")
    if not host or not port.isdigit():
        raise ProtocolError(f"--ipc expects HOST:PORT, got {host_port!r}")
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock
