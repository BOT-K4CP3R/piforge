"""Device models driven directly through the VirtualPi (deterministic ManualClock where timing matters)."""

from __future__ import annotations

import io
import json
import math

import pytest

from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.twin.clock import ManualClock
from piforge.twin.config import DeviceConfig, TwinConfig
from piforge.twin.devices import DEVICE_TYPES
from piforge.twin.runtime import Twin

REQUIRED = {
    "button", "switch", "limit_switch", "ir_breakbeam", "pir", "hcsr04", "led", "rgb_led", "buzzer",
    "relay", "servo", "stepper_28byj48", "dc_motor", "bme280", "ssd1306", "lcd1602_pcf8574",
    "mcp3008", "hx711", "rotary_encoder", "neopixel", "camera", "ds18b20", "dht22",
}
PROP_TYPES = {"bool", "float", "int", "enum", "image", "text"}

# 28BYJ-48 + ULN2003 half-step sequence (IN1..IN4).  # src: 28BYJ-48 datasheet, 8-step sequence
HALF = ["1000", "1100", "0100", "0110", "0010", "0011", "0001", "1001"]


def _twin(*devices: DeviceConfig, pulls=None) -> Twin:
    return Twin(TwinConfig(devices=list(devices), pulls=dict(pulls or {})), clock=ManualClock())


def test_registry_has_all_required_types():
    assert REQUIRED <= set(DEVICE_TYPES)


@pytest.mark.parametrize("dev_type", sorted(REQUIRED))
def test_every_device_type_describes_and_reports_state(dev_type):
    cls = DEVICE_TYPES[dev_type]
    cfg = cls.example_config("X1")
    assert cfg.type == dev_type
    twin = _twin(cfg)
    desc = twin.describe()
    assert len(desc) == 1
    d = desc[0]
    assert d["id"] == "X1" and d["type"] == dev_type
    json.dumps(d)
    for spec in list(d["inputs"].values()) + list(d["outputs"].values()):
        assert spec["type"] in PROP_TYPES
        assert {"min", "max", "unit", "default"} <= set(spec)
    st = twin.state()
    json.dumps(st)
    assert set(d["inputs"]) | set(d["outputs"]) <= set(st["devices"]["X1"])


def test_twin_state_shape():
    twin = _twin(DeviceConfig("D1", "led", {"pin": 17}))
    st = twin.state()
    assert set(st) >= {"t", "pins", "devices"}
    assert set(st["pins"]) == {str(i) for i in range(28)}
    assert st["devices"]["D1"]["brightness"] == 0.0


def test_unknown_device_type_suggests_close_match():
    with pytest.raises(NotFoundError, match="button"):
        _twin(DeviceConfig("SW1", "buton", {"pin": 27}))


def test_missing_pin_role_is_reported():
    with pytest.raises(ValidationError, match="echo"):
        _twin(DeviceConfig("US1", "hcsr04", {"trigger": 23}))


def test_set_input_validation():
    twin = _twin(DeviceConfig("US1", "hcsr04", {"trigger": 23, "echo": 24}))
    with pytest.raises(NotFoundError):
        twin.set_input("US1", "distnce", 1.0)
    with pytest.raises(NotFoundError):
        twin.set_input("NOPE", "distance", 1.0)
    with pytest.raises(ValidationError):
        twin.set_input("US1", "distance", "far")
    with pytest.raises(ValidationError):
        twin.set_input("US1", "distance", 99.0)
    twin.set_input("US1", "distance", "0.75")     # numeric strings are accepted
    assert twin.state()["devices"]["US1"]["distance"] == 0.75


def test_bool_inputs_accept_common_spellings():
    twin = _twin(DeviceConfig("SW1", "button", {"pin": 27}))
    for v, want in [("true", True), ("off", False), (1, True), (0, False), ("ON", True)]:
        twin.set_input("SW1", "pressed", v)
        assert twin.state()["devices"]["SW1"]["pressed"] is want


# --- simple digital devices -----------------------------------------------------------------

