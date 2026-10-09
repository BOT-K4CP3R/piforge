"""Money-counter parts: 74HC(T)595, ULN2003A, A3144 Hall switch, 5 V 5 A PSU — ERC and power budget."""

from __future__ import annotations

import pytest

from piforge.elec import Circuit, power_budget, run_erc
from piforge.elec.library import get_def
from piforge.elec.model import PinType

HALLS = (4, 5, 6, 13, 16, 19, 20, 21)


def money_counter(sr_key: str = "sn74hct595", *, com: bool = True, sr_supply: str = "5V") -> Circuit:
    """Pi Zero 2 W + 5 V 5 A PSU, 4 × 595 on SPI0, 8 × ULN2003A + 28BYJ-48, 8 × A3144 on GPIOs."""
    c = Circuit("money_counter")
    pi = c.add("rpizero2w", "U1")
    ps = c.add("psu_dc_5v5a", "PS1")
    c.connect(ps["V+"], pi["5V"])
    c.connect(ps["GND"], pi["GND"])
    srs = [c.add(sr_key, f"U{10 + i}") for i in range(4)]
    c.connect(pi["GPIO10"], srs[0]["SER"])                    # MOSI
    for i, sr in enumerate(srs):
        c.connect(pi["GPIO11"], sr["SRCLK"])                  # SCLK
        c.connect(pi["GPIO8"], sr["RCLK"])                    # CE0 = latch
        c.connect(sr["VCC"], pi[sr_supply], sr["SRCLR"])
        c.connect(sr["GND"], pi["GND"], sr["OE"])
        if i:
            c.connect(srs[i - 1]["QH'"], sr["SER"])
    for k in range(8):
        drv = c.add("uln2003a", f"U{20 + k}")
        m = c.add("stepper_28byj48", f"M{k + 1}")
        c.connect(drv["E"], pi["GND"])
        if com:
            c.connect(drv["COM"], pi["5V"])
        c.connect(m["COM"], pi["5V"])
        sr = srs[k // 2]
        for j, coil in enumerate("ABCD"):
            c.connect(sr["Q" + "ABCDEFGH"[4 * (k % 2) + j]], drv[f"{j + 1}B"])
            c.connect(drv[f"{j + 1}C"], m[coil])
        h = c.add("hall_a3144", f"H{k + 1}")
        c.connect(h["VCC"], pi["5V"])
        c.connect(h["GND"], pi["GND"])
        c.connect(h["OUT"], pi[f"GPIO{HALLS[k]}"])
    c.configure(pi, interfaces={"spi": True}, pulls={f"GPIO{b}": "up" for b in HALLS})
    return c


def _pin(d, name):
    return next(p for p in d.pins if p.name == name)


def _problems(rep):
    return [f for f in rep.findings if f.severity.name in ("ERROR", "WARNING")]


def test_parts_exist_with_datasheet_values():
    hc, hct = get_def("sn74hc595"), get_def("sn74hct595")
    for d in (hc, hct):
        assert d.footprint == "Package_DIP:DIP-16_W7.62mm" and len(d.pins) == 16
        assert {p.name for p in d.pins} >= {"SER", "SRCLK", "RCLK", "OE", "SRCLR", "QH'", "VCC", "GND",
                                            *(f"Q{x}" for x in "ABCDEFGH")}
        assert _pin(d, "QA").number == "15" and _pin(d, "QH'").number == "9" and _pin(d, "SER").number == "14"
        assert d.sim["twin"] == "shift_register_74hc595" and d.logic_from == "VCC"
    assert (hc.supply.v_min, hc.supply.v_max) == (2.0, 6.0)
    assert _pin(hc, "SER").vih_ratio == 0.7 and _pin(hc, "SER").vih_ratio * 4.5 == pytest.approx(3.15)
    assert (hct.supply.v_min, hct.supply.v_max) == (4.5, 5.5) and _pin(hct, "SER").vih == 2.0
    uln = get_def("uln2003a")
    assert _pin(uln, "1B").number == "1" and _pin(uln, "1C").number == "16" and _pin(uln, "7C").number == "10"
    assert _pin(uln, "COM").number == "9" and _pin(uln, "E").type == PinType.GND
    assert uln.params["clamp_pin"] == "COM" and "clamped_outputs" in uln.features
    hall = get_def("hall_a3144")
    assert _pin(hall, "OUT").type == PinType.OPEN_DRAIN and (hall.supply.v_min, hall.supply.v_max) == (4.5, 24.0)
    psu = get_def("psu_dc_5v5a")
    assert psu.params["i_max_ma"] == 5000 and psu.params["v_out"] == 5.0 and "psu" in psu.features


def test_eight_steppers_via_hct595_and_uln2003a_are_clean():
    c = money_counter("sn74hct595")
    rep = run_erc(c)
    assert not _problems(rep), [f.message for f in _problems(rep)]
    pb = power_budget(c)
    assert not _problems(pb.report), pb.to_markdown()
    five = pb.rails["5V"]
    assert five.available_ma == pytest.approx(5000 - 350)            # PSU − Pi Zero 2 W typical
    motors = [ma for ref, _t, ma in five.loads if ref.startswith("M")]
    assert len(motors) == 8 and sum(motors) == pytest.approx(1600)   # 8 × 200 mA worst case
    assert any(ref.startswith("H") for ref, *_ in five.loads)        # Hall sensors on 5 V
    assert five.utilization < 0.5


def test_hc595_at_5v_from_3v3_logic_warns_low_drive():
    rep = run_erc(money_counter("sn74hc595"))
    low = rep.by_code("ERC.LEVEL_LOW_DRIVE")
    assert low and all(f.severity.name == "WARNING" for f in low)
    subjects = " ".join(f.message for f in low)
    assert "U10.SER" in subjects and "U13.RCLK" in subjects and "U12.SRCLK" in subjects
    assert all(f.data["vih"] == pytest.approx(3.5) for f in low)
    # chained SER inputs are driven by the previous chip at 5 V: no warning for them
    assert "U11.SER" not in subjects
    assert not rep.errors


def test_hc595_powered_from_3v3_is_clean():
    rep = run_erc(money_counter("sn74hc595", sr_supply="3V3"))
    assert not rep.by_code("ERC.LEVEL_LOW_DRIVE"), [f.message for f in rep.by_code("ERC.LEVEL_LOW_DRIVE")]
    assert not rep.errors


def test_uln2003a_com_must_reach_the_motor_supply():
    rep = run_erc(money_counter(com=False))
    fly = rep.by_code("ERC.INDUCTIVE_NO_FLYBACK")
    assert len(fly) == 8 and all(f.severity.name == "WARNING" and "COM" in f.message for f in fly)
    assert "U20.COM" in fly[0].message


def test_uln2003a_without_com_but_external_diode_is_accepted():
    c = Circuit("one")
    pi = c.add("rpi4b", "U1")
    drv, m = c.add("uln2003a", "U2"), c.add("stepper_28byj48", "M1")
    c.connect(drv["E"], pi["GND"])
    c.connect(m["COM"], pi["5V"])
    for j, coil in enumerate("ABCD"):
        c.connect(pi[f"GPIO{(5, 6, 13, 19)[j]}"], drv[f"{j + 1}B"])
        c.connect(drv[f"{j + 1}C"], m[coil])
        d = c.add("diode_1n4007")
        c.connect(d["K"], pi["5V"])
        c.connect(d["A"], m[coil])
    assert not run_erc(c).by_code("ERC.INDUCTIVE_NO_FLYBACK")
    c.connect(drv["COM"], pi["5V"])
    assert not run_erc(c).by_code("ERC.INDUCTIVE_NO_FLYBACK")


def test_hall_sensor_needs_a_pull_up():
    c = Circuit("hall")
    pi = c.add("rpi4b", "U1")
    h = c.add("hall_a3144", "H1")
    c.connect(h["VCC"], pi["5V"])
    c.connect(h["GND"], pi["GND"])
    c.connect(h["OUT"], pi["GPIO4"])
    assert run_erc(c).by_code("ERC.FLOATING_INPUT")
    c.configure(pi, pulls={"GPIO4": "up"})
    rep = run_erc(c)
    assert not _problems(rep), [f.message for f in _problems(rep)]
