"""Circuit model: parts, pin resolution (Review Focus #4), nets, configuration."""

from __future__ import annotations

import json

import pytest

from piforge.core.errors import NotFoundError, PiForgeError
from piforge.elec.model import (
    Circuit,
    CircuitError,
    Net,
    PartDef,
    Pin,
    PinNotFoundError,
    PinRef,
    PinType,
    Supply,
)

GND_PINS = {"6", "9", "14", "20", "25", "30", "34", "39"}


def test_pin_aliases_resolve(pi4):
    c, pi = pi4
    ref = pi["GPIO17"]
    assert isinstance(ref, PinRef)
    assert ref.part is pi and ref.pin.number == "11" and ref.pin.name == "GPIO17"
    for alias in ("gpio17", "BCM17", "bcm17", "pin11", "PIN11", "Pin 11", 11, "11", " GPIO17 ", "GPIO_17"):
        assert pi[alias] == ref, alias
    # alternate-function and header names
    assert pi["SDA1"] == pi["GPIO2"] == pi["I2C1_SDA"] == pi[3]
    assert pi["SCL1"].pin.number == "5"
    assert pi["TXD0"].pin.name == "GPIO14" and pi["UART0_RX"].pin.name == "GPIO15"
    assert pi["SPI0_MOSI"].pin.number == "19"
    assert pi["ID_SD"] == pi["GPIO0"] and pi["ID_SC"].pin.number == "28"
    # power pins: the first free one in physical order, several spellings
    assert pi["3V3"].pin.type == PinType.POWER_OUT and pi["3V3"].pin.number == "1"
    assert pi["3.3V"].pin.number == "1"
    assert pi["5V"].pin.number == "2" and pi["5v"].pin.number == "2"
    assert pi["GND"].pin.type == PinType.GND and pi["gnd"].pin.number == "6"


def test_unknown_pin_suggests(pi4):
    c, pi = pi4
    with pytest.raises(PinNotFoundError) as ei:
        pi["GPIO77"]
    err = ei.value
    assert isinstance(err, NotFoundError) and isinstance(err, LookupError)
    assert err.suggestions, "expected close matches"
    assert any(s in ("GPIO7", "GPIO17", "GPIO27") for s in err.suggestions)
    assert "Did you mean" in str(err)
    with pytest.raises(PinNotFoundError):
        pi[41]
    with pytest.raises(PinNotFoundError):
        pi["pin0"]
    # function names carried by two pins are ambiguous, and the message says so
    with pytest.raises(PinNotFoundError, match="mbiguous"):
        pi["PWM0"]
    # simple parts too
    r = c.add("resistor", value=330)
    with pytest.raises(PinNotFoundError):
        r["3"]


def test_gnd_allocation_distinct(pi4):
    c, pi = pi4
    leds = [c.add("led") for _ in range(3)]
    phys = []
    for led in leds:
        gnd = pi["GND"]
        c.connect(gnd, led["K"])
        phys.append(gnd.pin.number)
    assert len(set(phys)) == 3 and set(phys) <= GND_PINS
    assert phys == ["6", "9", "14"], "deterministic physical order"
    # all Pi GND pins are one electrical net
    net = c.net("GND")
    assert {r.pin.number for r in net.refs if r.part is pi} == {"6", "9", "14"}
    assert all(c.net_of(led["K"]) is net for led in leds)
    # 5V and 3V3 are allocated the same way
    c.connect(pi["5V"], c.add("hcsr04")["VCC"])
    assert pi["5V"].pin.number == "4"
    c.connect(pi["3V3"], c.add("pir_hcsr501")["VCC"])
    assert pi["3V3"].pin.number == "17"


def test_power_pin_reused_when_all_taken(pi4):
    c, pi = pi4
    for _ in range(8):
        c.connect(pi["GND"], c.add("led")["K"])
    # every GND pin is used: resolution still works (deterministically the first one)
    assert pi["GND"].pin.number == "6"