def test_switch_limit_switch_and_breakbeam_levels():
    twin = _twin(DeviceConfig("S1", "switch", {"pin": 5}), DeviceConfig("L1", "limit_switch", {"pin": 6}),
                 DeviceConfig("B1", "ir_breakbeam", {"pin": 13}),
                 DeviceConfig("L2", "limit_switch", {"pin": 19}, params={"normally_closed": True}))
    pi = twin.pi
    for p in (5, 6, 13, 19):
        pi.setup(p, "input", pull="up")
    assert [pi.read(p) for p in (5, 6, 13)] == [1, 1, 1]
    assert pi.read(19) == 0                      # NC contact closed to GND while not pressed
    twin.set_input("S1", "on", True)
    twin.set_input("L1", "pressed", True)
    twin.set_input("B1", "broken", True)         # receiver pulls LOW when the beam is broken
    twin.set_input("L2", "pressed", True)
    assert [pi.read(p) for p in (5, 6, 13, 19)] == [0, 0, 0, 1]


def test_led_pwm_brightness_and_active_low():
    twin = _twin(DeviceConfig("D1", "led", {"pin": 12}),
                 DeviceConfig("D2", "led", {"pin": 16}, params={"active_high": False}))
    pi = twin.pi
    pi.setup(12, "output")
    pi.setup(16, "output")
    pi.write(12, 1)
    pi.write(16, 1)
    st = twin.state()["devices"]
    assert st["D1"]["brightness"] == 1.0 and st["D1"]["on"] is True
    assert st["D2"]["brightness"] == 0.0
    pi.set_pwm(12, 100.0, 0.3)
    assert twin.state()["devices"]["D1"]["brightness"] == pytest.approx(0.3)


def test_rgb_led_color():
    twin = _twin(DeviceConfig("RGB1", "rgb_led", {"red": 5, "green": 6, "blue": 13}))
    pi = twin.pi
    for p in (5, 6, 13):
        pi.setup(p, "output")
    pi.write(5, 1)
    pi.set_pwm(6, 100.0, 0.5)
    st = twin.state()["devices"]["RGB1"]
    assert st["color"] == "#ff8000"
    assert st["red"] == 1.0 and st["green"] == pytest.approx(0.5) and st["blue"] == 0.0


def test_relay_and_active_buzzer_and_passive_buzzer():
    twin = _twin(DeviceConfig("K1", "relay", {"pin": 5}),
                 DeviceConfig("BZ1", "buzzer", {"pin": 6}),
                 DeviceConfig("BZ2", "buzzer", {"pin": 13}, params={"active": False}))
    pi = twin.pi
    for p in (5, 6, 13):
        pi.setup(p, "output")
    pi.write(5, 1)
    pi.write(6, 1)
    pi.set_pwm(13, 440.0, 0.5)
    st = twin.state()["devices"]
    assert st["K1"]["on"] is True
    assert st["BZ1"]["on"] is True
    assert st["BZ2"]["on"] is True and st["BZ2"]["frequency"] == pytest.approx(440.0)
    pi.write(13, 0)
    pi.set_pwm(13, None, None)
    assert twin.state()["devices"]["BZ2"]["on"] is False


def test_button_bounce_generates_extra_edges_then_settles():
    twin = _twin(DeviceConfig("SW1", "button", {"pin": 27}, params={"bounce_ms": 5}))
    pi = twin.pi
    pi.setup(27, "input", pull="up")
    edges = []
    pi.add_listener(27, lambda bcm, level, t: edges.append(level))
    twin.set_input("SW1", "pressed", True)
    twin.step(0.05)
    assert pi.read(27) == 0
    assert len(edges) > 1 and edges[-1] == 0


# --- motors ---------------------------------------------------------------------------------

def _stepper_twin() -> Twin:
    twin = _twin(DeviceConfig("M1", "stepper_28byj48", {"in1": 5, "in2": 6, "in3": 13, "in4": 19}))
    for p in (5, 6, 13, 19):
        twin.pi.setup(p, "output")
    return twin


def _apply(twin: Twin, pattern: str) -> None:
    for pin, bit in zip((5, 6, 13, 19), pattern):
        twin.pi.write(pin, int(bit))


def test_stepper():
    twin = _stepper_twin()
    _apply(twin, HALF[0])
    for i in range(1, 513):                 # 512 half-steps forward
        _apply(twin, HALF[i % 8])
    st = twin.state()["devices"]["M1"]
    assert st["position"] == 512
    # 4096 half-steps per output-shaft revolution  # src: 28BYJ-48 datasheet (64:1 gearbox, 5.625°/64)
    assert st["angle"] == pytest.approx(360.0 * 512 / 4096, abs=0.01)


