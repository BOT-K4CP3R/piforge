"""Network-side test doubles: ``ws_feed``, a local WebSocket server that pushes an amount to the firmware.

The server runs inside the twin process (the runner) on its own asyncio thread, started by
:meth:`Device.on_start` before the firmware is launched. Its URL reaches the firmware through an
environment variable (``Device.env()``, default ``MONEY_COUNTER_URL=ws://127.0.0.1:<port>/``), so
firmware written as a WebSocket *client* runs unmodified against the twin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from piforge.core.errors import ValidationError
from piforge.twin.devices.base import Device, PropSpec, register

log = logging.getLogger(__name__)
START_TIMEOUT_S = 5.0


def _serve_fn() -> Any:
    try:                                        # websockets ≥ 13: new asyncio implementation
        from websockets.asyncio.server import serve
    except ImportError:  # pragma: no cover - older websockets (legacy API, same call shape)
        from websockets import serve  # type: ignore[no-redef]
    return serve


@register
class WsFeed(Device):
    """Local WebSocket server pushing ``{"amount": <float>}`` to every client on connect and on change.

    Inputs: ``amount`` (pushed whenever it changes), ``online`` (False closes every connection and the
    listening socket, so new connections are refused, until it is True again). Outputs: ``clients``,
    ``port``, ``url``, ``sent`` (messages pushed). Params: ``port`` (0 = pick a free port), ``host``
    (127.0.0.1), ``env_var`` (``MONEY_COUNTER_URL``), ``path`` (``/``; any path is accepted).
    Text received from clients is ignored (counted in ``received``).
    """

    type = "ws_feed"
    label = "WebSocket amount feed"
    defaults = {"port": 0, "host": "127.0.0.1", "env_var": "MONEY_COUNTER_URL", "path": "/"}
    inputs = {"amount": PropSpec("float", -1e12, 1e12, default=0.0, label="Amount", widget="slider"),
              "online": PropSpec("bool", default=True, label="Server online", widget="toggle")}
    outputs = {"clients": PropSpec("int", 0, None, default=0, label="Connected clients"),
               "port": PropSpec("int", 0, 65535, default=0, label="TCP port"),
               "url": PropSpec("text", default="", label="URL"),
               "sent": PropSpec("int", 0, None, default=0, label="Messages pushed"),
               "received": PropSpec("int", 0, None, default=0, label="Messages received")}
    example = {"params": {"port": 0}}

    def setup(self) -> None:
        port = self.params.get("port", 0)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValidationError(f"{self.id} (ws_feed): port must be 0…65535 (0 = auto), got {port!r}")
        self._port = port
        self._host = str(self.params.get("host") or "127.0.0.1")
        path = str(self.params.get("path") or "/")
        self._path = path if path.startswith("/") else "/" + path
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._clients: set[Any] = set()
        self._sent = 0
        self._received = 0
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------------------------------
    def on_start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"twin-ws-{self.id}", daemon=True)
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            self.pi.record_event("TWIN.DEVICE_ERROR", "error", f"{self.id}: WebSocket server did not start",
                                 device=self.id)
        elif self._error is not None:
            self.pi.record_event("TWIN.DEVICE_ERROR", "error", f"{self.id}: cannot listen on "
                                 f"{self._host}:{self._port}: {self._error}", device=self.id)

    def on_stop(self) -> None:
        loop, th = self._loop, self._thread
        if loop is None or th is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=3.0)
        except Exception:  # pragma: no cover - best effort
            pass
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=3.0)
        self._thread = self._loop = None

    def close(self) -> None:
        self.on_stop()
        super().close()

    def env(self) -> dict[str, str]:
        var = str(self.params.get("env_var") or "")
        return {var: self.url()} if var and self._port else {}

    def url(self) -> str:
        """``ws://host:port/path`` once the port is known ('' before the server started on port 0)."""
        return f"ws://{self._host}:{self._port}{self._path}" if self._port else ""

    # -- asyncio side ------------------------------------------------------------------------------
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._listen())
        except BaseException as exc:  # noqa: BLE001 - reported by on_start
            self._error = exc
        self._ready.set()
        if self._error is None:
            loop.run_forever()
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    async def _listen(self) -> None:
        if self._server is not None:
            return
        self._server = await _serve_fn()(self._handler, self._host, self._port)
        if not self._port:
            self._port = int(next(iter(self._server.sockets)).getsockname()[1])

    async def _unlisten(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()                       # also closes open connections (code 1001)
            try:
                await asyncio.wait_for(server.wait_closed(), 2.0)
            except (asyncio.TimeoutError, Exception):  # pragma: no cover - best effort
                pass
        for ws in list(self._clients):
            try:
                await ws.close(1001, "feed offline")
            except Exception:  # pragma: no cover
                pass
        self._clients.clear()

    async def _shutdown(self) -> None:
        await self._unlisten()

    def _message(self) -> str:
        return json.dumps({"amount": float(self._inputs["amount"])})

    async def _send(self, ws: Any, text: str) -> None:
        try:
            await ws.send(text)
            self._sent += 1
        except Exception:                       # client went away; its handler cleans up
            pass

    async def _handler(self, ws: Any, *_: Any) -> None:
        if not self._inputs["online"]:
            await ws.close(1013, "feed offline")
            return
        self._clients.add(ws)
        try:
            await self._send(ws, self._message())
            async for _msg in ws:
                self._received += 1
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    async def _broadcast(self) -> None:
        text = self._message()
        await asyncio.gather(*(self._send(ws, text) for ws in list(self._clients)))

    async def _set_online(self, online: bool) -> None:
        if online:
            try:
                await self._listen()
            except OSError as exc:
                log.warning("%s: cannot listen again on port %s: %s", self.id, self._port, exc)
        else:
            await self._unlisten()

    # -- inputs / outputs ----------------------------------------------------------------------------
    def on_input(self, prop: str, value: Any) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        if prop == "amount":
            asyncio.run_coroutine_threadsafe(self._broadcast(), loop)
        elif prop == "online":
            asyncio.run_coroutine_threadsafe(self._set_online(bool(value)), loop)

    def outputs_state(self) -> dict:
        return {"clients": len(self._clients), "port": int(self._port), "url": self.url(),
                "sent": self._sent, "received": self._received}
