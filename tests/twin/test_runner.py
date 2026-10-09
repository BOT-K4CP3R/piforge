"""Runner subprocess + JSON-lines protocol (spec §5.6) + misbehaving firmware (Review Focus #3)."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from piforge.core.errors import PiForgeError
from piforge.twin.config import DeviceConfig, TwinConfig
from piforge.twin.session import TwinSession

BUTTON_LED_FW = """\
from signal import pause
from gpiozero import LED, Button

led = LED(17)
button = Button(27)
button.when_pressed = led.on
button.when_released = led.off
print("ready", flush=True)
pause()
"""

CONFIG = TwinConfig(devices=[DeviceConfig("SW1", "button", {"pin": 27}),
                             DeviceConfig("D1", "led", {"pin": 17})])
START_TIMEOUT = 30.0      # generous: first import of gpiozero under heavy swap


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


class _Reader:
    """Collect JSON lines from a pipe in a thread; fail loudly on non-JSON output."""

    def __init__(self, stream) -> None:
        self.q: queue.Queue = queue.Queue()
        self.raw: list[str] = []
        threading.Thread(target=self._run, args=(stream,), daemon=True).start()

    def _run(self, stream) -> None:
        for line in stream:
            self.raw.append(line)
            self.q.put(json.loads(line))
        self.q.put(None)

    def wait(self, pred, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"timeout waiting; raw tail: {self.raw[-5:]}")
            try:
                msg = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if msg is None:
                raise AssertionError(f"stream closed; raw tail: {self.raw[-5:]}")
            try:
                if pred(msg):
                    return msg
            except (KeyError, TypeError):
                pass


def test_runner_stdio_roundtrip(spaced_tmp):
    fw = _write(spaced_tmp, "main fw.py", BUTTON_LED_FW)
    cfg = _write(spaced_tmp, "twin config.json", CONFIG.to_json())
    proc = subprocess.Popen(
        [sys.executable, "-m", "piforge.twin.runner", "--config", str(cfg), "--firmware", str(fw),
         "--cwd", str(spaced_tmp), "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", cwd=str(spaced_tmp))
    try:
        rd = _Reader(proc.stdout)
        hello = rd.wait(lambda m: m["op"] == "hello", START_TIMEOUT)
        assert {d["id"] for d in hello["devices"]} == {"SW1", "D1"}
        sw = next(d for d in hello["devices"] if d["id"] == "SW1")
        assert sw["inputs"]["pressed"]["type"] == "bool"
        rd.wait(lambda m: m["op"] == "log" and "ready" in m["text"], START_TIMEOUT)
        st = rd.wait(lambda m: m["op"] == "state", 5.0)
        assert st["devices"]["D1"]["brightness"] == 0.0 and "17" in st["pins"]
        proc.stdin.write(json.dumps({"op": "input", "device": "SW1", "prop": "pressed", "value": True}) + "\n")
        proc.stdin.flush()
        rd.wait(lambda m: m["op"] == "state" and m["devices"]["D1"]["brightness"] == 1.0, 5.0)
        proc.stdin.write(json.dumps({"op": "stop"}) + "\n")
        proc.stdin.flush()
        ex = rd.wait(lambda m: m["op"] == "exit", 5.0)
        assert ex["code"] == 0 and ex["error"] is None
        assert proc.wait(timeout=5) == 0
        assert all(json.loads(line)["op"] for line in rd.raw)     # nothing but protocol on stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_firmware_crash_reported(spaced_tmp):
    fw = _write(spaced_tmp, "crash.py", "print('about to fail')\nx = 1 / 0\n")
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp)
    s.start()
    try:
        ex = s.wait_for(lambda m: m["op"] == "exit", START_TIMEOUT)
        assert ex is not None
        assert ex["code"] == 1
        assert "ZeroDivisionError" in ex["error"] and "crash.py" in ex["error"]
        assert any("about to fail" in line for line in s.logs())
    finally:
        s.stop()
    assert not s.running


def test_infinite_loop_stopped_within_2s(spaced_tmp):
    fw = _write(spaced_tmp, "busy.py", "print('looping', flush=True)\nwhile True:\n    pass\n")
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp)
    s.start()
    assert s.wait_for(lambda m: m["op"] == "log" and "looping" in m["text"], START_TIMEOUT)
    t0 = time.monotonic()
    s.stop(timeout=2.0)
    assert time.monotonic() - t0 < 2.5
    assert not s.running
    ex = s.exit_message
    assert ex is not None and ex["code"] == 0           # interrupted cleanly by the stop request


def test_hostile_loop_is_killed_within_2s(spaced_tmp):
    """Firmware that swallows KeyboardInterrupt and ignores SIGTERM still dies within 2 s."""
    fw = _write(spaced_tmp, "hostile.py", (
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('looping', flush=True)\n"
        "while True:\n"
        "    try:\n"
        "        while True:\n"
        "            pass\n"
        "    except KeyboardInterrupt:\n"
        "        pass\n"))
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp)
    s.start()
    assert s.wait_for(lambda m: m["op"] == "log" and "looping" in m["text"], START_TIMEOUT)
    t0 = time.monotonic()
    s.stop(timeout=2.0)
    assert time.monotonic() - t0 < 2.5
    assert not s.running


def test_unsupported_import_reported(spaced_tmp):
    fw = _write(spaced_tmp, "vision.py", "import cv2\n")
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp)
    s.start()
    try:
        ex = s.wait_for(lambda m: m["op"] == "exit", START_TIMEOUT)
        assert ex is not None and ex["code"] == 1
        assert "cv2" in ex["error"]
        assert "not available in the PiForge twin" in ex["error"]
    finally:
        s.stop()


def test_tcp_transport_and_session_api(spaced_tmp, wait_until):
    fw = _write(spaced_tmp, "main.py", BUTTON_LED_FW)
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp, transport="tcp")
    s.start()
    try:
        hello = s.wait_for(lambda m: m["op"] == "hello", START_TIMEOUT)
        assert hello is not None and s.hello == hello
        assert s.wait_for(lambda m: m["op"] == "log" and "ready" in m["text"], START_TIMEOUT)
        s.send_input("SW1", "pressed", True)
        st = s.wait_for(lambda m: m["devices"]["D1"]["brightness"] == 1.0, 5.0)
        assert st is not None and st["op"] == "state"
        assert s.latest_state()["devices"]["D1"]["brightness"] == 1.0
        msgs = s.poll()
        assert any(m["op"] == "state" for m in msgs)
        assert all(m["op"] in ("hello", "state", "log", "display") for m in msgs)
    finally:
        s.stop()
    assert s.exit_message is not None and s.exit_message["code"] == 0


def test_runner_duration_and_bad_input(spaced_tmp):
    fw = _write(spaced_tmp, "main.py", BUTTON_LED_FW)
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp, duration=1.5)
    s.start()
    try:
        assert s.wait_for(lambda m: m["op"] == "hello", START_TIMEOUT)
        s.send_input("NOPE", "pressed", True)                     # reported, not fatal
        assert s.wait_for(lambda m: m["op"] == "log" and m.get("stream") == "twin"
                          and "NOPE" in m["text"], 5.0)
        ex = s.wait_for(lambda m: m["op"] == "exit", 10.0)
        assert ex is not None and ex["code"] == 0 and ex.get("reason") == "duration"
    finally:
        s.stop()


def test_send_input_requires_running_session(spaced_tmp):
    s = TwinSession(CONFIG, _write(spaced_tmp, "main.py", BUTTON_LED_FW), cwd=spaced_tmp)
    with pytest.raises(PiForgeError):
        s.send_input("SW1", "pressed", True)
    s.stop()                                                       # stopping a never-started session is a no-op


# --- real firmware styles end-to-end -------------------------------------------------------------
def _run_to_exit(spaced_tmp: Path, cfg: TwinConfig, fw_text: str, *, inputs=(), timeout: float = 20.0,
                 **kw) -> TwinSession:
    fw = _write(spaced_tmp, "fw.py", fw_text)
    s = TwinSession(cfg, fw, cwd=spaced_tmp, **kw)
    s.start()
    assert s.wait_for(lambda m: m["op"] == "hello", START_TIMEOUT)
    for dev, prop, val in inputs:
        s.send_input(dev, prop, val)
    s.wait_for(lambda m: m["op"] == "exit", timeout)
    s.stop()
    return s


def test_runner_blinka_firmware_emits_display_and_logs(spaced_tmp):
    pytest.importorskip("adafruit_ssd1306")
    pytest.importorskip("adafruit_bme280")
    cfg = TwinConfig(devices=[DeviceConfig("OLED1", "ssd1306", bus={"kind": "i2c", "bus": 1, "address": 0x3C}),
                              DeviceConfig("ENV1", "bme280", bus={"kind": "i2c", "bus": 1, "address": 0x76},
                                           params={})])
    fw = (
        "import time, board, adafruit_ssd1306\n"
        "from adafruit_bme280 import basic as bme\n"
        "i2c = board.I2C()\n"
        "oled = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)\n"
        "env = bme.Adafruit_BME280_I2C(i2c, address=0x76)\n"
        "oled.fill(0); oled.pixel(64, 32, 1); oled.show()\n"
        "print(f'T={env.temperature:.1f}', flush=True)\n"
        "time.sleep(0.3)\n")
    s = _run_to_exit(spaced_tmp, cfg, fw)
    assert s.exit_message["code"] == 0, s.exit_message
    assert any(line.startswith("T=22.0") for line in s.logs()), s.logs()   # BME280 default input 22 °C
    disp = s.displays["OLED1"]
    assert (disp["w"], disp["h"]) == (128, 64)
    import base64
    import io as _io

    from PIL import Image

    img = Image.open(_io.BytesIO(base64.b64decode(disp["png_b64"]))).convert("L")
    assert img.getpixel((64, 32)) > 127 and img.getpixel((0, 0)) < 128


def test_runner_rpigpio_polling_hcsr04(spaced_tmp):
    """Classic RPi.GPIO tutorial code timing the echo with time.time() in busy loops."""
    cfg = TwinConfig(devices=[DeviceConfig("US1", "hcsr04", {"trigger": 23, "echo": 24})])
    fw = (
        "import time\n"
        "import RPi.GPIO as GPIO\n"
        "GPIO.setmode(GPIO.BCM)\n"
        "GPIO.setup(23, GPIO.OUT)\n"
        "GPIO.setup(24, GPIO.IN)\n"
        "time.sleep(0.5)\n"
        "res = []\n"
        "for _ in range(5):\n"
        "    GPIO.output(23, True); time.sleep(0.00001); GPIO.output(23, False)\n"
        "    start = stop = time.time()\n"
        "    while GPIO.input(24) == 0: start = time.time()\n"
        "    while GPIO.input(24) == 1: stop = time.time()\n"
        "    res.append((stop - start) * 34300 / 2)\n"
        "    time.sleep(0.06)\n"
        "res.sort()\n"
        "print(f'DIST={res[2]:.1f}', flush=True)\n"
        "GPIO.cleanup()\n")
    s = _run_to_exit(spaced_tmp, cfg, fw, inputs=[("US1", "distance", 0.35)])
    assert s.exit_message["code"] == 0, s.exit_message
    line = next(x for x in s.logs() if x.startswith("DIST="))
    assert float(line[5:]) == pytest.approx(35.0, abs=3.0)        # cm; median of 5 pings


def test_runner_ds18b20_sysfs_firmware(spaced_tmp):
    """The classic glob('/sys/bus/w1/devices/28*') + open(.../w1_slave) DS18B20 recipe."""
    cfg = TwinConfig(devices=[DeviceConfig("T1", "ds18b20", {"pin": 4})])
    fw = (
        "import glob, time\n"
        "time.sleep(0.3)\n"
        "folder = glob.glob('/sys/bus/w1/devices/28*')[0]\n"
        "with open(folder + '/w1_slave') as f:\n"
        "    lines = f.readlines()\n"
        "assert lines[0].strip().endswith('YES')\n"
        "print('TEMP=' + str(float(lines[1].split('t=')[1]) / 1000.0), flush=True)\n")
    s = _run_to_exit(spaced_tmp, cfg, fw, inputs=[("T1", "temperature", 23.5)])
    assert s.exit_message["code"] == 0, s.exit_message
    assert "TEMP=23.5" in s.logs(), s.logs()


def test_runner_speed_scales_firmware_time(spaced_tmp):
    cfg = TwinConfig(devices=[])
    fw = "import time\nt = time.monotonic()\ntime.sleep(2.0)\nprint(f'SIM={time.monotonic() - t:.1f}', flush=True)\n"
    t0 = time.monotonic()
    s = _run_to_exit(spaced_tmp, cfg, fw, speed=4.0)
    assert s.exit_message["code"] == 0
    assert "SIM=2.0" in s.logs(), s.logs()
    assert time.monotonic() - t0 < START_TIMEOUT                    # sanity; the sleep itself took ~0.5 s


def test_shims_not_importable_in_normal_process():
    code = ("import importlib.util as u, sys\n"
            "names = ['RPi', 'smbus2', 'smbus', 'spidev', 'picamera2', 'w1thermsensor', 'adafruit_dht']\n"
            "found = [n for n in names if u.find_spec(n) is not None]\n"
            "spec = u.find_spec('board')\n"
            "assert not found, found\n"
            "assert spec is None or 'shims' not in (spec.origin or ''), spec.origin\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr


def test_runner_exits_when_parent_closes_channel(spaced_tmp):
    """If the server dies (stdin EOF), the runner must not linger as an orphan."""
    fw = _write(spaced_tmp, "main.py", BUTTON_LED_FW)
    cfg = _write(spaced_tmp, "twin.json", CONFIG.to_json())
    proc = subprocess.Popen(
        [sys.executable, "-m", "piforge.twin.runner", "--config", str(cfg), "--firmware", str(fw), "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        cwd=str(spaced_tmp))
    try:
        rd = _Reader(proc.stdout)
        rd.wait(lambda m: m["op"] == "log" and "ready" in m["text"], START_TIMEOUT)
        proc.stdin.close()                                   # parent "dies"
        ex = rd.wait(lambda m: m["op"] == "exit", 5.0)
        assert ex["code"] == 0
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- fix round 1 ---------------------------------------------------------------------------------
STUBBORN_FW = """\
import time
print('looping', flush=True)
while True:
    try:
        time.sleep(0.1)
    except:
        pass