def test_stepper_full_step_reverse():
    twin = _stepper_twin()
    full = ["1100", "0110", "0011", "1001"]
    _apply(twin, full[0])                   # first energisation snaps the rotor: use as baseline
    base = twin.state()["devices"]["M1"]["position"]
    for i in range(1, 9):
        _apply(twin, full[(-i) % 4])        # 8 full steps backwards
    assert twin.state()["devices"]["M1"]["position"] - base == -16


def test_servo_pwm_to_angle_with_slew():
    clock = ManualClock()
    twin = Twin(TwinConfig(devices=[DeviceConfig("SRV1", "servo", {"pin": 18})]), clock=clock)
    twin.pi.setup(18, "output")
    twin.pi.set_pwm(18, 50.0, 0.0875)        # 1.75 ms pulse → +45° with the gpiozero 1–2 ms convention
    twin.step(0.02)
    assert 0.0 < twin.state()["devices"]["SRV1"]["angle"] < 45.0     # still slewing
    twin.step(1.0)
    st = twin.state()["devices"]["SRV1"]
    assert st["angle"] == pytest.approx(45.0, abs=0.5)
    assert st["target_angle"] == pytest.approx(45.0, abs=0.5)
    assert st["pulse_us"] == pytest.approx(1750.0, abs=1.0)


def test_dc_motor_direction_speed_and_brake():
    twin = _twin(DeviceConfig("MOT1", "dc_motor", {"in1": 5, "in2": 6, "pwm": 13}))
    pi = twin.pi
    for p in (5, 6, 13):
        pi.setup(p, "output")
    pi.write(5, 1)
    pi.write(6, 0)
    pi.set_pwm(13, 1000.0, 0.5)
    twin.step(2.0)
    st = twin.state()["devices"]["MOT1"]
    assert st["direction"] == "forward"
    assert st["speed"] == pytest.approx(0.5, abs=0.02)
    assert st["angle"] > 0
    pi.write(5, 0)
    pi.write(6, 1)
    twin.step(2.0)
    assert twin.state()["devices"]["MOT1"]["speed"] == pytest.approx(-0.5, abs=0.02)
    pi.write(5, 1)                           # both high → brake
    twin.step(1.0)
    st = twin.state()["devices"]["MOT1"]
    assert st["direction"] == "brake" and abs(st["speed"]) < 0.02


# --- sensors --------------------------------------------------------------------------------

def test_hcsr04_echo_timing_is_exact():
    twin = _twin(DeviceConfig("US1", "hcsr04", {"trigger": 23, "echo": 24}))
    clock, pi = twin.clock, twin.pi
    twin.set_input("US1", "distance", 0.5)
    pi.setup(23, "output")
    pi.setup(24, "input", pull="down")
    edges: list[tuple[int, float]] = []
    pi.add_listener(24, lambda bcm, level, t: edges.append((level, t)))
    clock.advance(0.1)
    pi.write(23, 1)
    clock.advance(0.00001)
    pi.write(23, 0)
    t_trig = clock.now()
    twin.step(0.05)
    assert [lv for lv, _ in edges] == [1, 0]
    width = edges[1][1] - edges[0][1]
    assert edges[0][1] >= t_trig
    assert width * 343.26 / 2 == pytest.approx(0.5, abs=1e-6)


def test_hcsr04_out_of_range_gives_38ms_pulse():
    twin = _twin(DeviceConfig("US1", "hcsr04", {"trigger": 23, "echo": 24}))
    twin.set_input("US1", "distance", 4.5)
    pi = twin.pi
    pi.setup(23, "output")
    edges = []
    pi.add_listener(24, lambda bcm, level, t: edges.append(t))
    pi.write(23, 1)
    pi.write(23, 0)
    twin.step(0.1)
    assert edges[1] - edges[0] == pytest.approx(0.038, abs=1e-6)   # src: HC-SR04 datasheet, no-echo timeout


