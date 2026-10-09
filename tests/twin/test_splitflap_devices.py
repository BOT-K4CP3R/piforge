"""Money-counter twin devices: 74HC595 chain, stepper ``coil_source``, ``splitflap``, ``ws_feed``."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from piforge.core.errors import ValidationError
from piforge.twin.clock import ManualClock
from piforge.twin.config import DeviceConfig, TwinConfig
from piforge.twin.runtime import Twin
from piforge.twin.session import TwinSession

START_TIMEOUT = 30.0
# Half-step patterns as IN1..IN4 bit masks (IN1 = bit 0), forward order of the twin's HALF_STEP table.
HALF = [0b0001, 0b0011, 0b0010, 0b0110, 0b0100, 0b1100, 0b1000, 0b1001]


def _sr(length: int = 4, **kw) -> DeviceConfig:
    return DeviceConfig("SR1", "shift_register_74hc595", bus={"kind": "spi", "bus": 0, "cs": 0},
                        params={"length": length}, **kw)


def _motor(ref: str, bits: list[int]) -> DeviceConfig:
    return DeviceConfig(ref, "stepper_28byj48", params={"coil_source": {"device": "SR1", "bits": bits}})


def _flap(ref: str, stepper: str, **params) -> DeviceConfig:
    return DeviceConfig(ref, "splitflap", params={"stepper": stepper, **params})


# --- 74HC595 ----------------------------------------------------------------------------------------
def test_shift_register_spidev_daisy_chain(shims, make_twin):
    twin = make_twin(_sr(4), clock=ManualClock(), start=False)
    import spidev

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.xfer2([0x01, 0x02, 0x03, 0x04])
    st = twin.state()["devices"]["SR1"]
    assert st["bits"] == 0x01020304                 # first byte → last chip (chip 3)
    assert st["bytes"] == "04 03 02 01"             # chip 0 (nearest the Pi) first
    assert st["latches"] == 1 and st["enabled"] is True
    sr = twin.device("SR1")
    assert sr.bit(0) == 0 and sr.bit(2) == 1 and sr.bit(24) == 1   # chip 0 = 0x04 → Q2; chip 3 = 0x01 → Q0
    spi.writebytes([0xAA])                          # a short transfer shifts the chain by one chip
    assert twin.state()["devices"]["SR1"]["bits"] == 0x020304AA


def test_shift_register_bitbanged_gpiozero_with_oe(gpiozero_twin):
    from gpiozero import OutputDevice

    twin = gpiozero_twin(DeviceConfig("SR1", "shift_register_74hc595",
                                      pins={"SER": 17, "SRCLK": 27, "RCLK": 22, "OE": 23}, params={"length": 2}))
    data, clock, latch, oe = (OutputDevice(n) for n in (17, 27, 22, 23))
    for byte in (0xA5, 0x3C):                       # 0xA5 shifted first → chip 1
        for i in range(7, -1, -1):
            data.value = (byte >> i) & 1
            clock.on()
            clock.off()
    st = twin.state()["devices"]["SR1"]
    assert st["bits"] == 0 and st["latches"] == 0   # nothing latched yet
    latch.on()
    latch.off()
    assert twin.state()["devices"]["SR1"]["bits"] == 0xA53C
    oe.on()                                          # OE is active low: outputs off
    st = twin.state()["devices"]["SR1"]
    assert st["bits"] == 0 and st["enabled"] is False
    oe.off()
    assert twin.state()["devices"]["SR1"]["bits"] == 0xA53C


def test_shift_register_spi_with_separate_latch_pin(shims, make_twin):
    twin = make_twin(_sr(1, pins={"latch": 25}), clock=ManualClock(), start=False)
    import spidev
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(25, GPIO.OUT, initial=GPIO.LOW)
    spi = spidev.SpiDev(0, 0)
    spi.xfer2([0x81])
    assert twin.state()["devices"]["SR1"]["bits"] == 0     # CS release does not latch here
    GPIO.output(25, GPIO.HIGH)
    assert twin.state()["devices"]["SR1"]["bits"] == 0x81
    GPIO.cleanup()


def test_shift_register_bad_length():
    with pytest.raises(ValidationError, match="length"):
        Twin(TwinConfig(devices=[_sr(0)]), clock=ManualClock())


# --- stepper coil_source -------------------------------------------------------------------------------
def test_stepper_coil_source_counts_steps_through_chain(shims, make_twin):
    twin = make_twin(_sr(4), _motor("M1", [0, 1, 2, 3]), _motor("M8", [28, 29, 30, 31]),
                     clock=ManualClock(), start=False)
    import spidev

    spi = spidev.SpiDev(0, 0)
    for i in range(17):                              # first pattern only sets the phase
        word = (HALF[i % 8] << 28) | HALF[(-i) % 8]  # M8 forward, M1 backward
        spi.xfer2(list(word.to_bytes(4, "big")))
        twin.step(0.002)
    st = twin.state()["devices"]
    assert st["M8"]["position"] == 16 and st["M1"]["position"] == -16
    assert st["M8"]["coils"] == "1000" and st["M8"]["energized"] is True
    spi.xfer2([0, 0, 0, 0])                          # de-energised: rotor holds, position unchanged
    st = twin.state()["devices"]
    assert st["M8"]["energized"] is False and st["M8"]["position"] == 16


def test_stepper_coil_source_validation():
    with pytest.raises(ValidationError, match="not a device"):
        Twin(TwinConfig(devices=[_motor("M1", [0, 1, 2, 3])]), clock=ManualClock())
    with pytest.raises(ValidationError, match="do not exist"):
        Twin(TwinConfig(devices=[_sr(1), _motor("M1", [6, 7, 8, 9])]), clock=ManualClock())
    with pytest.raises(ValidationError, match="coil_source must be"):
        Twin(TwinConfig(devices=[_sr(1), _motor("M1", [0, 1])]), clock=ManualClock())
    with pytest.raises(ValidationError, match="expected"):
        Twin(TwinConfig(devices=[DeviceConfig("D1", "led", {"pin": 17}),
                                 DeviceConfig("M1", "stepper_28byj48",
                                              params={"coil_source": {"device": "D1", "bits": [0, 1, 2, 3]}})]),
             clock=ManualClock())
    # classic GPIO wiring still works and still requires all four pins
    with pytest.raises(ValidationError, match="in4"):
        Twin(TwinConfig(devices=[DeviceConfig("M1", "stepper_28byj48", {"in1": 5, "in2": 6, "in3": 13})]),
             clock=ManualClock())


# --- splitflap ------------------------------------------------------------------------------------------
class _Rig:
    """SR1 (1 chip) → M1 → F1, driven like the firmware does: one spidev transfer per half-step."""

    def __init__(self, make_twin, **flap_params) -> None:
        self.twin = make_twin(_sr(1), _motor("M1", [0, 1, 2, 3]),
                              _flap("F1", "M1", hall_pin=4, **flap_params), clock=ManualClock(), start=False)
        import spidev

        self.spi = spidev.SpiDev(0, 0)
        self.phase = 0
        self.spi.xfer2([HALF[0]])

    def step(self, n: int = 1, dt: float = 1 / 600) -> None:
        for _ in range(n):
            self.phase = (self.phase + 1) % 8
            self.spi.xfer2([HALF[self.phase]])
            self.twin.step(dt)

    def flap(self) -> dict:
        return self.twin.state()["devices"]["F1"]


def test_splitflap_digit_flip_and_hall(shims, make_twin):
    rig = _Rig(make_twin, start_angle=0.0, home_angle=0.0, offset_steps=0, flip_time=0.08)
    twin = rig.twin
    twin.pi.setup(4, "input", pull="up")
    st = rig.flap()
    assert st["digit"] == 0 and st["next_digit"] == 1 and st["flip"] == 0.0
    assert st["hall"] is True and twin.pi.read(4) == 0       # magnet under the sensor at home
    rig.step(205, dt=0.0)                                     # 204.8 half-steps per flap (4096 / 20)
    assert rig.flap()["angle"] == pytest.approx(205 * 360 / 4096, abs=1e-3)
    assert rig.flap()["hall"] is False and twin.pi.read(4) == 1   # released → pull-up
    twin.step(0.04)
    st = rig.flap()
    assert st["digit"] == 0 and st["next_digit"] == 1 and 0.3 < st["flip"] < 0.7   # flap falling
    twin.step(0.06)
    st = rig.flap()
    assert st["digit"] == 1 and st["next_digit"] == 2 and st["flip"] == 0.0
    rig.step(205 * 9 - 205 // 2)                              # stop mid-flap of digit 9 … no extra flap
    twin.step(0.5)
    assert rig.flap()["digit"] == 9 and rig.flap()["flip"] == 0.0
    rig.step(205 // 2 + 3)
    twin.step(0.2)
    st = rig.flap()
    assert st["flap"] == 10 and st["digit"] == 0 and st["next_digit"] == 1   # second set of 0–9


def test_splitflap_homing_offset_and_wrap(shims, make_twin):
    rig = _Rig(make_twin, start_angle=330.0, home_angle=0.0, offset_steps=20, hall_window=10.0)
    twin = rig.twin
    twin.pi.setup(4, "input", pull="up")
    assert twin.pi.read(4) == 1
    n = 0
    while twin.pi.read(4) == 1:                               # firmware homing: rotate until the edge
        rig.step()
        n += 1
        assert n < 5000
    assert n == 342                                           # 30° = 341.3 half-steps
    rig.step(20)                                              # module offset → digit 0 aligned
    twin.step(0.2)
    st = rig.flap()
    assert st["digit"] == 0 and st["flip"] == 0.0 and st["hall"] is True
    rig.step(615)                                             # 3 flaps
    twin.step(0.3)
    assert rig.flap()["digit"] == 3 and rig.flap()["hall"] is False
    rig.step(4096 - 615)                                      # a full turn later: 20 flaps, back to 0
    twin.step(0.3)
    st = rig.flap()
    assert st["digit"] == 0 and st["flap"] == 0 and st["hall"] is True


def test_splitflap_fast_spin_never_lags_more_than_one_flap(shims, make_twin):
    rig = _Rig(make_twin, start_angle=0.0, flip_time=0.08)
    rig.step(205 * 6, dt=0.0)                                 # instantaneous jump of 6 flaps
    st = rig.flap()
    assert st["digit"] == 5 and st["next_digit"] == 6
    rig.twin.step(0.1)
    assert rig.flap()["digit"] == 6


def test_splitflap_needs_a_stepper():
    with pytest.raises(ValidationError, match="stepper"):
        Twin(TwinConfig(devices=[DeviceConfig("F1", "splitflap", params={"hall_pin": 4})]), clock=ManualClock())
    with pytest.raises(ValidationError, match="not a device"):
        Twin(TwinConfig(devices=[_flap("F1", "M9")]), clock=ManualClock())


def test_new_types_describe_with_class_level_specs():
    from piforge.twin.devices import DEVICE_TYPES

    for key, outs in {"shift_register_74hc595": {"bits"},
                      "splitflap": {"angle", "digit", "next_digit", "flip", "hall"},
                      "ws_feed": {"clients", "port"}}.items():
        assert outs <= set(DEVICE_TYPES[key].outputs)
    assert {"amount", "online"} == set(DEVICE_TYPES["ws_feed"].inputs)


# --- ws_feed --------------------------------------------------------------------------------------------
def test_ws_feed_in_process_push_offline_online():
    from websockets.sync.client import connect

    twin = Twin(TwinConfig(devices=[DeviceConfig("WS1", "ws_feed", params={"port": 0})]), clock=ManualClock())
    assert twin.env() == {}                                   # port unknown until the server runs
    twin.start()
    try:
        url = twin.env()["MONEY_COUNTER_URL"]
        port = twin.state()["devices"]["WS1"]["port"]
        assert port > 0 and url == f"ws://127.0.0.1:{port}/"
        twin.set_input("WS1", "amount", 12.5)
        with connect(url, open_timeout=3) as ws:
            assert json.loads(ws.recv(timeout=3)) == {"amount": 12.5}         # pushed on connect
            deadline = time.monotonic() + 3
            while twin.state()["devices"]["WS1"]["clients"] != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert twin.state()["devices"]["WS1"]["clients"] == 1
            twin.set_input("WS1", "amount", 1234.56)
            assert json.loads(ws.recv(timeout=3)) == {"amount": 1234.56}     # pushed on change
            twin.set_input("WS1", "online", False)
            from websockets.exceptions import ConnectionClosed

            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=3)
        with pytest.raises(OSError):
            connect(url, open_timeout=1).close()              # refused while offline
        twin.set_input("WS1", "online", True)
        deadline = time.monotonic() + 3
        while True:
            try:
                with connect(url, open_timeout=1) as ws:
                    assert json.loads(ws.recv(timeout=3)) == {"amount": 1234.56}
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
    finally:
        twin.stop()
    assert twin.state()["devices"]["WS1"]["clients"] == 0


# --- through the real runner -----------------------------------------------------------------------------
SPLITFLAP_FW = """\
import time
import spidev
import RPi.GPIO as GPIO

