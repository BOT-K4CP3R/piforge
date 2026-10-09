"""Power budget: rails, PSU, Pi consumption, 3V3 limit.

Numbers: Raspberry Pi docs "Typical power requirements" (Pi 4: 3.0 A PSU, 600 mA typical bare board),
MG996R datasheet (stall 2.5 A @ 6 V), HC-SR04 (15 mA), BME280 (714 µA peak).
"""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError, ValidationError
from piforge.core.report import Severity
from piforge.elec.model import Circuit
from piforge.elec.power import PowerBudget, RailBudget, power_budget


def _pi4():
    c = Circuit("p")
    return c, c.add("rpi4b")


def _servo(c, pi, key="mg996r_servo", gpio="GPIO12"):
    s = c.add(key)
    c.connect(pi["5V"], s["V+"])
    c.connect(pi["GND"], s["GND"])
    c.connect(pi[gpio], s["SIG"])
    return s


def test_power_budget():
    c, pi = _pi4()
    s1 = _servo(c, pi, gpio="GPIO12")
    s2 = _servo(c, pi, gpio="GPIO13")
    pb = power_budget(c, psu="psu_usbc_5v3a")
    assert isinstance(pb, PowerBudget) and pb.psu == "psu_usbc_5v3a"
    rail = pb.rails["5V"]
    assert isinstance(rail, RailBudget) and rail.voltage == pytest.approx(5.0, abs=0.2)
    assert rail.available_ma == pytest.approx(3000 - 600)       # PSU minus typical Pi 4 draw
    assert rail.max_ma == pytest.approx(2 * 2500)                # two stalled MG996R
    assert {ref for ref, _, _ in rail.loads} >= {s1.ref, s2.ref}
    hit = [f for f in pb.report if f.code == "POWER.RAIL_OVERLOAD"]
    assert hit and hit[0].severity == Severity.ERROR and "5V" in hit[0].subject
    # three small sensors are fine
    c2, pi2 = _pi4()
    bme = c2.add("bme280_breakout")
    c2.connect(pi2["3V3"], bme["VIN"])
    c2.connect(pi2["GND"], bme["GND"])
    for key in ("hcsr04", "pir_hcsr501"):
        s = c2.add(key)
        c2.connect(pi2["5V"], s["VCC"])
        c2.connect(pi2["GND"], s["GND"])
    pb2 = power_budget(c2, psu="psu_usbc_5v3a")
    assert pb2.report.ok and not pb2.report.has("POWER.RAIL_MARGIN")
    assert pb2.rails["5V"].typ_ma == pytest.approx(15 + 0.05, abs=0.5)
    assert pb2.rails["3V3"].available_ma > 0 and pb2.rails["3V3"].max_ma < 1.0


def test_rail_margin_warning():
    c, pi = _pi4()
    strip = c.add("ws2812b_strip", count=34)  # 34 * 60 mA = 2040 mA = 85 % of 2400 mA
    c.connect(pi["5V"], strip["5V"])
    c.connect(pi["GND"], strip["GND"])
    pb = power_budget(c, psu="psu_usbc_5v3a")
    assert pb.rails["5V"].max_ma == pytest.approx(34 * 60)
    assert pb.report.has("POWER.RAIL_MARGIN", "warning") and pb.report.ok


def test_no_psu_info_and_psu_part():
    c, pi = _pi4()
    pb = power_budget(c)
    assert pb.psu is None and pb.report.has("POWER.NO_PSU", "info")
    assert pb.rails["5V"].available_ma == pytest.approx(3000 - 600)  # assumes the recommended PSU
    c.add("psu_usbc_5v5a")  # a PSU part in the circuit (plugged into the Pi's USB-C)
    pb2 = power_budget(c)
    assert pb2.psu == "psu_usbc_5v5a" and not pb2.report.has("POWER.NO_PSU")
    assert pb2.rails["5V"].available_ma == pytest.approx(5000 - 600)


def test_pi_load_modes():
    c, pi = _pi4()
    typ = power_budget(c, psu="psu_usbc_5v3a").rails["5V"].available_ma
    mx = power_budget(c, psu="psu_usbc_5v3a", pi_load="max").rails["5V"].available_ma
    idle = power_budget(c, psu="psu_usbc_5v3a", pi_load="idle").rails["5V"].available_ma
    assert idle > typ > mx
    with pytest.raises(ValidationError):
        power_budget(c, pi_load="turbo")
    with pytest.raises(NotFoundError):
        power_budget(c, psu="psu_nuclear")