def test_pir_hold_time():
    twin = _twin(DeviceConfig("PIR1", "pir", {"pin": 4}, params={"hold_s": 2.0}))
    pi = twin.pi
    pi.setup(4, "input", pull="down")
    twin.set_input("PIR1", "motion", True)
    assert pi.read(4) == 1
    twin.set_input("PIR1", "motion", False)
    twin.step(1.0)
    assert pi.read(4) == 1                   # still held high
    twin.step(1.5)
    assert pi.read(4) == 0


def test_mcp3008_spi_protocol():
    twin = _twin(DeviceConfig("ADC1", "mcp3008", bus={"kind": "spi", "bus": 0, "cs": 0}))
    twin.set_input("ADC1", "ch0", 1.65)
    twin.set_input("ADC1", "ch3", 3.3)
    spi = twin.pi.spi[0]

    def read(ch: int, single: bool = True) -> int:
        rx = spi.transfer(0, bytes([0x01, ((8 if single else 0) | ch) << 4, 0x00]))
        return ((rx[1] & 0x03) << 8) | rx[2]

    assert read(0) == 512                    # code = 1024·Vin/Vref  # src: MCP3008 datasheet eq. 4-1
    assert read(3) == 1023
    assert read(1) == 0
    assert read(0, single=False) == 512      # CH0 − CH1 differential


def test_hx711_bitbang_read():
    params = {"scale": 420.0, "offset": 8000, "rate": 80}
    twin = _twin(DeviceConfig("LC1", "hx711", {"dout": 5, "sck": 6}, params=params))
    pi, clock = twin.pi, twin.clock
    twin.set_input("LC1", "weight", 250.0)
    pi.setup(6, "output")
    pi.setup(5, "input", pull="none")
    assert pi.read(5) == 1                   # not ready during power-up settling
    twin.step(0.2)
    assert pi.read(5) == 0                   # data ready

    value = 0
    for _ in range(24):
        pi.write(6, 1)
        clock.advance(1e-6)
        pi.write(6, 0)
        value = (value << 1) | pi.read(5)
    pi.write(6, 1)                           # 25th pulse: channel A, gain 128 next
    pi.write(6, 0)
    if value & 0x800000:
        value -= 1 << 24
    assert value == 8000 + round(250.0 * 420.0)
    assert pi.read(5) == 1                   # DOUT high until the next conversion


def test_rotary_encoder_quadrature():
    twin = _twin(DeviceConfig("ENC1", "rotary_encoder", {"a": 20, "b": 21}))
    pi = twin.pi
    pi.setup(20, "input", pull="up")
    pi.setup(21, "input", pull="up")
    seq = []

    def rec(bcm, level, t):
        seq.append((pi.read(20), pi.read(21)))

    pi.add_listener(20, rec)
    pi.add_listener(21, rec)
    twin.set_input("ENC1", "steps", 1)
    twin.step(0.2)
    # clockwise detent: A leads (gpiozero RotaryEncoder convention 0→2→3→1→0 on active-low pins)
    assert seq == [(0, 1), (0, 0), (1, 0), (1, 1)]
    assert twin.state()["devices"]["ENC1"]["position"] == 1
    twin.set_input("ENC1", "steps", -2)
    twin.step(0.2)
    assert twin.state()["devices"]["ENC1"]["position"] == -1


def test_ds18b20_and_dht22_hold_inputs():
    twin = _twin(DeviceConfig("T1", "ds18b20", {"pin": 4}), DeviceConfig("H1", "dht22", {"pin": 17}))
    twin.set_input("T1", "temperature", 21.5)
    twin.set_input("H1", "humidity", 55.0)
    st = twin.state()["devices"]
    assert st["T1"]["temperature"] == 21.5
    assert st["H1"]["humidity"] == 55.0
    assert st["T1"]["rom"].startswith("28-")


# --- I2C / SPI peripherals at register level ---------------------------------------------------

