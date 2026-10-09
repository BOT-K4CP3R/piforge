"""Fix round 1 regressions (review findings C1, I1-I5, M1, M3, M6), built from the reviewer's probe circuits.

Numbers: 5 V * 3.3k / (1k + 3.3k) = 3.84 V; 5 V * 2.7k / 3.7k = 3.65 V; LCD backpack 4.7 kΩ to 5 V against the
Pi's 1.8 kΩ to 3.3 V: 3.3 + (5 - 3.3) * 1.8 / 6.5 = 3.77 V; Pi 5 GPIO abs max 3.8 V, VDD + 0.3 V = 3.6 V.
"""

from __future__ import annotations

import gc
import weakref

import pytest

from piforge.core.report import Severity
from piforge.elec.bootconfig import boot_config
from piforge.elec.erc import gpio_loads, run_erc
from piforge.elec.model import Circuit, CircuitError


def sev_of(rep, code: str, subject_part: str = "") -> set[str]:
    return {f.severity.name for f in rep if f.code == code and subject_part in f.subject}


def _pi(board: str = "rpi4b"):
    c = Circuit("fix1")
    return c, c.add(board)


# ---------------------------------------------------------------------------------- C1 common ground
@pytest.mark.parametrize("common", [True, False])
def test_servo_on_external_supply_needs_common_ground(common):
    c, pi = _pi()
    psu, reg, sv = c.add("psu_dc_12v2a"), c.add("lm2596_module", vout=5.0), c.add("mg996r_servo")
    c.connect(psu["V+"], reg["IN+"])
    c.connect(psu["GND"], reg["IN-"])
    c.connect(reg["OUT+"], sv["V+"])
    c.connect(reg["OUT-"], sv["GND"])
    c.connect(pi["GPIO18"], sv["SIG"])
    if common:
        c.connect(pi["GND"], psu["GND"])
    rep = run_erc(c)
    if common:
        assert not rep.has("ERC.NO_COMMON_GROUND"), rep.to_markdown()
    else:
        hits = [f for f in rep if f.code == "ERC.NO_COMMON_GROUND"]
        assert hits and all(f.severity == Severity.ERROR for f in hits), rep.to_markdown()
        assert "GPIO18" in hits[0].message


def test_l298n_on_adapter_without_common_ground():
    c, pi = _pi()
    drv, psu, m = c.add("l298n_module"), c.add("psu_dc_12v2a"), c.add("dc_motor")
    c.connect(psu["V+"], drv["12V"])
    c.connect(psu["GND"], drv["GND"])
    c.connect(pi["GPIO5"], drv["IN1"])
    c.connect(pi["GPIO6"], drv["IN2"])
    c.connect(drv["OUT1"], m["M+"])
    c.connect(drv["OUT2"], m["M-"])
    assert run_erc(c).has("ERC.NO_COMMON_GROUND", "error")
    c.connect(pi["GND"], psu["GND"])
    assert not run_erc(c).has("ERC.NO_COMMON_GROUND")


@pytest.mark.parametrize("pi_gnd", [True, False])
def test_module_grounds_only_tied_to_each_other(pi_gnd):
    c, pi = _pi()
    b, o = c.add("bme280_breakout"), c.add("ssd1306_096_i2c")
    c.connect(pi["3V3"], b["VIN"], o["VCC"])
    c.connect(b["GND"], o["GND"])
    if pi_gnd:
        c.connect(pi["GND"], b["GND"])
    c.connect(pi["SDA1"], b["SDA"], o["SDA"])
    c.connect(pi["SCL1"], b["SCL"], o["SCL"])
    rep = run_erc(c)
    gnd = [f for f in rep if f.code == "ERC.UNCONNECTED" and f.severity == Severity.ERROR and "ground" in f.message]
    if pi_gnd:
        assert not gnd and not rep.errors, rep.to_markdown()
    else:
        assert gnd, rep.to_markdown()