def test_3v3_rail_limit():
    c, pi = _pi4()
    strip = c.add("ws2812b_strip", count=10)  # 600 mA max on the 3V3 header pins (budget 500 mA)
    c.connect(pi["3V3"], strip["5V"])
    c.connect(pi["GND"], strip["GND"])
    pb = power_budget(c, psu="psu_usbc_5v3a")
    assert pb.rails["3V3"].max_ma == pytest.approx(600)
    assert any(f.code == "POWER.RAIL_OVERLOAD" and "3V3" in f.subject for f in pb.report.errors)
    # the 3V3 regulator input shows up as a load on the 5 V rail
    assert any("3V3" in ref for ref, _, _ in pb.rails["5V"].loads)


def test_separate_motor_rail_with_driver():
    c, pi = _pi4()
    psu = c.add("psu_dc_12v2a")
    drv = c.add("l298n_module")
    m1, m2 = c.add("dc_motor"), c.add("dc_motor")
    c.connect(psu["V+"], drv["12V"])
    c.connect(psu["GND"], drv["GND"], pi["GND"])
    c.connect(drv["OUT1"], m1["M+"])
    c.connect(drv["OUT2"], m1["M-"])
    c.connect(drv["OUT3"], m2["M+"])
    c.connect(drv["OUT4"], m2["M-"])
    pb = power_budget(c, psu="psu_usbc_5v3a")
    rail = pb.rails["12V"]
    assert rail.available_ma == pytest.approx(2000)
    refs = {ref for ref, _, _ in rail.loads}
    assert {"M1", "M2", drv.ref} <= refs  # motor current flows through the driver's VS pin


def test_regulator_rail():
    c, pi = _pi4()
    psu = c.add("psu_dc_12v2a")
    reg = c.add("lm2596_module", vout=5.0)
    c.connect(psu["V+"], reg["IN+"])
    c.connect(psu["GND"], reg["IN-"], pi["GND"])
    s = c.add("mg996r_servo")
    c.connect(reg["OUT+"], s["V+"])
    c.connect(reg["OUT-"], s["GND"])
    c.connect(pi["GPIO18"], s["SIG"])
    pb = power_budget(c, psu="psu_usbc_5v3a")
    out = next(r for r in pb.rails.values() if any(ref == s.ref for ref, _, _ in r.loads))
    assert out.voltage == pytest.approx(5.0) and out.available_ma == pytest.approx(3000)
    inp = pb.rails["12V"]
    # 2.5 A * 5 V / (0.8 * 12 V) = 1.30 A drawn from the 12 V adapter at servo stall
    assert any(ref == reg.ref for ref, _, _ in inp.loads)
    assert inp.max_ma == pytest.approx(2500 * 5.0 / (0.8 * 12.0), rel=0.02)


def test_pi5_with_3a_supply_is_flagged():
    c = Circuit("p5")
    c.add("rpi5")
    pb = power_budget(c, psu="psu_usbc_5v3a")
    hit = [f for f in pb.report if f.code == "POWER.PSU_RATING"]
    assert hit and "600 mA" in hit[0].message  # USB limited to 600 mA with a 3 A PSU (docs)
    assert pb.rails["5V"].available_ma == pytest.approx(3000 - 800)
    md = pb.to_markdown()
    assert md.startswith("| Rail |") and "5V" in md


def test_board_rails_named_by_pin_type_not_user_net_names():
    c, pi = _pi4()
    fan = c.add("fan_5v", "M2")
    oled = c.add("ssd1306_096_i2c", "U3")
    c.connect(pi["5V"], fan["+"], name="FAN_5V")
    c.connect(fan["-"], pi["GND"])
    c.connect(pi["3V3"], oled["VCC"], name="LOGIC")
    c.connect(pi["GND"], oled["GND"])
    pb = power_budget(c, psu="psu_usbc_5v3a")
    assert set(pb.rails) >= {"5V", "3V3"} and "FAN_5V" not in pb.rails and "LOGIC" not in pb.rails
    assert pb.rails["5V"].net == "FAN_5V" and pb.rails["3V3"].net == "LOGIC"  # user name kept as alias
    assert any(ref == fan.ref for ref, _, _ in pb.rails["5V"].loads)
