"""Where amounts come from: a WebSocket client (reconnects with back-off) or a small server.

``handle(message) -> dict`` is the controller's entry point (see :mod:`moneycounter.app`); it returns
``{"ok": True, ...}`` or ``{"ok": False, "error": ...}`` — the server sends that back as JSON.

* client mode: connect to ``url`` (``$MONEY_COUNTER_URL`` overrides config.toml), every text/binary
  message is an amount; on any error or close wait ``reconnect_min_s`` (doubling up to
  ``reconnect_max_s``) and connect again — the server's next message brings the display up to date.
* server mode: WebSocket on ``ws_port`` (any path) and HTTP on ``http_port``:
  ``POST /amount`` with ``{"amount": 1234.56}`` or ``1234.56`` as the body; ``GET /amount`` = status.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

Handler = Callable[[Any], dict]
StatusFn = Callable[[], dict]

MAX_BODY = 4096


def _ws_connect():
    try:
        from websockets.asyncio.client import connect          # websockets >= 13
    except ImportError:  # pragma: no cover - older websockets
        from websockets import connect  # type: ignore[no-redef]
    return connect


def _ws_serve():
    try:
        from websockets.asyncio.server import serve
    except ImportError:  # pragma: no cover
        from websockets import serve  # type: ignore[no-redef]
    return serve


class Backoff:
    def __init__(self, lo: float, hi: float) -> None:
        self.lo, self.hi, self.delay = lo, hi, lo

    def reset(self) -> None:
        self.delay = self.lo

    def next(self) -> float:
        d = self.delay
        self.delay = min(self.hi, self.delay * 2)
        return d


async def run_client(url: str, handle: Handler, *, reconnect_min_s: float = 0.5, reconnect_max_s: float = 10.0,
                     log: Callable[[str], None] = print, stop: asyncio.Event | None = None) -> None:
    connect = _ws_connect()
    backoff = Backoff(reconnect_min_s, reconnect_max_s)
    while stop is None or not stop.is_set():
        try:
            async with connect(url, open_timeout=5, ping_interval=20, ping_timeout=20,
                               max_size=MAX_BODY) as ws:
                log(f"WS connected to {url}")
                backoff.reset()
                async for message in ws:
                    handle(message)
            reason = "closed by the server"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - refused, reset, DNS, timeout, protocol, closed …
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        delay = backoff.next()
        log(f"WS {url} offline ({reason}); reconnecting in {delay:.1f} s")
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(delay)


# -- server mode ---------------------------------------------------------------------------------------
async def _ws_handler(ws, handle: Handler) -> None:
    async for message in ws:
        await ws.send(json.dumps(handle(message)))


def _http_response(status: int, body: dict) -> bytes:
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
              413: "Payload Too Large"}.get(status, "OK")
    data = json.dumps(body).encode()
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
            "Connection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n")
    return head.encode("ascii") + data


async def _http_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, handle: Handler,
                       status: StatusFn) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), 10)
        parts = line.decode("latin-1").split()
        if len(parts) < 2:
            return
        method, path = parts[0].upper(), parts[1].split("?", 1)[0]
        length = 0
        while True:
            h = await asyncio.wait_for(reader.readline(), 10)
            if h in (b"\r\n", b"\n", b""):
                break
            name, _, value = h.decode("latin-1").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip() or 0)
        if path not in ("/amount", "/"):
            resp = _http_response(404, {"ok": False, "error": "use POST /amount"})
        elif method == "GET":
            resp = _http_response(200, {"ok": True, **status()})
        elif method != "POST":
            resp = _http_response(405, {"ok": False, "error": "use POST /amount"})
        elif length > MAX_BODY:
            resp = _http_response(413, {"ok": False, "error": "body too large"})
        else:
            body = await asyncio.wait_for(reader.readexactly(length), 10) if length else b""
            result = handle(body)
            resp = _http_response(200 if result.get("ok") else 400, result)
        writer.write(resp)
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def run_server(handle: Handler, status: StatusFn, *, host: str = "0.0.0.0", ws_port: int = 8765,
                     http_port: int = 8080, log: Callable[[str], None] = print,
                     ready: Callable[[int, int], None] | None = None) -> None:
    """Serve forever (until cancelled). ``ready(ws_port, http_port)`` gets the bound ports (0 = any)."""
    serve = _ws_serve()
    async with serve(lambda ws, *_: _ws_handler(ws, handle), host, ws_port, max_size=MAX_BODY) as wss:
        http = await asyncio.start_server(lambda r, w: _http_client(r, w, handle, status), host, http_port)
        wp = next(iter(wss.sockets)).getsockname()[1]
        hp = http.sockets[0].getsockname()[1]
        log(f"SERVER listening: ws://{host}:{wp}/ and http://{host}:{hp}/amount")
        if ready:
            ready(wp, hp)
        async with http:
            await asyncio.Future()