"""


def _alive(pid: int) -> bool:
    res = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    stat = res.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def test_runner_dies_when_parent_is_killed(spaced_tmp):
    """Parent SIGKILLed → runner (whose firmware swallows KeyboardInterrupt) is gone within 3 s."""
    import os
    import signal

    fw = _write(spaced_tmp, "stubborn.py", STUBBORN_FW)
    parent = (
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "from piforge.twin.config import TwinConfig\n"
        "from piforge.twin.session import TwinSession\n"
        "s = TwinSession(TwinConfig(), Path(sys.argv[1]))\n"
        "s.start()\n"
        "assert s.wait_for(lambda m: m['op'] == 'log' and 'looping' in m['text'], 60)\n"
        "print(s._proc.pid, flush=True)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n")
    res = subprocess.run([sys.executable, "-c", parent, str(fw)], capture_output=True, text=True, timeout=120)
    assert res.returncode == -signal.SIGKILL, res.stderr
    pid = int(res.stdout.strip())
    try:
        deadline = time.monotonic() + 3.0
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "runner outlived its parent"
    finally:
        if _alive(pid):
            os.killpg(pid, signal.SIGKILL)


def test_fd_level_output_is_bounded_and_protocol_stays_clean(spaced_tmp):
    """os.write without newlines (progress bars, 1 MB blobs) never produces unbounded log lines."""
    from piforge.twin.runner import MAX_LOG_LINE

    fw = _write(spaced_tmp, "noisy.py", (
        "import os, time\n"
        "for i in range(50):\n"
        "    os.write(1, f'\\r{i:3d}% [{\"#\" * (i // 5):<10}]'.encode())\n"
        "os.write(1, b'\\n')\n"
        "os.write(2, b'z' * (1024 * 1024))\n"
        "os.write(1, b'tail-no-newline')\n"
        "time.sleep(0.6)\n"
        "print('done', flush=True)\n"))
    cfg = _write(spaced_tmp, "twin.json", TwinConfig().to_json())
    proc = subprocess.Popen(
        [sys.executable, "-m", "piforge.twin.runner", "--config", str(cfg), "--firmware", str(fw), "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        cwd=str(spaced_tmp))
    try:
        rd = _Reader(proc.stdout)                           # fails on any non-JSON line
        ex = rd.wait(lambda m: m["op"] == "exit", START_TIMEOUT)
        assert ex["code"] == 0, ex
        msgs = [json.loads(line) for line in rd.raw]
        logs = [m for m in msgs if m["op"] == "log"]
        assert all(len(m["text"]) <= MAX_LOG_LINE for m in logs)
        zs = [m["text"] for m in logs if m["stream"] == "stderr"]
        assert sum(len(t) for t in zs) == 1024 * 1024 and len(zs) >= 16
        out = [m["text"] for m in logs if m["stream"] == "stdout"]
        assert any("49%" in t for t in out)
        assert "tail-no-newline" in out                     # partial line flushed when output goes idle
        assert out[-1] == "done"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_malformed_parent_messages_reported_as_bad_message(spaced_tmp):
    fw = _write(spaced_tmp, "main.py", BUTTON_LED_FW)
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp)
    s.start()
    try:
        assert s.wait_for(lambda m: m["op"] == "log" and "ready" in m["text"], START_TIMEOUT)
        with s._send_lock:
            s._out.write(b"not json\n")
            s._out.flush()
        msg = s.wait_for(lambda m: m["op"] == "log" and m.get("code") == "TWIN.BAD_MESSAGE", 5.0)
        assert msg is not None and msg["stream"] == "twin"
        assert not any(m.get("code") == "TWIN.BAD_OP" for m in s.events())
    finally:
        s.stop()


def test_stop_synthesized_exit_reaches_poll(spaced_tmp):
    s = TwinSession(CONFIG, _write(spaced_tmp, "main.py", BUTTON_LED_FW), cwd=spaced_tmp)
    s._proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])   # reader never ran
    s._proc.wait()
    s.stop()
    msgs = s.poll()
    assert any(m["op"] == "exit" for m in msgs), msgs
    assert s.exit_message is not None


def test_stop_within_timeout_even_if_send_blocks(spaced_tmp):
    fw = _write(spaced_tmp, "stubborn.py", STUBBORN_FW)
    s = TwinSession(CONFIG, fw, cwd=spaced_tmp)
    s.start()
    holder_release = threading.Event()
    try:
        assert s.wait_for(lambda m: m["op"] == "log" and "looping" in m["text"], START_TIMEOUT)

        def hog() -> None:                                  # simulates a writer stuck in a blocking write
            with s._send_lock:
                holder_release.wait(10)

        threading.Thread(target=hog, daemon=True).start()
        time.sleep(0.05)
        done = threading.Event()
        t0 = time.monotonic()
        threading.Thread(target=lambda: (s.stop(timeout=2.0), done.set()), daemon=True).start()
        assert done.wait(5.0), "stop() blocked on the send lock"
        assert time.monotonic() - t0 < 2.6
        assert not s.running
    finally:
        holder_release.set()
        s.stop()


def test_twin_events_do_not_block_on_protocol_writer():
    """record_event (often under pi.lock) must never write to the protocol stream itself."""
    import argparse

    from piforge.twin.runner import Runner

    gate = threading.Event()
    sent: list = []

    class SlowWriter:
        closed = False

        def send(self, msg, **kw):
            gate.wait(5)
            sent.append(msg)
            return True

    r = Runner(argparse.Namespace())
    r.writer = SlowWriter()                                 # type: ignore[assignment]
    t0 = time.monotonic()
    r._on_twin_event({"code": "TWIN.CONTENTION", "level": "warning", "message": "m"})
    assert time.monotonic() - t0 < 0.5
    gate.set()
    r._drain_events()
    assert sent and sent[0]["code"] == "TWIN.CONTENTION"
