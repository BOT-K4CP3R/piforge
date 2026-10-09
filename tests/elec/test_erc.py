"""Electrical rule check: every code has at least one positive and one negative test.

Physics validation (Ohm's law, Thevenin divider) cites the numbers in the brief / datasheets:
LED current (3.3 V - 2.0 V) / 330 Ω = 3.9 mA; 5 V · 2k / (1k + 2k) = 3.33 V.
"""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError
from piforge.core.report import Report, Severity
from piforge.elec.erc import RULES, gpio_loads, run_erc
from piforge.elec.model import Circuit

ALL_CODES = """
ERC.UNCONNECTED ERC.SUPPLY_VOLTAGE ERC.LEVEL_MISMATCH ERC.LEVEL_LOW_DRIVE ERC.OUTPUT_CONFLICT
ERC.RAIL_SHORT ERC.I2C_ADDRESS_CONFLICT ERC.I2C_PULLUPS ERC.GPIO_OVERCURRENT ERC.GPIO_TOTAL_CURRENT
ERC.LED_NO_RESISTOR ERC.LED_OVERCURRENT ERC.INDUCTIVE_NO_FLYBACK ERC.MOSFET_GATE_DRIVE
ERC.FLOATING_INPUT ERC.RESERVED_PIN ERC.INTERFACE_PIN_CONFLICT ERC.MOTOR_ON_GPIO
""".split()


def codes(rep: Report, sev: str | None = None) -> set[str]:
    return {f.code for f in rep if sev is None or f.severity == Severity.parse(sev)}


def power(c, pi, part, vcc="VCC", gnd="GND", rail="5V"):
    c.connect(pi[rail], part[vcc])
    c.connect(pi["GND"], part[gnd])


# ---------------------------------------------------------------------------------- general
def test_rules_registry_and_filter(pi4, wire_led):
    c, pi = pi4
    assert set(ALL_CODES) <= set(RULES)
    wire_led(c, pi, "GPIO17", None)  # LED without resistor
    rep = run_erc(c)
    assert isinstance(rep, Report) and rep.title == "ERC"
    only = run_erc(c, rules=["ERC.FLOATING_INPUT"])
    assert "ERC.LED_NO_RESISTOR" not in codes(only)
    assert "ERC.LED_NO_RESISTOR" in codes(run_erc(c, rules=["led_no_resistor"]))
    with pytest.raises(NotFoundError):
        run_erc(c, rules=["ERC.NOPE"])


