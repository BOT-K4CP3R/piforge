from __future__ import annotations

import asyncio
import json
import socket

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from moneycounter.net import Backoff, run_client, run_server  # noqa: E402


def handler_factory(store):
    def handle(message):
        store.append(message)
        text = message.decode() if isinstance(message, bytes) else message
        try:
            float(json.loads(text)["amount"] if text.startswith("{") else text)
        except Exception:
            return {"ok": False, "error": "bad"}
        return {"ok": True}
    return handle


def test_backoff_doubles_and_caps():
    b = Backoff(0.5, 3.0)
    assert [b.next() for _ in range(5)] == [0.5, 1.0, 2.0, 3.0, 3.0]
    b.reset()
    assert b.next() == 0.5


async def _http(port, method, body=b""):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} /amount HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    await w.drain()
    data = await r.read()
    w.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_server_mode_ws_and_http():
    got = []

    async def main():
        ports = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(run_server(handler_factory(got), lambda: {"shown": "000000,00"}, host="127.0.0.1",
                                              ws_port=0, http_port=0, log=lambda m: None,
                                              ready=lambda w, h: ports.set_result((w, h))))
        wp, hp = await asyncio.wait_for(ports, 5)
        async with connect(f"ws://127.0.0.1:{wp}/") as ws:
            await ws.send('{"amount": 12.5}')
            assert json.loads(await ws.recv())["ok"]
            await ws.send("nonsense")
            assert not json.loads(await ws.recv())["ok"]
        assert await _http(hp, "POST", b'{"amount": 99}') == (200, {"ok": True})
        assert (await _http(hp, "POST", b"x"))[0] == 400
        status, body = await _http(hp, "GET")
        assert status == 200 and body["shown"] == "000000,00"
        task.cancel()

    asyncio.run(main())
    assert got[0] == '{"amount": 12.5}' and got[2] == b'{"amount": 99}'


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_client_reconnects_and_catches_up():
    got, logs = [], []
    port = _free_port()
    url = f"ws://127.0.0.1:{port}/"

    async def feed(ws):
        await ws.send(json.dumps({"amount": feed.amount}))
        await ws.wait_closed()

    async def main():
        feed.amount = 1.0
        task = asyncio.create_task(run_client(url, got.append, reconnect_min_s=0.1, reconnect_max_s=0.4,
                                              log=logs.append))
        await asyncio.sleep(0.5)                       # server not up yet: refused, retrying
        assert any("offline" in m for m in logs) and not got
        async with serve(feed, "127.0.0.1", port) as srv:
            for _ in range(50):
                if got:
                    break
                await asyncio.sleep(0.05)
            srv.close()                                # server goes away …
        feed.amount = 2.0
        await asyncio.sleep(0.6)
        async with serve(feed, "127.0.0.1", port):     # … and comes back with a new amount
            for _ in range(60):
                if len(got) >= 2:
                    break
                await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(main())
    assert [json.loads(m)["amount"] for m in got[:2]] == [1.0, 2.0]
    assert sum("connected" in m for m in logs) >= 2