def test_auto_refs_and_params(pi4):
    c, pi = pi4
    assert pi.ref == "U1"
    r1 = c.add("resistor", value=330)
    r2 = c.add("resistor", value="4k7")
    d1 = c.add("led")
    d2 = c.add("diode_1n4007")
    sw = c.add("pushbutton")
    assert (r1.ref, r2.ref, d1.ref, d2.ref, sw.ref) == ("R1", "R2", "D1", "D2", "SW1")
    assert r2.params["value"] == pytest.approx(4700.0)
    assert c.add("capacitor", value="100n").params["value"] == pytest.approx(100e-9)
    assert c.add("resistor", value="10kΩ").params["value"] == pytest.approx(10_000.0)
    named = c.add("resistor", "RX", value=1000)
    assert named.ref == "RX" and c.part("RX") is named
    with pytest.raises(CircuitError):
        c.add("resistor", "RX", value=1000)  # duplicate ref
    with pytest.raises(CircuitError):
        c.add("resistor")  # value is required
    with pytest.raises(CircuitError):
        c.add("resistor", value="lots")
    with pytest.raises(NotFoundError):
        c.add("no_such_part")
    with pytest.raises(NotFoundError):
        c.part("Q99")


def test_led_color_sets_forward_voltage(pi4):
    c, _ = pi4
    assert c.add("led").params["vf"] == pytest.approx(2.0)
    blue = c.add("led", color="blue")
    assert blue.params["vf"] > 2.6
    assert c.add("led", color="blue", vf=2.9).params["vf"] == pytest.approx(2.9)
    with pytest.raises(CircuitError):
        c.add("led", color="ultraviolet-ish")


def test_i2c_address_param_validated(pi4):
    c, _ = pi4
    a = c.add("bme280_breakout")
    assert a.params["i2c_address"] == 0x76
    b = c.add("bme280_breakout", i2c_address=0x77)
    assert b.params["i2c_address"] == 0x77
    with pytest.raises(CircuitError, match="0x76"):
        c.add("bme280_breakout", i2c_address=0x50)


def test_connect_merges_and_names(pi4):
    c, pi = pi4
    r = c.add("resistor", value=330)
    led = c.add("led")
    n1 = c.connect(pi["GPIO17"], r["1"])
    assert isinstance(n1, Net) and n1.name.startswith("N$")
    n2 = c.connect(r["1"], led["A"])
    assert n2 is c.net_of(pi["GPIO17"]) and len(n2.refs) == 3
    assert len([n for n in c.nets if pi["GPIO17"] in n.refs]) == 1
    # power names win over auto names, user names win over power names
    us = c.add("hcsr04")
    assert c.connect(pi["5V"], us["VCC"]).name == "5V"
    assert c.connect(pi["GND"], us["GND"]).name == "GND"
    named = c.connect(us["ECHO"], r["2"], name="ECHO")
    assert named.name == "ECHO" and c.net("ECHO") is named
    # nets with the same user name are the same net (global labels)
    c.connect(led["K"], name="ECHO")
    assert c.net_of(led["K"]) is c.net("ECHO")
    with pytest.raises(NotFoundError):
        c.net("nope")
    assert c.net_of(c.add("led")["A"]) is None


def test_connect_rejects_bad_arguments(pi4):
    c, pi = pi4
    other = Circuit("other")
    foreign = other.add("led")
    with pytest.raises(CircuitError):
        c.connect(pi["GPIO17"], foreign["A"])
    with pytest.raises(CircuitError):
        c.connect()
    with pytest.raises(CircuitError):
        c.connect(pi["GPIO17"], "GPIO18")  # type: ignore[arg-type]
    a, b = c.add("resistor", value=1), c.add("resistor", value=1)
    c.connect(a["1"], name="X")
    c.connect(b["1"], name="Y")
    with pytest.raises(CircuitError):
        c.connect(a["1"], b["1"])  # would merge two differently named nets


def test_configure_pulls_and_interfaces(pi4):
    c, pi = pi4
    # int keys are physical pins (like pi[22]); names are normalised to the pin name
    c.configure(pi, pulls={"GPIO27": "up", 22: "DOWN"}, interfaces={"i2c": True})
    cfg = c.config(pi)
    assert cfg["pulls"] == {"GPIO27": "up", "GPIO25": "down"}
    assert cfg["interfaces"]["i2c1"] is True
    c.configure(pi, pulls={"pin13": "none"})  # pin13 is GPIO27: later calls update
    assert c.config(pi)["pulls"]["GPIO27"] == "none"
    with pytest.raises(CircuitError):
        c.configure(pi, pulls={"GPIO27": "sideways"})
    with pytest.raises(PinNotFoundError):
        c.configure(pi, pulls={"GPIO99": "up"})
    with pytest.raises(CircuitError):
        c.configure(pi, interfaces={"warp_drive": True})
    with pytest.raises(PiForgeError):
        c.configure(pi, pulls={"5V": "up"})  # not a GPIO