def test_clean_circuit_has_no_errors_or_warnings(pi4, wire_led):
    c, pi = pi4
    wire_led(c, pi, "GPIO17", 330)
    btn = c.add("pushbutton")
    c.connect(pi["GPIO27"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    c.configure(pi, pulls={"GPIO27": "up"})
    bme = c.add("bme280_breakout")
    power(c, pi, bme, "VIN", rail="3V3")
    c.connect(pi["SDA1"], bme["SDA"])
    c.connect(pi["SCL1"], bme["SCL"])
    rep = run_erc(c)
    assert rep.ok and not rep.warnings, rep.to_markdown()


# ---------------------------------------------------------------------------------- UNCONNECTED
def test_unconnected(pi4):
    c, pi = pi4
    us = c.add("hcsr04")
    c.connect(pi["5V"], us["VCC"])
    rep = run_erc(c)
    hits = [f for f in rep if f.code == "ERC.UNCONNECTED"]
    assert any(f.severity == Severity.ERROR and "GND" in f.subject for f in hits)
    c.connect(pi["GND"], us["GND"])
    c.connect(pi["GPIO23"], us["TRIG"])
    bs = c.add("bss138_level_shifter")  # proper level shifting keeps the rest clean
    c.connect(pi["3V3"], bs["LV"])
    c.connect(pi["5V"], bs["HV"])
    c.connect(pi["GND"], bs["GND"])
    c.connect(us["ECHO"], bs["HV1"])
    c.connect(bs["LV1"], pi["GPIO24"])
    assert "ERC.UNCONNECTED" not in codes(run_erc(c), "error")


# ---------------------------------------------------------------------------------- SUPPLY_VOLTAGE
def test_supply_voltage(pi4):
    c, pi = pi4
    bme = c.add("bme280_breakout")
    power(c, pi, bme, "VIN", rail="5V")  # 3.6 V max part on 5 V
    rep = run_erc(c, rules=["ERC.SUPPLY_VOLTAGE"])
    assert rep.has("ERC.SUPPLY_VOLTAGE", "error")
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    bme2 = c2.add("bme280_breakout")
    power(c2, pi2, bme2, "VIN", rail="3V3")
    assert not run_erc(c2, rules=["ERC.SUPPLY_VOLTAGE"]).findings


def test_supply_undervoltage_is_warning(pi4):
    c, pi = pi4
    us = c.add("hcsr04")
    power(c, pi, us, rail="3V3")  # HC-SR04 wants 5 V
    rep = run_erc(c, rules=["ERC.SUPPLY_VOLTAGE"])
    assert rep.has("ERC.SUPPLY_VOLTAGE", "warning") and not rep.has("ERC.SUPPLY_VOLTAGE", "error")


# ---------------------------------------------------------------------------------- LEVEL_MISMATCH
def _hcsr04(c, pi, *, divider: tuple[float, float] | None):
    us = c.add("hcsr04")
    power(c, pi, us)
    c.connect(pi["GPIO23"], us["TRIG"])
    if divider is None:
        c.connect(us["ECHO"], pi["GPIO24"])
    else:
        r1 = c.add("resistor", value=divider[0])
        r2 = c.add("resistor", value=divider[1])
        c.connect(us["ECHO"], r1["1"])
        c.connect(r1["2"], r2["1"], pi["GPIO24"])
        c.connect(r2["2"], pi["GND"])
    return us


def test_level_mismatch_5v_echo(pi4):
    c, pi = pi4
    _hcsr04(c, pi, divider=None)
    rep = run_erc(c)
    hits = [f for f in rep if f.code == "ERC.LEVEL_MISMATCH" and f.severity == Severity.ERROR]
    assert hits, rep.to_markdown()
    assert hits[0].data["volts"] == pytest.approx(5.0, abs=0.01)
    # 1k/2k divider: Thevenin 5 V * 2/3 = 3.33 V on GPIO24 -> no error
    c2 = Circuit("div")
    pi2 = c2.add("rpi4b")
    _hcsr04(c2, pi2, divider=(1000, 2000))
    rep2 = run_erc(c2)
    assert not rep2.has("ERC.LEVEL_MISMATCH", "error"), rep2.to_markdown()
    # a too-weak divider (1k/4.7k -> 4.12 V) is still caught
    c3 = Circuit("bad div")
    pi3 = c3.add("rpi4b")
    _hcsr04(c3, pi3, divider=(1000, 4700))
    hits3 = [f for f in run_erc(c3) if f.code == "ERC.LEVEL_MISMATCH"]
    assert hits3 and hits3[0].data["volts"] == pytest.approx(5 * 4700 / 5700, rel=1e-3)


def test_level_mismatch_spi_adc_on_5v(pi4):
    c, pi = pi4
    adc = c.add("mcp3008")
    for p in ("VDD", "VREF"):
        c.connect(pi["5V"], adc[p])
    for p in ("DGND", "AGND"):
        c.connect(pi["GND"], adc[p])
    c.connect(pi["SPI0_SCLK"], adc["CLK"])
    c.connect(pi["SPI0_MOSI"], adc["DIN"])
    c.connect(pi["SPI0_MISO"], adc["DOUT"])
    c.connect(pi["SPI0_CE0"], adc["CS"])
    rep = run_erc(c)
    assert any(f.code == "ERC.LEVEL_MISMATCH" and "GPIO9" in f.subject for f in rep.errors)


def test_level_mismatch_i2c_pullups_to_5v(pi4):
    c, pi = pi4
    lcd = c.add("lcd1602_i2c")  # backpack pulls SDA/SCL up to its 5 V VCC
    power(c, pi, lcd)
    c.connect(pi["SDA1"], lcd["SDA"])
    c.connect(pi["SCL1"], lcd["SCL"])
    rep = run_erc(c)
    assert rep.has("ERC.LEVEL_MISMATCH", "error"), rep.to_markdown()
    # with the backpack pull-ups removed the Pi side is safe (but PCF8574 VIH at 5 V is marginal)
    c2 = Circuit("lcd")
    pi2 = c2.add("rpi4b")
    lcd2 = c2.add("lcd1602_i2c", pullup_ohms=None)
    power(c2, pi2, lcd2)
    c2.connect(pi2["SDA1"], lcd2["SDA"])
    c2.connect(pi2["SCL1"], lcd2["SCL"])
    rep2 = run_erc(c2)
    assert not rep2.has("ERC.LEVEL_MISMATCH", "error")
    assert rep2.has("ERC.LEVEL_LOW_DRIVE", "warning")


# ---------------------------------------------------------------------------------- LEVEL_LOW_DRIVE
def test_level_low_drive(pi4):
    c, pi = pi4
    strip = c.add("ws2812b_strip", count=8)
    power(c, pi, strip, "5V")
    c.connect(pi["GPIO18"], strip["DIN"])  # 3.3 V into VIH = 0.7 * 5 V = 3.5 V
    rep = run_erc(c)
    hit = [f for f in rep if f.code == "ERC.LEVEL_LOW_DRIVE"]
    assert hit and hit[0].severity == Severity.WARNING and hit[0].data["vih"] == pytest.approx(3.5)
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    drv = c2.add("l298n_module")  # L298 VIH 2.3 V: fine from 3.3 V
    c2.connect(pi2["GND"], drv["GND"])
    c2.connect(pi2["GPIO5"], drv["IN1"])
    assert "ERC.LEVEL_LOW_DRIVE" not in codes(run_erc(c2))


# ---------------------------------------------------------------------------------- OUTPUT_CONFLICT
def test_output_conflict(pi4):
    c, pi = pi4
    a, b = c.add("pir_hcsr501"), c.add("pir_hcsr501")
    for pir in (a, b):
        power(c, pi, pir)
    c.connect(a["OUT"], b["OUT"], pi["GPIO17"])
    assert run_erc(c).has("ERC.OUTPUT_CONFLICT", "error")
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    p = c2.add("pir_hcsr501")
    power(c2, pi2, p)
    c2.connect(p["OUT"], pi2["GPIO17"])
    assert "ERC.OUTPUT_CONFLICT" not in codes(run_erc(c2))


def test_output_shorted_to_rail_and_gpio_on_rail(pi4):
    c, pi = pi4
    p = c.add("pir_hcsr501")
    power(c, pi, p)
    c.connect(p["OUT"], pi["GND"])
    c.connect(pi["GPIO22"], pi["3V3"])
    rep = run_erc(c, rules=["ERC.OUTPUT_CONFLICT"])
    assert rep.has("ERC.OUTPUT_CONFLICT", "error")      # module output into GND
    assert rep.has("ERC.OUTPUT_CONFLICT", "warning")    # GPIO hard-wired to a rail


# ---------------------------------------------------------------------------------- RAIL_SHORT
def test_rail_short(pi4):
    c, pi = pi4
    c.connect(pi["5V"], pi["GND"])
    assert run_erc(c).has("ERC.RAIL_SHORT", "error")
    c2 = Circuit("3v3-5v")
    pi2 = c2.add("rpi4b")
    c2.connect(pi2["5V"], pi2["3V3"])
    assert run_erc(c2).has("ERC.RAIL_SHORT", "error")
    c3 = Circuit("ok")
    pi3 = c3.add("rpi4b")
    us = c3.add("hcsr04")
    power(c3, pi3, us)
    assert "ERC.RAIL_SHORT" not in codes(run_erc(c3))


# ---------------------------------------------------------------------------------- I2C
def _bme(c, pi, **params):
    s = c.add("bme280_breakout", **params)
    power(c, pi, s, "VIN", rail="3V3")
    c.connect(pi["SDA1"], s["SDA"])
    c.connect(pi["SCL1"], s["SCL"])
    return s


def test_i2c_conflict(pi4):
    c, pi = pi4
    _bme(c, pi)
    _bme(c, pi)
    rep = run_erc(c)
    hit = [f for f in rep if f.code == "ERC.I2C_ADDRESS_CONFLICT"]
    assert hit and hit[0].severity == Severity.ERROR and "0x76" in hit[0].message
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    _bme(c2, pi2)
    _bme(c2, pi2, i2c_address=0x77)
    assert "ERC.I2C_ADDRESS_CONFLICT" not in codes(run_erc(c2))


def test_i2c_pullups(pi4):
    c, pi = pi4
    _bme(c, pi, pullup_ohms=None)  # module without pull-ups: only the Pi's 1.8 kΩ
    hit = [f for f in run_erc(c) if f.code == "ERC.I2C_PULLUPS"]
    assert hit and hit[0].severity == Severity.INFO and "1.8" in hit[0].message
    # module pull-ups present and not too strong -> nothing to say
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    _bme(c2, pi2)
    assert "ERC.I2C_PULLUPS" not in codes(run_erc(c2))
    # four 4.7 kΩ modules + the Pi's 1.8 kΩ -> ~660 Ω, 5 mA sink -> warning
    c3 = Circuit("strong")
    pi3 = c3.add("rpi4b")
    for addr in (0x3C, 0x3D):
        o = c3.add("ssd1306_096_i2c", i2c_address=addr)
        power(c3, pi3, o, rail="3V3")
        c3.connect(pi3["SDA1"], o["SDA"])
        c3.connect(pi3["SCL1"], o["SCL"])
    for addr in (0x48, 0x49):
        a = c3.add("ads1115_breakout", i2c_address=addr, pullup_ohms=4700)
        power(c3, pi3, a, "VDD", rail="3V3")
        c3.connect(pi3["SDA1"], a["SDA"])
        c3.connect(pi3["SCL1"], a["SCL"])
    assert run_erc(c3).has("ERC.I2C_PULLUPS", "warning")


def test_i2c_without_any_pullup_on_soft_bus(pi4):
    c, pi = pi4
    s = c.add("bme280_breakout", pullup_ohms=None)
    power(c, pi, s, "VIN", rail="3V3")
    c.connect(pi["GPIO23"], s["SDA"])  # software I2C on plain GPIOs: no fixed pull-ups
    c.connect(pi["GPIO24"], s["SCL"])
    assert run_erc(c).has("ERC.I2C_PULLUPS", "warning")


# ---------------------------------------------------------------------------------- LED / GPIO current
def test_led_resistor_sizing(pi4, wire_led):
    c, pi = pi4
    wire_led(c, pi, "GPIO17", 330)
    rep = run_erc(c)
    assert rep.ok, rep.to_markdown()
    assert gpio_loads(c)["GPIO17"] == pytest.approx((3.3 - 2.0) / 330 * 1000, rel=0.02)  # 3.9 mA
    c2 = Circuit("47R")
    pi2 = c2.add("rpi4b")
    wire_led(c2, pi2, "GPIO17", 47)  # 27.7 mA
    rep2 = run_erc(c2)
    hit = [f for f in rep2 if f.code == "ERC.GPIO_OVERCURRENT"]
    assert hit and hit[0].severity == Severity.ERROR and hit[0].data["ma"] == pytest.approx(27.7, rel=0.05)
    c3 = Circuit("no R")
    pi3 = c3.add("rpi4b")
    wire_led(c3, pi3, "GPIO17", None)
    rep3 = run_erc(c3)
    assert rep3.has("ERC.LED_NO_RESISTOR", "error")


def test_led_on_rail_without_resistor(pi4, wire_led):
    c, pi = pi4
    wire_led(c, pi, None, None, supply="5V")
    assert run_erc(c).has("ERC.LED_NO_RESISTOR", "error")


def test_led_overcurrent(pi4, wire_led):
    c, pi = pi4
    wire_led(c, pi, None, 47, supply="5V")  # (5 - 2) / 47 = 64 mA
    hit = [f for f in run_erc(c) if f.code == "ERC.LED_OVERCURRENT"]
    assert hit and hit[0].severity == Severity.ERROR and hit[0].data["ma"] == pytest.approx(63, rel=0.05)
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    wire_led(c2, pi2, None, 330, supply="5V")  # 9 mA
    assert "ERC.LED_OVERCURRENT" not in codes(run_erc(c2))


def test_gpio_total_current(pi4, wire_led):
    c, pi = pi4
    for g in ("GPIO17", "GPIO27", "GPIO22", "GPIO23", "GPIO24"):
        wire_led(c, pi, g, 100)  # 13 mA each, 65 mA total
    rep = run_erc(c)
    assert rep.has("ERC.GPIO_TOTAL_CURRENT", "warning")
    assert not rep.has("ERC.GPIO_OVERCURRENT")
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    for g in ("GPIO17", "GPIO27"):
        wire_led(c2, pi2, g, 100)
    assert "ERC.GPIO_TOTAL_CURRENT" not in codes(run_erc(c2))


def test_npn_base_without_resistor_overloads_gpio(pi4):
    c, pi = pi4
    q = c.add("npn_2n2222")
    c.connect(pi["GPIO17"], q["B"])
    c.connect(q["E"], pi["GND"])
    assert run_erc(c).has("ERC.GPIO_OVERCURRENT", "error")


# ---------------------------------------------------------------------------------- inductive loads
def _relay_with_npn(c, pi, *, flyback: str | None):
    k = c.add("relay_srd05vdc")
    q = c.add("npn_2n2222")
    rb = c.add("resistor", value=1000)
    c.connect(pi["5V"], k["COIL1"])
    c.connect(k["COIL2"], q["C"])
    c.connect(q["E"], pi["GND"])
    c.connect(pi["GPIO17"], rb["1"])
    c.connect(rb["2"], q["B"])
    if flyback == "ok":
        d = c.add("diode_1n4007")
        c.connect(d["K"], k["COIL1"])
        c.connect(d["A"], k["COIL2"])
    elif flyback == "reversed":
        d = c.add("diode_1n4007")
        c.connect(d["A"], k["COIL1"])
        c.connect(d["K"], k["COIL2"])
    return k


def test_relay_without_flyback(pi4):
    c, pi = pi4
    _relay_with_npn(c, pi, flyback=None)
    rep = run_erc(c)
    assert rep.has("ERC.INDUCTIVE_NO_FLYBACK", "warning")
    assert not rep.has("ERC.GPIO_OVERCURRENT")  # 1 kΩ base resistor: (3.3 - 0.7) / 1k = 2.6 mA
    c2 = Circuit("diode")
    pi2 = c2.add("rpi4b")
    _relay_with_npn(c2, pi2, flyback="ok")
    assert "ERC.INDUCTIVE_NO_FLYBACK" not in codes(run_erc(c2))
    c3 = Circuit("module")
    pi3 = c3.add("rpi4b")
    m = c3.add("relay_1ch_5v")  # driver transistor + flyback diode on the module
    power(c3, pi3, m)
    c3.connect(pi3["GPIO17"], m["IN"])
    rep3 = run_erc(c3)
    assert "ERC.INDUCTIVE_NO_FLYBACK" not in codes(rep3)
    assert rep3.ok, rep3.to_markdown()


def test_reversed_flyback_diode_is_error(pi4):
    c, pi = pi4
    _relay_with_npn(c, pi, flyback="reversed")
    assert run_erc(c).has("ERC.INDUCTIVE_NO_FLYBACK", "error")


# ---------------------------------------------------------------------------------- MOSFET gate
def _mosfet(c, pi, key, *, gate_resistor=None):
    m = c.add(key)
    motor = c.add("dc_motor")
    d = c.add("diode_1n4007")
    c.connect(pi["5V"], motor["M+"], d["K"])
    c.connect(motor["M-"], m["D"], d["A"])
    c.connect(m["S"], pi["GND"])
    if gate_resistor:
        r = c.add("resistor", value=gate_resistor)
        c.connect(pi["GPIO18"], r["1"])
        c.connect(r["2"], m["G"])
    else:
        c.connect(pi["GPIO18"], m["G"])
    return m


def test_mosfet_gate(pi4):
    c, pi = pi4
    _mosfet(c, pi, "nmos_irf540n")
    rep = run_erc(c)
    hit = [f for f in rep if f.code == "ERC.MOSFET_GATE_DRIVE"]
    assert hit and hit[0].severity == Severity.WARNING
    c2 = Circuit("ao3400")
    pi2 = c2.add("rpi4b")
    _mosfet(c2, pi2, "nmos_ao3400", gate_resistor=100)
    rep2 = run_erc(c2)
    assert "ERC.MOSFET_GATE_DRIVE" not in codes(rep2)
    assert "ERC.INDUCTIVE_NO_FLYBACK" not in codes(rep2)


# ---------------------------------------------------------------------------------- FLOATING_INPUT
def test_floating_button(pi4):
    c, pi = pi4
    btn = c.add("pushbutton")
    c.connect(pi["GPIO27"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    rep = run_erc(c)
    hit = [f for f in rep if f.code == "ERC.FLOATING_INPUT"]
    assert hit and hit[0].severity == Severity.WARNING and "GPIO27" in hit[0].subject
    c.configure(pi, pulls={"GPIO27": "up"})
    assert "ERC.FLOATING_INPUT" not in codes(run_erc(c))


def test_external_pullup_and_open_collector(pi4):
    c, pi = pi4
    rx = c.add("ir_breakbeam_rx")  # open-collector output needs a pull-up
    power(c, pi, rx, rail="3V3")
    c.connect(rx["OUT"], pi["GPIO22"])
    assert "ERC.FLOATING_INPUT" in codes(run_erc(c))
    r = c.add("resistor", value=10_000)
    c.connect(pi["3V3"], r["1"])
    c.connect(r["2"], pi["GPIO22"])
    assert "ERC.FLOATING_INPUT" not in codes(run_erc(c))


# ---------------------------------------------------------------------------------- RESERVED_PIN
def test_reserved_pin(pi4):
    c, pi = pi4
    btn = c.add("pushbutton")
    c.connect(pi["GPIO0"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    c.configure(pi, pulls={"GPIO0": "up"})
    assert run_erc(c).has("ERC.RESERVED_PIN", "warning")
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    b2 = c2.add("pushbutton")
    c2.connect(pi2["GPIO5"], b2["A"])
    c2.connect(b2["B"], pi2["GND"])
    assert "ERC.RESERVED_PIN" not in codes(run_erc(c2))


# ---------------------------------------------------------------------------------- INTERFACE_PIN_CONFLICT
def test_interface_pin_conflict_shared_pin(pi4):
    c, pi = pi4
    _bme(c, pi)
    btn = c.add("pushbutton")
    c.connect(pi["GPIO2"], btn["A"])  # button on the I2C SDA line
    c.connect(btn["B"], pi["GND"])
    assert run_erc(c).has("ERC.INTERFACE_PIN_CONFLICT", "error")


def test_interface_pin_conflict_swapped_lines(pi4):
    c, pi = pi4
    s = c.add("bme280_breakout")
    power(c, pi, s, "VIN", rail="3V3")
    c.connect(pi["GPIO3"], s["SDA"])  # SDA on the SCL pin
    c.connect(pi["GPIO2"], s["SCL"])
    hit = [f for f in run_erc(c) if f.code == "ERC.INTERFACE_PIN_CONFLICT"]
    assert hit and hit[0].severity == Severity.ERROR
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    _bme(c2, pi2)
    assert "ERC.INTERFACE_PIN_CONFLICT" not in codes(run_erc(c2))


def test_interface_pin_conflict_configured_uart(pi4):
    c, pi = pi4
    c.configure(pi, interfaces={"uart": True})
    btn = c.add("pushbutton")
    c.connect(pi["GPIO14"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    assert run_erc(c).has("ERC.INTERFACE_PIN_CONFLICT", "error")


# ---------------------------------------------------------------------------------- MOTOR_ON_GPIO
def test_motor_on_gpio(pi4):
    c, pi = pi4
    m = c.add("dc_motor")
    c.connect(pi["GPIO17"], m["M+"])
    c.connect(m["M-"], pi["GND"])
    assert run_erc(c).has("ERC.MOTOR_ON_GPIO", "error")
    c2 = Circuit("driver")
    pi2 = c2.add("rpi4b")
    drv = c2.add("l298n_module")
    psu = c2.add("psu_dc_12v2a")
    mot = c2.add("dc_motor")
    c2.connect(psu["V+"], drv["12V"])
    c2.connect(psu["GND"], drv["GND"], pi2["GND"])
    c2.connect(pi2["GPIO5"], drv["IN1"])
    c2.connect(pi2["GPIO6"], drv["IN2"])
    c2.connect(pi2["GPIO12"], drv["ENA"])
    c2.connect(drv["OUT1"], mot["M+"])
    c2.connect(drv["OUT2"], mot["M-"])
    rep2 = run_erc(c2)
    assert "ERC.MOTOR_ON_GPIO" not in codes(rep2)
    assert "ERC.INDUCTIVE_NO_FLYBACK" not in codes(rep2)  # L298N module has clamp diodes
    assert rep2.ok, rep2.to_markdown()


def test_module_powered_from_gpio(pi4):
    c, pi = pi4
    us = c.add("hcsr04")
    c.connect(pi["GPIO5"], us["VCC"])  # "powering" a 15 mA module from a GPIO
    c.connect(pi["GND"], us["GND"])
    rep = run_erc(c)
    assert any(f.code == "ERC.SUPPLY_VOLTAGE" and "GPIO5" in f.message for f in rep.warnings)
    assert gpio_loads(c)["GPIO5"] >= 15.0


def test_report_is_serialisable(pi4, wire_led):
    c, pi = pi4
    wire_led(c, pi, "GPIO17", 47)
    rep = run_erc(c)
    d = rep.to_dict()
    assert d["counts"]["error"] >= 1 and all(f["hint"] for f in d["findings"])
    assert "ERC.GPIO_OVERCURRENT" in rep.to_markdown()


def test_pwm_configuration_checked(pi4):
    c, pi = pi4
    c.configure(pi, interfaces={"pwm": [12, 18]})  # both are PWM0 channel 0 on the BCM2711
    assert run_erc(c, rules=["ERC.INTERFACE_PIN_CONFLICT"]).has("ERC.INTERFACE_PIN_CONFLICT", "error")
    c2 = Circuit("ok")
    pi2 = c2.add("rpi4b")
    c2.configure(pi2, interfaces={"pwm": [12, 13]})
    assert not run_erc(c2, rules=["ERC.INTERFACE_PIN_CONFLICT"]).findings
    c3 = Circuit("soft")
    pi3 = c3.add("rpi4b")
    c3.configure(pi3, interfaces={"pwm": [17]})
    assert run_erc(c3, rules=["ERC.INTERFACE_PIN_CONFLICT"]).has("ERC.INTERFACE_PIN_CONFLICT", "warning")
