"""Sensors: HC-SR04 ultrasonic, BME280 (register-accurate), DS18B20, DHT22, HX711 load-cell ADC."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from typing import Any

from piforge.twin.bus import I2CDevice, SPIDevice
from piforge.twin.devices.base import Device, PropSpec, register


@register
class HCSR04(Device):
    """HC-SR04 ultrasonic ranger: TRIG falling edge → ECHO high for 2·d/c.

    Echo edges are scheduled at exact simulated times, so both gpiozero's edge-timestamp method and
    polling firmware measure the true width. Out of range → 38 ms pulse.
    # src: HC-SR04 datasheet (ElecFreaks) — 10 µs trigger, range 2 cm–4 m, 38 ms "no obstacle" pulse
    # src: gpiozero DistanceSensor default speed_of_sound 343.26 m/s (dry air ≈ 20 °C)
    """

    type = "hcsr04"
    label = "Ultrasonic HC-SR04"
    pin_roles = ("trigger", "echo")
    pin_aliases = {"trig": "trigger", "tr": "trigger", "ec": "echo"}
    defaults = {"speed_of_sound": 343.26, "echo_delay": 0.0005, "min_range": 0.02, "max_range": 4.0,
                "timeout_pulse": 0.038}
    inputs = {"distance": PropSpec("float", 0.0, 6.0, unit="m", default=1.0, label="Distance",
                                   widget="slider")}
    outputs = {"echo_us": PropSpec("float", 0.0, 40000.0, unit="µs", default=0.0, label="Last echo"),
               "pings": PropSpec("int", 0, None, default=0, label="Pings")}
    example = {"pins": {"trigger": 23, "echo": 24}}

    def setup(self) -> None:
        self._trig_rise: float | None = None
        self._busy_until = float("-inf")
        self._pings = 0
        self._echo_us = 0.0
        self.pi.drive(self.pins["echo"], self.id, 0)
        self.listen(self.pins["trigger"], self._on_trigger)

    def _on_trigger(self, bcm: int, level: int, t: float) -> None:
        if level:
            self._trig_rise = t
            return
        if self._trig_rise is None or t < self._busy_until:
            return
        self._trig_rise = None
        p = self.params
        d = float(self._inputs["distance"])
        if d > float(p["max_range"]):
            width = float(p["timeout_pulse"])
        else:
            width = 2.0 * max(d, float(p["min_range"])) / float(p["speed_of_sound"])
        t_rise = t + float(p["echo_delay"])
        t_fall = t_rise + width
        self._busy_until = t_fall
        self._pings += 1
        self._echo_us = width * 1e6
        echo = self.pins["echo"]
        self.pi.call_at(t_rise, lambda: self.pi.drive(echo, self.id, 1, t=t_rise))
        self.pi.call_at(t_fall, lambda: self.pi.drive(echo, self.id, 0, t=t_fall))

    def outputs_state(self) -> dict:
        return {"echo_us": round(self._echo_us, 2), "pings": self._pings}


# --- BME280 ------------------------------------------------------------------------------------
# Trim values: T/P from the Bosch BMP280 datasheet compensation example (§3.12); H from a typical
# production BME280. Any plausible set works because the twin inverts the same formulas.
# src: Bosch BST-BMP280-DS001 §3.12 (dig_T1..dig_P9 example), BST-BME280-DS002 §4.2.2 (register layout)
_CAL = {"T1": 27504, "T2": 26435, "T3": -1000, "P1": 36477, "P2": -10685, "P3": 3024, "P4": 2855,
        "P5": 140, "P6": -7, "P7": 15500, "P8": -14600, "P9": 6000,
        "H1": 75, "H2": 362, "H3": 0, "H4": 313, "H5": 50, "H6": 30}
_STANDBY_MS = [0.5, 62.5, 125.0, 250.0, 500.0, 1000.0, 10.0, 20.0]   # src: BME280 DS table 27 (t_sb)
_FILTER = [1, 2, 4, 8, 16, 16, 16, 16]                                # src: BME280 DS table 28
_OS = [0, 1, 2, 4, 8, 16, 16, 16]                                      # src: BME280 DS tables 20/23/24


def _t_fine(adc_t: float) -> int:
    c = _CAL
    v1 = (adc_t / 16384.0 - c["T1"] / 1024.0) * c["T2"]
    v2 = ((adc_t / 131072.0 - c["T1"] / 8192.0) ** 2) * c["T3"]
    return int(v1 + v2)


def _pressure_pa(adc_p: float, t_fine: int) -> float:
    c = _CAL
    v1 = t_fine / 2.0 - 64000.0
    v2 = v1 * v1 * c["P6"] / 32768.0 + v1 * c["P5"] * 2.0
    v2 = v2 / 4.0 + c["P4"] * 65536.0
    v1 = (c["P3"] * v1 * v1 / 524288.0 + c["P2"] * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * c["P1"]
    p = (1048576.0 - adc_p - v2 / 4096.0) * 6250.0 / v1
    return p + (c["P9"] * p * p / 2147483648.0 + p * c["P8"] / 32768.0 + c["P7"]) / 16.0


def _humidity(adc_h: float, t_fine: int) -> float:
    c = _CAL
    v = t_fine - 76800.0
    v = (adc_h - (c["H4"] * 64.0 + c["H5"] / 16384.0 * v)) * (
        c["H2"] / 65536.0 * (1.0 + c["H6"] / 67108864.0 * v * (1.0 + c["H3"] / 67108864.0 * v)))
    return v * (1.0 - c["H1"] * v / 524288.0)


def _search(f: Callable[[int], float], target: float, hi: int, increasing: bool) -> int:
    """Smallest-error integer x in [0, hi] for a monotonic f (binary search)."""
    lo_x, hi_x = 0, hi
    while lo_x < hi_x:
        mid = (lo_x + hi_x) // 2
        val = f(mid)
        if (val < target) == increasing:
            lo_x = mid + 1
        else:
            hi_x = mid
    best = lo_x
    if best > 0 and abs(f(best - 1) - target) < abs(f(best) - target):
        best -= 1
    return best


class _BMEBus(I2CDevice, SPIDevice):
    """Bus front-end for the BME280 register file (I2C or 4-wire SPI)."""

    def __init__(self, dev: "BME280") -> None:
        self.dev = dev
        self.pointer = 0
        self._spi_state = "cmd"

    # I2C: first byte = register pointer; further written bytes are (value, reg, value…) pairs
    def on_write(self, data: bytes) -> None:
        if not data:
            return
        self.pointer = data[0]
        rest = data[1:]
        if rest:
            self.dev.write_register(self.pointer, rest[0])
            for i in range(1, len(rest) - 1, 2):     # burst write = reg/value pairs  # src: BME280 DS §6.2.1
                self.dev.write_register(rest[i], rest[i + 1])

    def on_read(self, n: int) -> bytes:
        out = bytes(self.dev.read_register((self.pointer + i) & 0xFF) for i in range(n))
        self.pointer = (self.pointer + n) & 0xFF
        return out

    # SPI: control byte bit7 = 1 read (auto-increment), 0 write (pairs); MSB of address replaced
    def begin(self) -> None:
        self._spi_state = "cmd"

    def transfer(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if self._spi_state == "cmd":
                out.append(0xFF)
                self.pointer = (b & 0x7F) | 0x80
                self._spi_state = "read" if b & 0x80 else "write"
            elif self._spi_state == "read":
                out.append(self.dev.read_register(self.pointer))
                self.pointer = (self.pointer + 1) & 0xFF
            else:
                out.append(0xFF)
                self.dev.write_register(self.pointer, b)
                self._spi_state = "cmd"
        return bytes(out)


@register
class BME280(Device):
    """Bosch BME280 temperature/pressure/humidity sensor with its real register map.

    Sleep/forced/normal modes, oversampling (incl. "skipped" = 0x80000), IIR filter, soft reset,
    status ``measuring`` bit and calibration trims; raw ADC values are found by inverting the
    datasheet compensation formulas, so any driver (Adafruit, Bosch, smbus2 scripts) reads back the
    input values. I2C (0x76/0x77) or SPI.  # src: Bosch BST-BME280-DS002 (§4.2.3, §5.3–5.4, §9)
    """

    type = "bme280"
    label = "BME280 environment sensor"
    bus_kinds = ("i2c", "spi")
    default_address = 0x76
    inputs = {"temperature": PropSpec("float", -40.0, 85.0, unit="°C", default=22.0, label="Temperature",
                                      widget="slider"),
              "pressure": PropSpec("float", 300.0, 1100.0, unit="hPa", default=1013.25, label="Pressure",
                                   widget="slider"),
              "humidity": PropSpec("float", 0.0, 100.0, unit="%RH", default=45.0, label="Humidity",
                                   widget="slider")}
    outputs = {"mode": PropSpec("enum", default="sleep", choices=("sleep", "forced", "normal")),
               "measurements": PropSpec("int", 0, None, default=0, label="Conversions")}
    example = {"bus": {"kind": "i2c", "bus": 1, "address": 0x76}}

    def setup(self) -> None:
        self.regs = bytearray(256)
        c = _CAL
        self.regs[0x88:0xA0] = struct.pack("<HhhHhhhhhhhh", c["T1"], c["T2"], c["T3"], c["P1"], c["P2"],
                                           c["P3"], c["P4"], c["P5"], c["P6"], c["P7"], c["P8"], c["P9"])
        self.regs[0xA1] = c["H1"]
        h4, h5 = c["H4"], c["H5"]
        self.regs[0xE1:0xE8] = struct.pack("<hBbBbb", c["H2"], c["H3"], h4 >> 4,
                                           ((h5 & 0x0F) << 4) | (h4 & 0x0F), h5 >> 4, c["H6"])
        self.regs[0xD0] = 0x60                                     # chip id
        self._front = _BMEBus(self)
        assert self.bus is not None
        if self.bus["kind"] == "i2c":
            self.pi.i2c_bus(self.bus["bus"]).attach(self.bus["address"], self._front)
        else:
            self.pi.spi[self.bus["bus"]].attach(self.bus["cs"], self._front, cs_gpio=self.bus.get("cs_pin"))
        self._count = 0
        self._reset()

    def _reset(self) -> None:
        for r in (0xF2, 0xF3, 0xF4, 0xF5):
            self.regs[r] = 0
        self.regs[0xF7:0xFF] = bytes([0x80, 0, 0, 0x80, 0, 0, 0x80, 0])   # power-on/skipped values
        self._measuring_until: float | None = None
        self._next_start: float | None = None
        self._filt: dict[str, float] = {}

    # -- register access ------------------------------------------------------------------------
    def write_register(self, reg: int, value: int) -> None:
        with self.pi.lock:
            if reg == 0xE0:
                if value == 0xB6:
                    self._reset()
            elif reg in (0xF2, 0xF5):
                self.regs[reg] = value & 0xFF
            elif reg == 0xF4:
                self.regs[0xF4] = value & 0xFF
                mode = value & 0x03
                if mode in (1, 2):
                    self._start(self.pi.clock.now())
                elif mode == 3:
                    self._start(self.pi.clock.now())
                else:
                    self._next_start = None

    def read_register(self, reg: int) -> int:
        with self.pi.lock:
            self._service(self.pi.clock.now())
            if reg == 0xF3:
                return 0x08 if self._measuring_until is not None else 0x00
            return self.regs[reg]

    # -- conversion timing ----------------------------------------------------------------------
    def _t_measure(self) -> float:
        osr_t = _OS[(self.regs[0xF4] >> 5) & 7]
        osr_p = _OS[(self.regs[0xF4] >> 2) & 7]
        osr_h = _OS[self.regs[0xF2] & 7]
        ms = 1.25 + 2.3 * osr_t + (2.3 * osr_p + 0.575 if osr_p else 0) + (2.3 * osr_h + 0.575 if osr_h else 0)
        return ms / 1000.0                                  # src: BME280 DS §9.1 t_measure,max

    def _start(self, now: float) -> None:
        self._measuring_until = now + self._t_measure()
        self._next_start = None

    def _service(self, now: float) -> None:
        if self._measuring_until is None:
            if self._next_start is not None and now >= self._next_start and (self.regs[0xF4] & 3) == 3:
                self._start(self._next_start)
            else:
                return
        if self._measuring_until is None or now < self._measuring_until:
            return
        done = self._measuring_until
        self._convert()
        self._measuring_until = None
        if (self.regs[0xF4] & 3) == 3:
            t_sb = _STANDBY_MS[(self.regs[0xF5] >> 5) & 7] / 1000.0
            self._next_start = done + t_sb
            if now >= self._next_start:                       # catch up without busy recursion
                self._start(now)
        else:
            self.regs[0xF4] &= ~0x03                          # forced → back to sleep

    def tick(self, t: float, dt: float) -> None:
        self._service(t)

    def _convert(self) -> None:
        self._count += 1
        osr_t = (self.regs[0xF4] >> 5) & 7
        osr_p = (self.regs[0xF4] >> 2) & 7
        osr_h = self.regs[0xF2] & 7
        coef = _FILTER[(self.regs[0xF5] >> 2) & 7]
        temp = float(self._inputs["temperature"])
        adc_t = _search(lambda x: _t_fine(x) / 5120.0, temp, (1 << 20) - 1, True)
        t_fine = _t_fine(adc_t)
        adc_p = _search(lambda x: _pressure_pa(x, t_fine), float(self._inputs["pressure"]) * 100.0,
                        (1 << 20) - 1, False)
        adc_h = _search(lambda x: _humidity(x, t_fine), float(self._inputs["humidity"]), 0xFFFF, True)

        def filt(key: str, raw: int, osr: int) -> int:
            if osr == 0:
                self._filt.pop(key, None)
                return 0x80000
            bits = 20 if coef > 1 else 15 + min(osr, 5)          # resolution 16…20 bit (DS §3.4.2)
            raw &= ~((1 << (20 - bits)) - 1)
            prev = self._filt.get(key)
            val = raw if prev is None or coef == 1 else (prev * (coef - 1) + raw) / coef
            self._filt[key] = val
            return int(round(val))

        p20 = filt("p", adc_p, osr_p)
        t20 = filt("t", adc_t, osr_t)
        h16 = adc_h if osr_h else 0x8000
        self.regs[0xF7:0xFA] = bytes([(p20 >> 12) & 0xFF, (p20 >> 4) & 0xFF, (p20 & 0x0F) << 4])
        self.regs[0xFA:0xFD] = bytes([(t20 >> 12) & 0xFF, (t20 >> 4) & 0xFF, (t20 & 0x0F) << 4])
        self.regs[0xFD:0xFF] = bytes([(h16 >> 8) & 0xFF, h16 & 0xFF])

    def outputs_state(self) -> dict:
        mode = {0: "sleep", 1: "forced", 2: "forced", 3: "normal"}[self.regs[0xF4] & 3]
        return {"mode": mode, "measurements": self._count}


# --- 1-Wire / single-wire sensors (accessed through shims) ----------------------------------------
def _crc8_maxim(data: bytes) -> int:
    """Dallas/Maxim 1-Wire CRC-8 (poly x^8+x^5+x^4+1, reflected 0x8C).  # src: Maxim AN27"""
    crc = 0
    for byte in data:
        for _ in range(8):
            mix = (crc ^ byte) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return crc