def test_relay_contacts_on_separate_supply_are_isolated():
    """Relay contacts are galvanically isolated: a 12 V load on its own ground is fine."""
    c, pi = _pi()
    k, psu, m, d = c.add("relay_1ch_5v"), c.add("psu_dc_12v2a"), c.add("dc_motor"), c.add("diode_1n4007")
    c.connect(pi["5V"], k["VCC"])
    c.connect(pi["GND"], k["GND"])
    c.connect(pi["GPIO17"], k["IN"])
    c.connect(psu["V+"], k["COM"])
    c.connect(k["NO"], m["M+"], d["K"])
    c.connect(m["M-"], psu["GND"], d["A"])
    rep = run_erc(c)
    assert not rep.has("ERC.NO_COMMON_GROUND"), rep.to_markdown()


def test_npn_low_side_switch_on_foreign_supply_needs_common_ground():
    c, pi = _pi()
    psu, q, rb, m, d = (c.add("psu_dc_12v2a"), c.add("npn_2n2222"), c.add("resistor", value=1000),
                        c.add("dc_motor"), c.add("diode_1n4007"))
    c.connect(psu["V+"], m["M+"], d["K"])
    c.connect(m["M-"], q["C"], d["A"])
    c.connect(q["E"], pi["GND"])
    c.connect(pi["GPIO17"], rb["1"])
    c.connect(rb["2"], q["B"])
    assert run_erc(c).has("ERC.NO_COMMON_GROUND", "error")
    c.connect(psu["GND"], pi["GND"])
    assert not run_erc(c).has("ERC.NO_COMMON_GROUND")


# ---------------------------------------------------------------------------------- I1 over-voltage tiers
def _lcd(board):
    c, pi = _pi(board)
    lcd = c.add("lcd1602_i2c")
    c.connect(pi["5V"], lcd["VCC"])
    c.connect(pi["GND"], lcd["GND"])
    c.connect(pi["SDA1"], lcd["SDA"])
    c.connect(pi["SCL1"], lcd["SCL"])
    return run_erc(c)


def test_lcd_backpack_5v_pullups_pi5_warning_pi4_error():
    assert sev_of(_lcd("rpi5"), "ERC.LEVEL_MISMATCH", "GPIO2") == {"WARNING"}
    assert sev_of(_lcd("rpi4b"), "ERC.LEVEL_MISMATCH", "GPIO2") == {"ERROR"}


@pytest.mark.parametrize("board,bottom,expected", [
    ("rpi5", 3300, {"ERROR"}),     # 3.84 V >= 3.8 V abs max
    ("rpi5", 2700, {"WARNING"}),   # 3.65 V: above VDD + 0.3 V, below abs max
    ("rpi5", 2000, set()),         # 3.33 V
    ("rpi4b", 3300, {"ERROR"}),
    ("rpi4b", 2700, {"ERROR"}),    # 3.65 V >= 3.6 V abs max
    ("rpi4b", 2000, set()),
])
def test_echo_divider_tiers(board, bottom, expected):
    c, pi = _pi(board)
    us = c.add("hcsr04")
    c.connect(pi["5V"], us["VCC"])
    c.connect(pi["GND"], us["GND"])
    c.connect(pi["GPIO23"], us["TRIG"])
    r1, r2 = c.add("resistor", value=1000), c.add("resistor", value=bottom)
    c.connect(us["ECHO"], r1["1"])
    c.connect(r1["2"], r2["1"], pi["GPIO24"])
    c.connect(r2["2"], pi["GND"])
    assert sev_of(run_erc(c), "ERC.LEVEL_MISMATCH", "GPIO24") == expected