def test_to_dict_is_json(pi4, wire_led):
    c, pi = pi4
    wire_led(c, pi, "GPIO17", 330)
    c.configure(pi, pulls={"GPIO27": "up"})
    d = c.to_dict()
    text = json.dumps(d)
    assert d["name"] == "test"
    assert {p["ref"] for p in d["parts"]} == {"U1", "R1", "D1"}
    assert any(n["name"] == "GND" for n in d["nets"])
    assert "U1.GPIO17" in text and "pulls" in text


def test_custom_partdef_and_supply(pi4):
    c, pi = pi4
    d = PartDef(
        key="my_sensor", name="My sensor", category="sensor",
        pins=(Pin("VCC", "1", PinType.POWER_IN), Pin("OUT", "2", PinType.OUTPUT), Pin("GND", "3", PinType.GND)),
        supply=Supply(3.0, 5.5, 1.0, 2.0, pin="VCC"), logic_from="VCC",
    )
    part = c.add(d)
    assert part.ref.startswith("U") and part["OUT"].pin.type == PinType.OUTPUT
    assert part.pins() == d.pins
    assert hash(d) == hash(d)  # usable as dict key


def test_trace_bcm_through_resistor_and_driver(pi4, wire_led):
    c, pi = pi4
    r, led = wire_led(c, pi, "GPIO17", 330)
    assert c.bcm_of(led["A"]) == 17          # through the series resistor
    assert c.bcm_of(led["K"]) is None        # GND is not a GPIO
    drv = c.add("uln2003_board")
    motor = c.add("stepper_28byj48")
    for i, (gpio, out) in enumerate(zip((5, 6, 13, 19), "ABCD"), start=1):
        c.connect(pi[f"GPIO{gpio}"], drv[f"IN{i}"])
        c.connect(drv[out], motor[out])
    assert [c.bcm_of(motor[x]) for x in "ABCD"] == [5, 6, 13, 19]


def test_pi5_header_labels_stay_unambiguous():
    c = Circuit("p5")
    pi = c.add("rpi5")
    assert pi["SDA1"].pin.name == "GPIO2" and pi["SCL1"].pin.name == "GPIO3"  # header labels
    assert pi["TXD0"].pin.name == "GPIO14" and pi["RXD0"].pin.name == "GPIO15"
    # RP1 really offers I2C1 on GPIO2/3 and GPIO10/11: the function name alone is ambiguous
    with pytest.raises(PinNotFoundError, match="mbiguous"):
        pi["I2C1_SDA"]
    with pytest.raises(PinNotFoundError, match="GPIO14.*GPIO18"):  # RP1 PWM0 ch2 is on GPIO14 and GPIO18
        pi["PWM0_2"]
    assert pi[12] == pi["GPIO18"]


def test_trace_relay_coil_through_transistor(pi4):
    c, pi = pi4
    k = c.add("relay_srd05vdc")
    q = c.add("npn_2n2222")
    rb = c.add("resistor", value=1000)
    c.connect(pi["5V"], k["COIL1"])
    c.connect(k["COIL2"], q["C"])
    c.connect(q["E"], pi["GND"])
    c.connect(pi["GPIO22"], rb["1"])
    c.connect(rb["2"], q["B"])
    assert c.bcm_of(k["COIL2"]) == 22   # COIL2 -> Q.C -> (passthrough) Q.B -> Rb -> GPIO22
    assert c.bcm_of(k["COIL1"]) is None  # never trace out of the 5 V rail


def test_configure_validates_interface_pins(pi4):
    c, pi = pi4
    with pytest.raises(CircuitError):
        c.configure(pi, interfaces={"pwm": [40]})
    with pytest.raises(CircuitError):
        c.configure(pi, interfaces={"onewire": "GPIO4"})
    c.configure(pi, interfaces={"onewire": 17, "pwm": 18})
    assert c.config(pi)["interfaces"] == {"onewire": [17], "pwm": [18]}