HALF = [0b0001, 0b0011, 0b0010, 0b0110, 0b0100, 0b1100, 0b1000, 0b1001]
HALL, OFFSET, PER_FLAP = 4, 20, 4096 / 20
GPIO.setmode(GPIO.BCM)
GPIO.setup(HALL, GPIO.IN, pull_up_down=GPIO.PUD_UP)
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1_000_000
phase = 0

def write(pattern):
    word = pattern << 24                       # motor on chip 3 (QA..QD) of a 4-chip chain
    spi.xfer2(list(word.to_bytes(4, "big")))

def step(n):
    global phase
    for _ in range(n):
        phase = (phase + 1) % 8
        write(HALF[phase])
        time.sleep(0.0012)

write(HALF[0])
steps = 0
while GPIO.input(HALL):                        # homing: rotate until the Hall edge
    step(1)
    steps += 1
print("HOMED", steps, flush=True)
step(OFFSET)
step(round(3 * PER_FLAP))
write(0)                                       # de-energise the coils when idle
print("SHOW 3", flush=True)
while True:
    time.sleep(1)
"""


def test_runner_firmware_homes_and_shows_digit(spaced_tmp: Path):
    fw = spaced_tmp / "main.py"
    fw.write_text(SPLITFLAP_FW, encoding="utf-8")
    cfg = TwinConfig(board="rpizero2w", devices=[
        _sr(4), _motor("M4", [24, 25, 26, 27]),
        _flap("F4", "M4", hall_pin=4, position=3, start_angle=330.0, offset_steps=20)])
    s = TwinSession(cfg, fw, cwd=spaced_tmp)
    s.start()
    try:
        assert s.wait_for(lambda m: m["op"] == "log" and "SHOW 3" in m["text"], START_TIMEOUT), s.logs()[-5:]
        assert any("HOMED 342" in line for line in s.logs())
        msg = s.wait_for(lambda m: m["op"] == "state" and m["devices"]["F4"]["digit"] == 3
                         and m["devices"]["F4"]["flip"] == 0.0, 5.0)
        assert msg is not None, s.latest_state()
        st = msg["devices"]
        assert st["F4"]["hall"] is False and st["F4"]["position"] == 3
        assert st["M4"]["energized"] is False and st["SR1"]["bits"] == 0
    finally:
        s.stop()
    assert not s.running


WS_FW = """\
import json, os, time
from websockets.sync.client import connect