@register
class DS18B20(Device):
    """DS18B20 1-Wire thermometer. Read via the ``w1thermsensor`` shim or the virtual sysfs file
    ``/sys/bus/w1/devices/28-…/w1_slave`` (runner). 12-bit = 0.0625 °C steps.
    # src: Maxim DS18B20 datasheet — range −55…+125 °C, scratchpad layout, 0.0625 °C @ 12 bit
    """

    type = "ds18b20"
    label = "DS18B20 thermometer"
    pin_roles = ("pin",)
    pin_aliases = {"dq": "pin", "data": "pin", "sig": "pin", "s": "pin", "out": "pin"}
    defaults = {"resolution": 12}
    inputs = {"temperature": PropSpec("float", -55.0, 125.0, unit="°C", default=21.0, label="Temperature",
                                      widget="slider")}
    outputs = {"rom": PropSpec("text", default="", label="ROM id")}
    example = {"pins": {"pin": 4}}

    def setup(self) -> None:
        rom = self.params.get("rom")
        if not rom:
            rom = "28-" + hashlib.sha1(self.id.encode()).hexdigest()[:12]
        self.rom = str(rom)

    def raw(self) -> int:
        """Temperature register value (1/16 °C units, truncated to the configured resolution)."""
        bits = int(self.params.get("resolution", 12))
        raw = int(round(float(self._inputs["temperature"]) * 16))
        return raw & ~((1 << (12 - bits)) - 1) if 9 <= bits <= 12 else raw

    def millicelsius(self) -> int:
        """Value as reported by the Linux w1_therm driver (``t=`` field)."""
        return int(round(self.raw() * 1000 / 16))

    def w1_slave(self) -> str:
        """Contents of ``w1_slave`` exactly as the Linux w1_therm driver prints it."""
        raw = self.raw() & 0xFFFF
        cfg = {9: 0x1F, 10: 0x3F, 11: 0x5F, 12: 0x7F}.get(int(self.params.get("resolution", 12)), 0x7F)
        pad = bytes([raw & 0xFF, raw >> 8, 0x4B, 0x46, cfg, 0xFF, 0x0C, 0x10])
        pad += bytes([_crc8_maxim(pad)])
        hexs = " ".join(f"{b:02x}" for b in pad)
        return f"{hexs} : crc={pad[-1]:02x} YES\n{hexs} t={self.millicelsius()}\n"

    def outputs_state(self) -> dict:
        return {"rom": self.rom}