# ---------------------------------------------------------------------------------- I2 LEDs / pot
@pytest.mark.parametrize("sw", ["nmos_ao3400", "npn_2n2222"])
@pytest.mark.parametrize("ohms,code", [(None, "ERC.LED_NO_RESISTOR"), (47, "ERC.LED_OVERCURRENT")])
def test_led_switched_by_transistor(sw, ohms, code):
    c, pi = _pi()
    led, q = c.add("led"), c.add(sw)
    if ohms:
        r = c.add("resistor", value=ohms)
        c.connect(pi["5V"], r["1"])
        c.connect(r["2"], led["A"])
    else:
        c.connect(pi["5V"], led["A"])
    ctrl, out, com, rg = ("G", "D", "S", 100) if sw.startswith("nmos") else ("B", "C", "E", 1000)
    g = c.add("resistor", value=rg)
    c.connect(led["K"], q[out])
    c.connect(q[com], pi["GND"])
    c.connect(pi["GPIO18"], g["1"])
    c.connect(g["2"], q[ctrl])
    rep = run_erc(c)
    assert rep.has(code, "error"), rep.to_markdown()


def test_led_through_transistor_with_proper_resistor_is_clean():
    c, pi = _pi()
    led, q, r, g = c.add("led"), c.add("nmos_ao3400"), c.add("resistor", value=330), c.add("resistor", value=100)
    c.connect(pi["5V"], r["1"])
    c.connect(r["2"], led["A"])
    c.connect(led["K"], q["D"])
    c.connect(q["S"], pi["GND"])
    c.connect(pi["GPIO18"], g["1"])
    c.connect(g["2"], q["G"])
    rep = run_erc(c)
    assert not rep.errors, rep.to_markdown()


def test_led_between_two_gpios_without_resistor():
    c, pi = _pi()
    led = c.add("led")
    c.connect(pi["GPIO17"], led["A"])
    c.connect(led["K"], pi["GPIO27"])
    assert run_erc(c).has("ERC.LED_NO_RESISTOR", "error")


def test_antiparallel_leds_between_gpios_with_resistor():
    c, pi = _pi()
    r, l1, l2 = c.add("resistor", value=220), c.add("led"), c.add("led")
    c.connect(pi["GPIO17"], r["1"])
    c.connect(r["2"], l1["A"], l2["K"])
    c.connect(l1["K"], l2["A"], pi["GPIO27"])
    rep = run_erc(c)
    assert not rep.errors, rep.to_markdown()
    # one GPIO high, the other low: (3.3 - 2.0) / (220 + 1) = 5.9 mA
    assert gpio_loads(c)["GPIO17"] == pytest.approx(1.3 / 221 * 1000, rel=0.02)


@pytest.mark.parametrize("rail,bad", [("5V", True), ("3V3", False)])
def test_pot_checked_at_both_ends(rail, bad):
    c, pi = _pi()
    adc, pot = c.add("mcp3008"), c.add("potentiometer", position=0.5)
    c.connect(pi["3V3"], adc["VDD"], adc["VREF"])
    c.connect(pi["GND"], adc["DGND"], adc["AGND"])
    c.connect(pi["GPIO11"], adc["CLK"])
    c.connect(pi["GPIO10"], adc["DIN"])
    c.connect(pi["GPIO9"], adc["DOUT"])
    c.connect(pi["GPIO8"], adc["CS"])
    c.connect(pi[rail], pot["CW"])
    c.connect(pot["CCW"], pi["GND"])
    c.connect(pot["W"], adc["CH0"])
    rep = run_erc(c)
    assert rep.has("ERC.LEVEL_MISMATCH", "error") is bad, rep.to_markdown()


# ---------------------------------------------------------------------------------- I3 cache
def test_param_mutation_invalidates_analysis(pi4, wire_led):
    c, pi = pi4
    r, _ = wire_led(c, pi, "GPIO17", 330)
    assert not run_erc(c).errors
    r.params["value"] = 47.0
    assert run_erc(c).has("ERC.GPIO_OVERCURRENT", "error")
    assert gpio_loads(c)["GPIO17"] > 20


