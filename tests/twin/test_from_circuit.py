"""``twin_config_from_circuit``: auto-wiring twin devices from an electronics netlist.

Two layers:

* ``test_from_circuit_duck_typed`` uses tiny stand-ins that follow the Task 4 interface
  (``Circuit.parts/nets/net_of/config``, ``Part.ref/definition/params/pins()/[name]``, ``PinRef``,
  ``PartDef.sim["twin"]``) so the mapping logic is always tested;
* ``test_from_circuit`` runs against the real ``piforge.elec`` model and library and is skipped
  until that package provides them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from piforge.twin.from_circuit import twin_config_from_circuit


# --- minimal stand-ins for piforge.elec.model -------------------------------------------------
@dataclass(frozen=True)
class FPin:
    name: str
    number: str
    type: str = "passive"
    functions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class FDef:
    key: str
    name: str
    category: str
    pins: tuple[FPin, ...]
    i2c_addresses: tuple[int, ...] = ()
    params: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)


class FPart:
    def __init__(self, ref: str, definition: FDef, **params) -> None:
        self.ref, self.definition, self.params = ref, definition, dict(params)

    def pins(self) -> tuple[FPin, ...]:
        return self.definition.pins

    def __getitem__(self, name: str) -> "FRef":
        for p in self.definition.pins:
            if name in (p.name, p.number) or name in p.functions:
                return FRef(self, p)
        raise KeyError(name)


@dataclass(frozen=True)
class FRef:
    part: FPart
    pin: FPin


@dataclass
class FNet:
    name: str
    refs: list


class FCircuit:
    def __init__(self) -> None:
        self.parts: list[FPart] = []
        self.nets: list[FNet] = []
        self._cfg: dict = {}

    def add(self, definition: FDef, ref: str, **params) -> FPart:
        p = FPart(ref, definition, **params)
        self.parts.append(p)
        return p

    def connect(self, *refs: FRef, name: str | None = None) -> FNet:
        merged = [n for n in self.nets if any(r in n.refs for r in refs)]
        net = FNet(name or (merged[0].name if merged else f"N${len(self.nets) + 1}"), [])
        for n in merged:
            net.refs.extend(n.refs)
            self.nets.remove(n)
        net.refs.extend(r for r in refs if r not in net.refs)
        self.nets.append(net)
        return net

    def net_of(self, ref: FRef):
        return next((n for n in self.nets if ref in n.refs), None)

    def configure(self, part: FPart, *, pulls=None, interfaces=None) -> None:
        self._cfg[part.ref] = {"pulls": dict(pulls or {}), "interfaces": dict(interfaces or {})}

    def config(self, part: FPart) -> dict:
        return self._cfg.get(part.ref, {"pulls": {}, "interfaces": {}})


def _pi_def() -> FDef:
    # Physical header positions for the pins used here.  # src: raspberrypi.com GPIO pinout
    gpio = {2: "3", 3: "5", 4: "7", 17: "11", 27: "13", 23: "16", 24: "18", 8: "24", 9: "21",
            10: "19", 11: "23"}
    extra = {2: ("I2C1_SDA",), 3: ("I2C1_SCL",), 8: ("SPI0_CE0",), 9: ("SPI0_MISO",),
             10: ("SPI0_MOSI",), 11: ("SPI0_SCLK",)}
    pins = [FPin(f"GPIO{b}", n, "bidir", (f"GPIO{b}",) + extra.get(b, ())) for b, n in gpio.items()]
    pins += [FPin("GND", "6", "gnd"), FPin("GND", "9", "gnd"), FPin("3V3", "1", "power_out"),
             FPin("5V", "2", "power_out")]
    return FDef("rpi4b", "Raspberry Pi 4B", "board", tuple(pins))


BUTTON = FDef("pushbutton", "Tact switch", "input", (FPin("1", "1"), FPin("2", "2")), sim={"twin": "button"})
RES = FDef("resistor", "Resistor", "passive", (FPin("1", "1"), FPin("2", "2")), params={"value": 330})
LED = FDef("led", "LED", "led", (FPin("A", "1"), FPin("K", "2")), params={"color": "red"}, sim={"twin": "led"})
BME = FDef("bme280_breakout", "BME280", "sensor",
           (FPin("VIN", "1", "power_in"), FPin("GND", "2", "gnd"), FPin("SCL", "3"), FPin("SDA", "4")),
           i2c_addresses=(0x76, 0x77), sim={"twin": "bme280"})
SR04 = FDef("hcsr04", "HC-SR04", "sensor",
            (FPin("VCC", "1", "power_in"), FPin("TRIG", "2", "input"), FPin("ECHO", "3", "output"),
             FPin("GND", "4", "gnd")), sim={"twin": "hcsr04"})
ADC = FDef("mcp3008", "MCP3008", "adc",
           (FPin("CLK", "13"), FPin("DOUT", "12"), FPin("DIN", "11"), FPin("CS", "10"),
            FPin("VDD", "16", "power_in"), FPin("DGND", "9", "gnd")), sim={"twin": "mcp3008"})


def _demo_circuit() -> FCircuit:
    c = FCircuit()
    pi = c.add(_pi_def(), "U1")
    sw = c.add(BUTTON, "SW1")
    led = c.add(LED, "D1")
    r1 = c.add(RES, "R1")
    bme = c.add(BME, "U2")
    c.connect(pi["GPIO27"], sw["1"])
    c.connect(sw["2"], pi["GND"], name="GND")
    c.configure(pi, pulls={"GPIO27": "up"})
    c.connect(pi["GPIO17"], r1["1"])
    c.connect(r1["2"], led["A"])
    c.connect(led["K"], pi["GND"])
    c.connect(pi["3V3"], bme["VIN"], name="3V3")
    c.connect(bme["GND"], pi["GND"])
    c.connect(pi["GPIO2"], bme["SDA"], name="SDA")
    c.connect(pi["GPIO3"], bme["SCL"], name="SCL")
    return c


def test_from_circuit_duck_typed():
    cfg = twin_config_from_circuit(_demo_circuit())
    assert cfg.board == "rpi4b"
    devs = {d.id: d for d in cfg.devices}
    assert set(devs) == {"SW1", "D1", "U2"}
    assert devs["SW1"].type == "button" and devs["SW1"].pins == {"pin": 27}
    assert devs["SW1"].params.get("active_low") is True
    assert devs["D1"].type == "led" and devs["D1"].pins == {"pin": 17}
    assert devs["D1"].params.get("active_high") is True
    assert devs["U2"].type == "bme280"
    assert devs["U2"].bus == {"kind": "i2c", "bus": 1, "address": 0x76}


def test_from_circuit_divider_spi_and_address_param():
    c = _demo_circuit()
    pi = c.parts[0]
    c.parts[4].params["i2c_address"] = 0x77                    # BME280 strapped to 0x77
    us = c.add(SR04, "US1")
    ra, rb = c.add(RES, "R2", value=1000), c.add(RES, "R3", value=2000)
    c.connect(pi["GPIO23"], us["TRIG"])
    c.connect(us["ECHO"], ra["1"])
    c.connect(ra["2"], rb["1"], pi["GPIO24"])                 # 1k/2k divider into GPIO24
    c.connect(rb["2"], pi["GND"])
    adc = c.add(ADC, "U3")
    c.connect(pi["GPIO11"], adc["CLK"])
    c.connect(pi["GPIO9"], adc["DOUT"])
    c.connect(pi["GPIO10"], adc["DIN"])
    c.connect(pi["GPIO8"], adc["CS"])
    cfg = twin_config_from_circuit(c)
    devs = {d.id: d for d in cfg.devices}
    assert devs["U2"].bus["address"] == 0x77
    assert devs["US1"].pins == {"trigger": 23, "echo": 24}
    assert devs["U3"].bus == {"kind": "spi", "bus": 0, "cs": 0}
    # the resulting config must build a working twin
    from piforge.twin.runtime import Twin

    Twin(cfg)


def test_from_circuit_physical_pullup_becomes_config_pull():
    c = _demo_circuit()
    pi = c.parts[0]
    rp = c.add(RES, "R9", value=10000)
    c.connect(rp["1"], pi["GPIO27"])
    c.connect(rp["2"], pi["3V3"])
    cfg = twin_config_from_circuit(c)
    assert cfg.pulls == {27: "up"}


def test_from_circuit():
    """Against the real Task 4 model + part library (skipped until ``piforge.elec`` provides them)."""
    model = pytest.importorskip("piforge.elec.model")
    if not hasattr(model, "Circuit"):
        pytest.skip("piforge.elec.model.Circuit not available yet")
    c = model.Circuit("twin-test")
    try:
        pi = c.add("rpi4b", "U1")
        sw = c.add("pushbutton", "SW1")
        led = c.add("led", "D1")
        r1 = c.add("resistor", "R1", value=330)
        bme = c.add("bme280_breakout", "U2")
    except Exception as exc:  # library key missing or different
        pytest.skip(f"elec library not ready: {exc}")

    def pin_named(part, *names):
        for name in names:                       # by name only: pin numbers ("1"/"2") are ambiguous
            for p in part.pins():
                if p.name.upper() == name:
                    return part[p.name]
        pytest.skip(f"{part.ref} has none of {names}: {[p.name for p in part.pins()]}")

    sw_pins = list(sw.pins())
    c.connect(pi["GPIO27"], sw[sw_pins[0].name])
    c.connect(sw[sw_pins[1].name], pi["GND"])
    c.configure(pi, pulls={"GPIO27": "up"})
    r_pins = list(r1.pins())
    c.connect(pi["GPIO17"], r1[r_pins[0].name])
    c.connect(r1[r_pins[1].name], pin_named(led, "A", "ANODE", "+", "1"))
    c.connect(pin_named(led, "K", "CATHODE", "-", "2"), pi["GND"])
    c.connect(pi["3V3"], pin_named(bme, "VIN", "VCC", "3V3", "VDD"))
    c.connect(pin_named(bme, "GND"), pi["GND"])
    c.connect(pi["SDA1"], pin_named(bme, "SDA", "SDI"))
    c.connect(pi["GPIO3"], pin_named(bme, "SCL", "SCK"))
    cfg = twin_config_from_circuit(c)
    devs = {d.id: d for d in cfg.devices}
    assert devs["SW1"].type == "button" and devs["SW1"].pins == {"pin": 27}
    assert devs["SW1"].params.get("active_low") is True
    assert devs["D1"].type == "led" and devs["D1"].pins == {"pin": 17}
    assert devs["U2"].type == "bme280"
    assert devs["U2"].bus == {"kind": "i2c", "bus": 1, "address": 0x76}


def test_from_circuit_real_drivers_and_buses():
    """Task 4 ``sim["pins"]`` + ``passthrough``: steppers via ULN2003, DC motor via L298N (with ENA
    PWM), HC-SR04 echo through a divider, MCP3008 on SPI0, KY-040 on-board pull-ups, CSI camera."""
    model = pytest.importorskip("piforge.elec.model")
    if not hasattr(model, "Circuit"):
        pytest.skip("piforge.elec.model.Circuit not available yet")
    c = model.Circuit("bench")
    try:
        pi = c.add("rpi4b")
        uln, step = c.add("uln2003_board"), c.add("stepper_28byj48", "M1")
        l298, mot = c.add("l298n_module"), c.add("dc_motor", "M2")
        us = c.add("hcsr04", "US1")
        ra, rb = c.add("resistor", value=1000), c.add("resistor", value=2000)
        adc = c.add("mcp3008", "ADC1")
        enc = c.add("rotary_encoder_ky040", "ENC1")
        cam = c.add("pi_camera_v3", "CAM1")  # noqa: F841 - CSI, no header pins
        for i, g in zip(range(1, 5), (5, 6, 13, 19)):
            c.connect(pi[f"GPIO{g}"], uln[f"IN{i}"])
        for x in "ABCD":
            c.connect(uln[x], step[x])
        c.connect(pi["GPIO20"], l298["IN1"])
        c.connect(pi["GPIO21"], l298["IN2"])
        c.connect(pi["GPIO12"], l298["ENA"])
        c.connect(l298["OUT1"], mot["M+"])
        c.connect(l298["OUT2"], mot["M-"])
        c.connect(pi["GPIO23"], us["TRIG"])
        c.connect(us["ECHO"], ra["1"])
        c.connect(ra["2"], rb["1"], pi["GPIO24"])
        c.connect(rb["2"], pi["GND"])
        for a, b in (("GPIO11", "CLK"), ("GPIO10", "DIN"), ("GPIO9", "DOUT"), ("GPIO8", "CS")):
            c.connect(pi[a], adc[b])
        c.connect(pi["GPIO16"], enc["CLK"])
        c.connect(pi["GPIO26"], enc["DT"])
        c.connect(pi["3V3"], enc["+"])
    except Exception as exc:  # library keys / pin names changed
        pytest.skip(f"elec library differs from the expected Task 4 contract: {exc}")
    cfg = twin_config_from_circuit(c)
    devs = {d.id: d for d in cfg.devices}
    assert devs["M1"].pins == {"in1": 5, "in2": 6, "in3": 13, "in4": 19}
    assert devs["M2"].pins == {"in1": 20, "in2": 21, "pwm": 12}
    assert devs["US1"].pins == {"trigger": 23, "echo": 24}
    assert devs["ADC1"].bus == {"kind": "spi", "bus": 0, "cs": 0} and devs["ADC1"].pins == {}
    assert devs["ENC1"].pins == {"a": 16, "b": 26}
    assert devs["CAM1"].type == "camera"
    assert cfg.pulls.get(16) == "up" and cfg.pulls.get(26) == "up"      # KY-040 on-board 10 kΩ
    from piforge.twin.runtime import Twin

    Twin(cfg)                                                           # builds a working twin