url = os.environ["MONEY_COUNTER_URL"]
print("URL", url, flush=True)
while True:
    try:
        with connect(url, open_timeout=1) as ws:
            print("CONNECTED", flush=True)
            for msg in ws:
                print("GOT", json.loads(msg)["amount"], flush=True)
        print("CLOSED", flush=True)
    except OSError:
        print("REFUSED", flush=True)
    except Exception as exc:
        print("ERR", type(exc).__name__, exc, flush=True)
    time.sleep(0.2)
"""


def test_runner_ws_feed_round_trip(spaced_tmp: Path):
    fw = spaced_tmp / "main.py"
    fw.write_text(WS_FW, encoding="utf-8")
    cfg = TwinConfig(devices=[DeviceConfig("WS1", "ws_feed", params={"port": 0})])
    s = TwinSession(cfg, fw, cwd=spaced_tmp)
    s.start()
    try:
        def logged(text: str, timeout: float = 10.0) -> bool:
            return s.wait_for(lambda m: m["op"] == "log" and text in m["text"], timeout) is not None

        assert logged("GOT 0.0", START_TIMEOUT), s.logs()[-5:]
        url = next(line for line in s.logs() if line.startswith("URL "))
        port = s.wait_for(lambda m: m["op"] == "state" and m["devices"]["WS1"]["clients"] == 1, 5.0)
        assert port is not None and url == f"URL ws://127.0.0.1:{port['devices']['WS1']['port']}/"
        s.send_input("WS1", "amount", 1234.56)
        assert logged("GOT 1234.56"), s.logs()[-5:]
        s.send_input("WS1", "online", False)
        assert logged("CLOSED") and logged("REFUSED"), s.logs()[-5:]
        s.send_input("WS1", "amount", 7.5)
        s.send_input("WS1", "online", True)
        assert logged("GOT 7.5"), s.logs()[-5:]
    finally:
        s.stop()
    assert not s.running
    assert "MONEY_COUNTER_URL" not in os.environ          # set only inside the runner process


# --- from_circuit ---------------------------------------------------------------------------------------
def _chain_circuit(data: str = "GPIO10", clock: str = "GPIO11", latch: str = "GPIO8"):
    from piforge.elec import Circuit

    c = Circuit("chain")
    pi = c.add("rpizero2w", "U1")
    srs = [c.add("sn74hct595", f"U{10 + i}") for i in range(2)]
    c.connect(pi[data], srs[0]["SER"])
    for i, sr in enumerate(srs):
        c.connect(pi[clock], sr["SRCLK"])
        c.connect(pi[latch], sr["RCLK"])
        c.connect(sr["VCC"], pi["5V"], sr["SRCLR"])
        c.connect(sr["GND"], pi["GND"], sr["OE"])
        if i:
            c.connect(srs[i - 1]["QH'"], sr["SER"])
    for k, (chip, first) in enumerate(((0, 4), (1, 0))):      # M1 on U10 QE..QH, M2 on U11 QA..QD
        drv, m = c.add("uln2003a", f"U{20 + k}"), c.add("stepper_28byj48", f"M{k + 1}")
        c.connect(drv["E"], pi["GND"])
        c.connect(drv["COM"], pi["5V"], m["COM"])
        for j, coil in enumerate("ABCD"):
            c.connect(srs[chip]["Q" + "ABCDEFGH"[first + j]], drv[f"{j + 1}B"])
            c.connect(drv[f"{j + 1}C"], m[coil])
    return c


def test_from_circuit_spi_chain_and_coil_source():
    from piforge.twin.from_circuit import twin_config_from_circuit

    cfg = twin_config_from_circuit(_chain_circuit())
    devs = {d.id: d for d in cfg.devices}
    assert set(devs) == {"U10", "M1", "M2"}                    # U11 is part of U10's chain
    sr = devs["U10"]
    assert sr.type == "shift_register_74hc595" and sr.bus == {"kind": "spi", "bus": 0, "cs": 0} and sr.pins == {}
    assert sr.params["length"] == 2 and sr.params["chain"] == ["U10", "U11"]
    assert devs["M1"].pins == {} and devs["M1"].params["coil_source"] == {"device": "U10", "bits": [4, 5, 6, 7]}
    assert devs["M2"].params["coil_source"] == {"device": "U10", "bits": [8, 9, 10, 11]}
    twin = Twin(cfg, clock=ManualClock())                      # the derived config builds
    assert twin.device("M2")._source is twin.device("U10")


def test_from_circuit_bitbanged_and_separate_latch():
    from piforge.twin.from_circuit import twin_config_from_circuit

    cfg = twin_config_from_circuit(_chain_circuit("GPIO17", "GPIO27", "GPIO22"))
    sr = cfg.device("U10")
    assert sr.bus is None and sr.pins == {"data": 17, "clock": 27, "latch": 22}
    cfg = twin_config_from_circuit(_chain_circuit(latch="GPIO25"))
    sr = cfg.device("U10")
    assert sr.bus is not None and "cs_pin" not in sr.bus and sr.pins == {"latch": 25}
    Twin(cfg, clock=ManualClock())