def test_analysed_circuit_is_garbage_collected(wire_led):
    refs = []
    for _ in range(5):
        c = Circuit("tmp")
        pi = c.add("rpi4b")
        wire_led(c, pi, "GPIO17", 330)
        run_erc(c)
        refs.append(weakref.ref(c))
    del c, pi
    gc.collect()
    assert all(r() is None for r in refs)


# ---------------------------------------------------------------------------------- I4 SPI CE1
def _mcp_on_ce1():
    c, pi = _pi()
    adc = c.add("mcp3008")
    c.connect(pi["3V3"], adc["VDD"], adc["VREF"])
    c.connect(pi["GND"], adc["DGND"], adc["AGND"])
    c.connect(pi["GPIO11"], adc["CLK"])
    c.connect(pi["GPIO10"], adc["DIN"])
    c.connect(pi["GPIO9"], adc["DOUT"])
    c.connect(pi["GPIO7"], adc["CS"])
    return c, pi


def test_single_spi_device_on_ce1_keeps_spidev0_1():
    c, _ = _mcp_on_ce1()
    lines = boot_config(c).splitlines()
    assert "dtparam=spi=on" in lines
    assert not any(ln.startswith("dtoverlay=spi0-1cs") for ln in lines), lines


def test_single_spi_device_on_ce1_with_gpio8_in_use():
    c, pi = _mcp_on_ce1()
    btn = c.add("pushbutton")
    c.connect(pi["GPIO8"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    c.configure(pi, pulls={"GPIO8": "up"})
    lines = boot_config(c).splitlines()
    assert "dtoverlay=spi0-1cs,cs0_pin=7" in lines, lines
    assert "dtoverlay=spi0-1cs" not in lines
    assert not run_erc(c).has("ERC.INTERFACE_PIN_CONFLICT", "error")


# ---------------------------------------------------------------------------------- I5 net names
def test_naming_a_net_gnd_joins_the_ground_net():
    c, pi = _pi()
    r, led = c.add("resistor", value=330), c.add("led")
    c.connect(pi["GPIO17"], r["1"])
    c.connect(r["2"], led["A"])
    c.connect(pi["GND"], c.add("bme280_breakout")["GND"])
    net = c.connect(led["K"], name="GND")
    assert [n.name for n in c.nets].count("GND") == 1
    assert c.net("GND") is net
    assert gpio_loads(c)["GPIO17"] == pytest.approx(1.3 / 331 * 1000, rel=0.01)


def test_naming_a_net_5v_joins_the_5v_rail():
    c, pi = _pi()
    us = c.add("hcsr04")
    c.connect(pi["5V"], us["VCC"])
    c.connect(us["GND"], name="GND")
    c.connect(pi["GND"], name="GND")
    assert [n.name for n in c.nets].count("GND") == 1
    assert c.net_of(us["GND"]) is c.net_of(pi[6])


# ---------------------------------------------------------------------------------- M1 params
def test_unknown_parameter_raises_with_suggestion():
    c = Circuit("p")
    with pytest.raises(CircuitError) as exc:
        c.add("led", colour="blue")
    assert "color" in str(exc.value)
    with pytest.raises(CircuitError):
        c.add("resistor", value=330, ohms=10)


def test_library_params_are_not_shared():
    c = Circuit("p")
    a, b = c.add("ssd1306_096_i2c"), c.add("ssd1306_096_i2c")
    a.params["pullups"]["SDA"] = (1000.0, "VCC")
    assert b.params["pullups"]["SDA"] != (1000.0, "VCC")
    l1, l2 = c.add("led", color="blue"), c.add("led", color="blue")
    assert l1.params is not l2.params


# ---------------------------------------------------------------------------------- M3 pull direction
@pytest.mark.parametrize("to,pull,bad", [("GND", "down", True), ("GND", "up", False),
                                         ("3V3", "up", True), ("3V3", "down", False)])
def test_pull_in_wrong_direction(to, pull, bad):
    c, pi = _pi()
    b = c.add("pushbutton")
    c.connect(pi["GPIO27"], b["A"])
    c.connect(b["B"], pi[to])
    c.configure(pi, pulls={"GPIO27": pull})
    rep = run_erc(c)
    assert rep.has("ERC.PULL_DIRECTION", "warning") is bad, rep.to_markdown()


# ---------------------------------------------------------------------------------- M6 wiring colours
def _lum(hexcol: str) -> float:
    hexcol = {"white": "#ffffff"}.get(hexcol, hexcol)
    ch = [int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_pin_number_label_contrast_and_lime_vs_black():
    from piforge.elec.wiring_draw import HEX, label_color

    for name, fc in HEX.items():
        assert _contrast(fc, label_color(fc)) >= 3.5, name
    assert label_color(HEX["lime"]) != "white"
    assert _contrast(HEX["lime"], HEX["black"]) >= 2.0


# ---------------------------------------------------------------------------------- fix round 2
def _lab(hexcol: str):
    ch = [int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
    xyz = [(0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883]
    f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def test_signal_wire_colours_pairwise_distinguishable():
    from piforge.elec.wiring import ROLE_COLORS, SIGNAL_PALETTE
    from piforge.elec.wiring_draw import HEX

    used = sorted(set(ROLE_COLORS.values()) | set(SIGNAL_PALETTE))
    assert len(set(SIGNAL_PALETTE)) == len(SIGNAL_PALETTE)
    assert HEX["black"] != HEX["lime"]
    for i, a in enumerate(used):
        for b in used[i + 1:]:
            la, lb = _lab(HEX[a]), _lab(HEX[b])
            de = sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5
            assert de >= 20 or _contrast(HEX[a], HEX[b]) >= 2.0, (a, b, de)
    # supply colours stay distinct from every signal colour
    for s in used:
        for sup in ("black", "red", "orange"):
            if s != sup:
                la, lb = _lab(HEX[s]), _lab(HEX[sup])
                assert sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5 >= 20 or _contrast(HEX[s], HEX[sup]) >= 2.0, (s, sup)


@pytest.mark.parametrize("gnd_first", [True, False])
def test_power_named_net_is_one_net_in_either_order(gnd_first):
    c, pi = _pi()
    r, led = c.add("resistor", value=330), c.add("led")
    c.connect(pi["GPIO17"], r["1"])
    c.connect(r["2"], led["A"])
    other = c.add("bme280_breakout")
    if gnd_first:
        c.connect(pi["GND"], other["GND"])
        c.connect(led["K"], name="GND")
    else:
        c.connect(led["K"], name="GND")
        c.connect(pi["GND"], other["GND"])
    names = [n.name for n in c.nets]
    assert len(names) == len(set(names)), names
    assert names.count("GND") == 1
    assert c.net_of(led["K"]) is c.net_of(pi[6]) is c.net_of(other["GND"])
    assert gpio_loads(c)["GPIO17"] == pytest.approx(1.3 / 331 * 1000, rel=0.01)
    from piforge.elec.kicad import kicad_netlist
    import re
    nets = re.findall(r'\(net \(code "?\d+"?\) \(name "([^"]*)"', kicad_netlist(c))
    assert nets and len(nets) == len(set(nets)), nets


@pytest.mark.parametrize("pull,no,nc,bad", [("up", "GND", "3V3", False), ("down", "GND", "3V3", False),
                                            ("up", "GND", None, False), ("up", "3V3", None, True)])
def test_changeover_switch_pull_is_irrelevant(pull, no, nc, bad):
    c, pi = _pi()
    sw = c.add("limit_switch")
    c.connect(pi["GPIO27"], sw["COM"])
    c.connect(sw["NO"], pi[no])
    if nc:
        c.connect(sw["NC"], pi[nc])
    c.configure(pi, pulls={"GPIO27": pull})
    rep = run_erc(c)
    assert rep.has("ERC.PULL_DIRECTION", "warning") is bad, rep.to_markdown()
