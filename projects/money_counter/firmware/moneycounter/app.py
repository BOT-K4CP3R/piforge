"""The controller: amounts in → count-up animation → module targets; logs ``SHOW 001234,56``."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .amount import InvalidAmount, format_cents, format_digits, max_cents_for, parse_message
from .animator import CountUp
from .config import Config, client_url
from .motion import Motion

FRAME_S = 0.02          # animation update period (50 Hz)


def log(msg: str) -> None:
    print(msg, flush=True)


class Controller:
    def __init__(self, cfg: Config, motion: Motion, *, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = log) -> None:
        self.cfg, self.motion, self.clock, self.log = cfg, motion, clock, log
        d = cfg.display
        self.max_cents = max_cents_for(d.max_amount)
        self.anim = CountUp(count_time_min=d.count_time_min, count_time_max=d.count_time_max,
                            capacity=motion.capacity, flap_time=motion.flap_time)
        self._applied: list[tuple[int, bool] | None] = [None] * len(motion.modules)
        self._announce = True           # log SHOW at the next settle (after homing / a new target)
        self.shown_text: str | None = None
        self.received = 0
        self.ignored = 0

    # -- input ---------------------------------------------------------------------------------------
    def handle(self, message: Any) -> dict:
        """One amount message (WebSocket or HTTP body). Invalid ones are logged and ignored."""
        self.received += 1
        try:
            amt = parse_message(message, self.cfg.network.json_path, self.max_cents)
        except InvalidAmount as exc:
            self.ignored += 1
            text = message.decode("utf-8", "replace") if isinstance(message, (bytes, bytearray)) else str(message)
            self.log(f"IGNORED invalid message {text[:80]!r}: {exc}")
            return {"ok": False, "error": str(exc)}
        if amt.clamped:
            self.log(f"CLAMP {amt.requested} > max {format_cents(self.max_cents)} -> showing {format_cents(amt.cents)}")
        mode, duration = self.anim.set_target(amt.cents, self.clock())
        if mode != "same":
            self._announce = True
            how = f"count-up {duration:.1f} s" if mode == "count" else "forward to the new digits"
            self.log(f"TARGET {format_cents(amt.cents)} ({how})")
        return {"ok": True, "target": format_cents(amt.cents), "clamped": amt.clamped}

    def status(self) -> dict:
        return {"target": format_cents(self.anim.target), "shown": self.shown_text,
                "homed": self.motion.homed(), "settled": self.motion.settled(), "received": self.received,
                "ignored": self.ignored, **self.motion.stats()}

    # -- display loop ---------------------------------------------------------------------------------
    def update(self) -> None:
        """Push the animation frame to the modules; log SHOW once everything has settled."""
        if not self.motion.homed():
            return
        frame = self.anim.frame(self.clock())
        for i, (digit, spin) in enumerate(zip(frame.digits, frame.spin, strict=True)):
            want = (digit, spin)
            if spin or self._applied[i] != want:
                self.motion.set_desired(i, digit, spin, frame.final[i], frame.time_left)
                self._applied[i] = want
        if frame.done and self.motion.settled():
            text = format_digits(self.motion.shown_digits())
            if self._announce or text != self.shown_text:
                self.shown_text = text
                self._announce = False
                self.log(f"SHOW {text}")

    async def display_loop(self) -> None:
        while True:
            self.update()
            await asyncio.sleep(FRAME_S)

    async def main(self) -> None:
        net = self.cfg.network
        tasks = [asyncio.create_task(self.display_loop())]
        if net.mode == "client":
            from .net import run_client

            url = client_url(self.cfg)
            self.log(f"client mode: {url}")
            tasks.append(asyncio.create_task(run_client(url, self.handle, reconnect_min_s=net.reconnect_min_s,
                                                        reconnect_max_s=net.reconnect_max_s, log=self.log)))
        else:
            from .net import run_server

            tasks.append(asyncio.create_task(run_server(self.handle, self.status, host=net.listen_host,
                                                        ws_port=net.ws_port, http_port=net.http_port, log=self.log)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