def _bme280_compensate(calib: bytes, hcal: bytes, h1: int, data: bytes) -> tuple[float, float, float]:
    """Bosch BME280 floating-point compensation.  # src: BME280 datasheet BST-BME280-DS002 §8.1"""
    import struct

    t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = struct.unpack("<HhhHhhhhhhhh", calib)
    h2, h3, e4, e5, e6, h6 = struct.unpack("<hBbBbb", hcal)
    h4 = (e4 << 4) | (e5 & 0x0F)
    h5 = (e6 << 4) | (e5 >> 4)
    adc_p = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
    adc_t = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
    adc_h = (data[6] << 8) | data[7]
    v1 = (adc_t / 16384.0 - t1 / 1024.0) * t2
    v2 = ((adc_t / 131072.0 - t1 / 8192.0) ** 2) * t3
    t_fine = v1 + v2
    temp = t_fine / 5120.0
    v1 = t_fine / 2.0 - 64000.0
    v2 = v1 * v1 * p6 / 32768.0 + v1 * p5 * 2.0
    v2 = v2 / 4.0 + p4 * 65536.0
    v1 = (p3 * v1 * v1 / 524288.0 + p2 * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * p1
    p = 1048576.0 - adc_p
    p = (p - v2 / 4096.0) * 6250.0 / v1
    p += (p9 * p * p / 2147483648.0 + p * p8 / 32768.0 + p7) / 16.0
    h = t_fine - 76800.0
    h = (adc_h - (h4 * 64.0 + h5 / 16384.0 * h)) * (
        h2 / 65536.0 * (1.0 + h6 / 67108864.0 * h * (1.0 + h3 / 67108864.0 * h)))
    h = h * (1.0 - h1 * h / 524288.0)
    return temp, p / 100.0, h


def test_bme280_register_map_and_forced_measurement():
    twin = _twin(DeviceConfig("ENV1", "bme280", bus={"kind": "i2c", "bus": 1, "address": 0x76}))
    for k, v in {"temperature": -12.25, "pressure": 987.6, "humidity": 71.0}.items():
        twin.set_input("ENV1", k, v)
    bus = twin.pi.i2c[1]
    assert bus.write_read(0x76, b"\xd0", 1) == b"\x60"          # chip id  # src: BME280 DS §5.4.1
    bus.write(0x76, bytes([0xF2, 0x01]))                         # ctrl_hum osrs_h ×1
    bus.write(0x76, bytes([0xF4, (1 << 5) | (5 << 2) | 0x01]))   # osrs_t ×1, osrs_p ×16, forced
    twin.step(0.05)
    assert bus.write_read(0x76, b"\xf3", 1)[0] & 0x08 == 0       # not measuring
    calib = bus.write_read(0x76, b"\x88", 24)
    h1 = bus.write_read(0x76, b"\xa1", 1)[0]
    hcal = bus.write_read(0x76, b"\xe1", 7)
    data = bus.write_read(0x76, b"\xf7", 8)
    t, p, h = _bme280_compensate(calib, hcal, h1, data)
    assert t == pytest.approx(-12.25, abs=0.05)
    assert p == pytest.approx(987.6, abs=0.1)
    assert h == pytest.approx(71.0, abs=0.2)
    assert bus.write_read(0x76, b"\xf4", 1)[0] & 0x03 == 0       # back to sleep after forced conversion


def _png_pixels(png: bytes) -> set[tuple[int, int]]:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("L")
    w, h = img.size
    px = img.load()
    return {(x, y) for y in range(h) for x in range(w) if px[x, y] > 127}


def test_ssd1306_raw_command_stream():
    twin = _twin(DeviceConfig("OLED1", "ssd1306", bus={"kind": "i2c", "bus": 1, "address": 0x3C}))
    bus = twin.pi.i2c[1]
    for cmd in (0xAE, 0x20, 0x00, 0xA1, 0xC8, 0xA8, 63, 0x8D, 0x14, 0xAF, 0x21, 0, 127, 0x22, 0, 7):
        bus.write(0x3C, bytes([0x80, cmd]))
    fb = bytearray(1024)
    fb[0] |= 0x01                              # (0, 0)
    fb[(20 // 8) * 128 + 10] |= 1 << (20 % 8)  # (10, 20)
    fb[7 * 128 + 127] |= 0x80                  # (127, 63)
    bus.write(0x3C, b"\x40" + bytes(fb))
    dev = twin.devices["OLED1"]
    w, h, png = dev.render_png()
    assert (w, h) == (128, 64)
    assert _png_pixels(png) == {(0, 0), (10, 20), (127, 63)}
    assert twin.state()["devices"]["OLED1"]["on"] is True


def _lcd_send(bus, value: int, rs: int) -> None:
    for nib in (value & 0xF0, (value << 4) & 0xF0):
        base = nib | 0x08 | rs                  # backlight on
        bus.write(0x27, bytes([base]))
        bus.write(0x27, bytes([base | 0x04]))   # E high
        bus.write(0x27, bytes([base]))          # E low → latch


def test_lcd1602_raw_pcf8574_stream():
    twin = _twin(DeviceConfig("LCD1", "lcd1602_pcf8574", bus={"kind": "i2c", "bus": 1, "address": 0x27}))
    bus = twin.pi.i2c[1]
    for nib in (0x30, 0x30, 0x30, 0x20):       # HD44780 4-bit init by instruction  # src: HD44780U DS fig. 24
        for b in (nib | 0x08, nib | 0x0C, nib | 0x08):
            bus.write(0x27, bytes([b]))
    for cmd in (0x28, 0x0C, 0x01, 0x06):
        _lcd_send(bus, cmd, 0)
    for ch in b"Hi!":
        _lcd_send(bus, ch, 1)
    _lcd_send(bus, 0xC0 | 0x02, 0)             # DDRAM 0x42 → row 1, col 2
    for ch in b"ok":
        _lcd_send(bus, ch, 1)
    st = twin.state()["devices"]["LCD1"]
    assert st["lines"] == ["Hi!", "  ok"]
    assert st["backlight"] is True
    w, h, png = twin.devices["LCD1"].render_png()
    assert w > 0 and h > 0 and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_neopixel_device_and_camera_frame():
    twin = _twin(DeviceConfig("NP1", "neopixel", {"pin": 18}, params={"count": 4}),
                 DeviceConfig("CAM1", "camera"))
    np_dev = twin.devices["NP1"]
    np_dev.set_pixels([(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0)], brightness=1.0)
    assert twin.state()["devices"]["NP1"]["pixels"] == ["#ff0000", "#00ff00", "#0000ff", "#000000"]
    frame = twin.devices["CAM1"].frame((64, 48))
    assert frame.shape == (48, 64, 3) and str(frame.dtype) == "uint8"
    assert frame.std() > 10                    # a real pattern, not a flat colour


def test_events_surface_in_twin_log():
    twin = _twin(DeviceConfig("SW1", "button", {"pin": 17}))
    twin.pi.setup(17, "output")
    twin.pi.write(17, 1)
    twin.set_input("SW1", "pressed", True)
    assert any(e["code"] == "TWIN.CONTENTION" for e in twin.pi.events)
    assert math.isfinite(twin.clock.now())
    with pytest.raises(PiForgeError):
        twin.step(-1.0)


def _hx711_ready(params=None):
    twin = _twin(DeviceConfig("LC1", "hx711", {"dout": 5, "sck": 6}, params=params or {"rate": 80}))
    pi = twin.pi
    pi.setup(6, "output")
    pi.setup(5, "input", pull="none")
    twin.step(0.2)
    assert pi.read(5) == 0
    return twin


def _hx711_pulse(twin, high: float) -> None:
    twin.pi.write(6, 1)
    twin.clock.advance(high)
    twin.pi.write(6, 0)
    twin.clock.advance(1e-6)


def test_hx711_slow_gain_pulses_do_not_power_down():
    """Python jitter (> 60 µs high) on pulses 25–27 must not be taken as a power-down."""
    twin = _hx711_ready()
    for _ in range(24):
        _hx711_pulse(twin, 1e-6)
    _hx711_pulse(twin, 200e-6)                 # 25th
    _hx711_pulse(twin, 300e-6)                 # 26th → next conversion: channel B, gain 32
    st = twin.state()["devices"]["LC1"]
    assert st["powered"] is True
    assert st["gain"] == 32                    # src: HX711 datasheet table 3 (26 pulses → B, 32)


def test_hx711_long_high_after_read_powers_down():
    twin = _hx711_ready()
    for _ in range(25):
        _hx711_pulse(twin, 1e-6)
    twin.pi.write(6, 1)
    twin.step(0.1)                             # held high 100 ms → powered down
    assert twin.state()["devices"]["LC1"]["powered"] is False
    twin.pi.write(6, 0)
    assert twin.state()["devices"]["LC1"]["gain"] == 128