@register
class DHT22(Device):
    """DHT22/AM2302 humidity + temperature sensor (read via ``adafruit_dht`` / ``Adafruit_DHT`` shims).
    # src: Aosong AM2302 datasheet — −40…80 °C, 0…100 %RH, 0.1 resolution, ≥2 s between reads
    """

    type = "dht22"
    label = "DHT22 humidity sensor"
    pin_roles = ("pin",)
    pin_aliases = {"data": "pin", "sig": "pin", "s": "pin", "out": "pin", "dout": "pin"}
    inputs = {"temperature": PropSpec("float", -40.0, 80.0, unit="°C", default=21.0, label="Temperature",
                                      widget="slider"),
              "humidity": PropSpec("float", 0.0, 100.0, unit="%RH", default=50.0, label="Humidity",
                                   widget="slider")}
    outputs = {"reads": PropSpec("int", 0, None, default=0, label="Reads")}
    example = {"pins": {"pin": 4}}

    def setup(self) -> None:
        self.reads = 0

    def measure(self) -> tuple[float, float]:
        """(temperature °C, humidity %RH) rounded to the sensor's 0.1 resolution."""
        with self.pi.lock:
            self.reads += 1
            return (round(float(self._inputs["temperature"]), 1), round(float(self._inputs["humidity"]), 1))

    def outputs_state(self) -> dict:
        return {"reads": self.reads}


# --- HX711 -------------------------------------------------------------------------------------
@register
class HX711(Device):
    """HX711 24-bit load-cell ADC, bit-banged on DOUT/PD_SCK exactly like the datasheet.

    counts = offset + weight·scale (channel A gain 128; gain 64 halves, channel B reads offset/4),
    two's complement, clipped to 24 bits. DOUT is HIGH until a conversion is ready; each PD_SCK
    rising edge shifts one bit out MSB first; pulses 25/26/27 select A128/B32/A64 for the next
    conversion. PD_SCK held HIGH > 60 µs outside a read powers the chip down (re-settles on wake).
    Long HIGH phases *during* a read are tolerated (Python bit-banging jitter): pulses 1–24 always,
    gain pulses 25–27 up to ``GAIN_PULSE_JITTER_S`` (longer = a deliberate power-down).
    # src: Avia HX711 datasheet — 10/80 SPS, settling 400/50 ms, Fig./Table "PD_SCK pulses and gain"
    """

    type = "hx711"
    label = "HX711 load cell amplifier"
    pin_roles = ("dout", "sck")
    pin_aliases = {"dt": "dout", "data": "dout", "do": "dout", "clk": "sck", "pd_sck": "sck", "pdsck": "sck"}
    defaults = {"scale": 420.0, "offset": 8000, "rate": 10}
    inputs = {"weight": PropSpec("float", -50000.0, 200000.0, unit="g", default=0.0, label="Load",
                                 widget="slider")}
    outputs = {"counts": PropSpec("int", default=0, label="Last conversion"),
               "gain": PropSpec("int", default=128, label="Gain"),
               "powered": PropSpec("bool", default=True, label="Powered")}
    example = {"pins": {"dout": 5, "sck": 6}}
    POWER_DOWN_S = 60e-6         # src: Avia HX711 datasheet, "PD_SCK high > 60 µs → power down"
    GAIN_PULSE_JITTER_S = 0.02   # twin tolerance for Python scheduling jitter (not a chip parameter)

    def setup(self) -> None:
        self._gen = 0
        self._gain = 128
        self._pulses = 0
        self._value = 0
        self._ready = False
        self._powered = True
        self._rise_t: float | None = None
        self._last = 0
        self.pi.drive(self.pins["dout"], self.id, 1)
        self._schedule_ready(self._settle())
        self.listen(self.pins["sck"], self._on_sck)

    def _rate(self) -> float:
        return 80.0 if float(self.params.get("rate", 10)) >= 80 else 10.0

    def _settle(self) -> float:
        return 0.05 if self._rate() >= 80 else 0.4

    def _schedule_ready(self, delay: float, t: float | None = None) -> None:
        self._gen += 1
        gen = self._gen
        at = (self.pi.clock.now() if t is None else t) + delay

        def ready() -> None:
            if self._gen == gen and self._powered:
                self._ready = True
                self._pulses = 0
                self.pi.drive(self.pins["dout"], self.id, 0, t=at)
        self.pi.call_at(at, ready)

    def counts(self) -> int:
        """Conversion result for the current load and gain."""
        p = self.params
        base = float(p.get("offset", 0)) + float(self._inputs["weight"]) * float(p.get("scale", 420.0))
        val = {128: base, 64: base / 2.0, 32: float(p.get("offset", 0)) / 4.0}[self._gain]
        return max(-(1 << 23), min((1 << 23) - 1, int(round(val))))

    def _reading(self, high: float = 0.0) -> bool:
        """True while PD_SCK pulses still belong to a read (data bits 1–24, gain pulses 25–27)."""
        if 0 < self._pulses < 25:
            return True
        return 25 <= self._pulses <= 27 and high <= self.GAIN_PULSE_JITTER_S

    def _power_down(self, t: float) -> None:
        self._powered = False
        self._ready = False
        self._gen += 1                                         # cancel a pending "ready"
        self.pi.drive(self.pins["dout"], self.id, 1, t=t)

    def _on_sck(self, bcm: int, level: int, t: float) -> None:
        dout = self.pins["dout"]
        if level:
            self._rise_t = t
            if not self._powered:
                return
            if self._ready and self._pulses == 0:
                self._last = self.counts()
                self._value = self._last & 0xFFFFFF
            if self._ready or self._pulses > 0:
                self._pulses += 1
                if self._pulses <= 24:
                    self.pi.drive(dout, self.id, (self._value >> (24 - self._pulses)) & 1, t=t)
                elif self._pulses == 25:
                    self._ready = False
                    self.pi.drive(dout, self.id, 1, t=t)
            return
        high = 0.0 if self._rise_t is None else t - self._rise_t
        self._rise_t = None
        if not self._powered or (high > self.POWER_DOWN_S and not self._reading(high)):
            # PD_SCK was held high > 60 µs: chip powered down; falling edge wakes it (gain resets)
            self._powered = True
            self._gain = 128
            self._pulses = 0
            self._ready = False
            self.pi.drive(dout, self.id, 1, t=t)
            self._schedule_ready(self._settle(), t)
            return
        if self._pulses >= 25:
            self._gain = {25: 128, 26: 32, 27: 64}.get(min(self._pulses, 27), 128)
            self._schedule_ready(1.0 / self._rate(), t)

    def tick(self, t: float, dt: float) -> None:
        if (self._powered and self._rise_t is not None and t - self._rise_t > self.POWER_DOWN_S
                and not self._reading(t - self._rise_t) and self.pi.effective(self.pins["sck"]) >= 0.5):
            self._power_down(self._rise_t + self.POWER_DOWN_S)

    def outputs_state(self) -> dict:
        return {"counts": self._last, "gain": self._gain, "powered": self._powered}

    def on_input(self, prop: str, value: Any) -> None:
        pass
