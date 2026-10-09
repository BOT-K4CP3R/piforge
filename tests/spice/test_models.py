"""Device model library: every model loads, and each reproduces its datasheet key values.

Datasheet sources are cited next to each expected value; the model text in
``piforge.spice.models`` carries the same citation.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from piforge.core.errors import NotFoundError
from piforge.spice.models import (
    MODEL_INFO,
    MODELS,
    gpio_resistances,
    list_models,
    model_info,
    model_text,
)
from piforge.spice.netlist import SpiceCircuit
from piforge.spice.runner import run

EXPECTED_KEYS = {
    "led_red", "led_green", "led_blue", "led_white", "led_ir",
    "d1n4148", "d1n4007", "d1n5819", "zener_3v3",
    "q2n2222", "q2n3904",
    "nmos_2n7000", "nmos_bss138", "nmos_ao3400", "nmos_irlz44n", "nmos_irf540n",
    "sw_ideal", "gpio_out", "relay_coil_5v", "dc_motor_small", "fan_5v", "psu_cable",
}


def test_model_keys_and_listing():
    assert EXPECTED_KEYS <= set(MODELS)
    assert list_models() == sorted(MODELS)
    assert set(list_models("led")) == {"led_red", "led_green", "led_blue", "led_white", "led_ir"}
    assert "nmos_ao3400" in list_models("nmos")


def test_model_text_unknown_suggests():
    with pytest.raises(NotFoundError, match="led_red"):
        model_text("led_rd")
    with pytest.raises(NotFoundError):
        model_info("nope")


def test_every_model_documented():
    for key, text in MODELS.items():
        info = MODEL_INFO[key]
        assert info.key == key
        assert info.reproduces, key  # the datasheet value the model is fitted to
        assert info.source, key
        first = text.strip().splitlines()[0].lower()
        defined = first.split()[1]
        assert defined == key, f"{key}: model/subckt name must equal the key"
        if info.element == "X":
            assert first.startswith(".subckt") and info.pins
        else:
            assert first.startswith(".model")


def test_gpio_resistances_follow_drive_strength():
    # src: raspberrypi/documentation gpio-pad-controls.adoc — up to the drive-strength current the
    # pad keeps VOH ≥ 3.0 V and VOL ≤ 0.14 V at VDD IO = 3.3 V → R ≤ 0.3 V/I and ≤ 0.14 V/I.
    assert gpio_resistances(8) == pytest.approx((37.5, 17.5))
    assert gpio_resistances(16) == pytest.approx((18.75, 8.75))
    with pytest.raises(ValueError):
        gpio_resistances(3)


def _hookup(key: str, c: SpiceCircuit) -> None:
    """Connect model ``key`` in a minimal circuit that has a DC solution."""
    info = MODEL_INFO[key]
    if info.element == "D":
        c.I("f", "0", "a", 1e-3)
        c.D("1", "a", "0", key)
    elif info.element == "Q":
        c.V("cc", "vcc", "0", 5)
        c.R("c", "vcc", "c", 1e3)
        c.R("b", "vcc", "b", 100e3)
        c.Q("1", "c", "b", "0", key)
    elif info.element == "M":
        c.V("dd", "vdd", "0", 5)
        c.R("d", "vdd", "d", 100)
        c.V("g", "g", "0", 5)
        c.M("1", "d", "g", "0", key)
    elif info.element == "S":
        c.V("1", "a", "0", 1)
        c.R("1", "a", "b", 1e3)
        c.V("c", "ctl", "0", 1)
        c.S("1", "b", "0", "ctl", "0", key)
    elif key == "gpio_out":
        c.V("dd", "vdd", "0", 3.3)
        c.V("c", "ctl", "0", 3.3)
        c.X("1", ["pin", "vdd", "0", "ctl"], key)
        c.R("l", "pin", "0", 1e3)
    elif key in ("relay_coil_5v", "dc_motor_small", "fan_5v"):
        c.V("1", "p", "0", 1.5)
        c.X("1", ["p", "0"], key)
    elif key == "psu_cable":
        c.X("1", ["out", "0"], key)
        c.R("l", "out", "0", 10)
    else:  # pragma: no cover - a new model needs a hookup here
        raise AssertionError(f"no test hookup for {key}")


@pytest.mark.spice
@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS))
def test_every_model_loads(ngspice_path, key):
    c = SpiceCircuit(f"load {key}")
    _hookup(key, c)
    c.op()
    c.tran(1e-6, 100e-6)
    res = run(c)
    for name, vec in res.plot("op").items():
        assert np.all(np.isfinite(vec)), (key, name)
    assert res.plot("tran")["time"][-1] == pytest.approx(100e-6)


def _op(build) -> dict:
    c = SpiceCircuit("probe")
    build(c)
    c.op()
    return {k: float(v[0]) for k, v in run(c).plot("op").items()}


# key: (VF typ at IF = 20 mA, tolerance) — Everlight 333-2SURD/S530-A3 (red, 2.0 V typ),
# Kingbright WP7113GD (green GaP, 2.2 V typ), Kingbright WP7113QBC/D (blue InGaN, 3.3 V typ),
# Everlight 334-15/T1C1-4WYA (white InGaN, 2.8–3.6 V → 3.2 V), Everlight IR333-A (940 nm, 1.2 V typ).
LED_VF_20MA = {
    "led_red": (2.0, 0.1),
    "led_green": (2.2, 0.1),
    "led_blue": (3.3, 0.1),
    "led_white": (3.2, 0.15),
    "led_ir": (1.2, 0.1),
}


@pytest.mark.spice
def test_led_forward_voltage_at_20ma(ngspice_path):
    def build(c):
        for k in LED_VF_20MA:
            c.I(k, "0", k, 20e-3)
            c.D(k, k, "0", k)

    v = _op(build)
    for k, (vf, tol) in LED_VF_20MA.items():
        assert abs(v[f"v({k})"] - vf) <= tol, (k, v[f"v({k})"])
    # red LED at the ~4 mA a 330 Ω GPIO driver gives is still within 1.8–2.0 V
    v4 = _op(lambda c: (c.I("f", "0", "a", 4e-3), c.D("1", "a", "0", "led_red")))
    assert 1.8 <= v4["v(a)"] <= 2.0


@pytest.mark.spice
def test_diode_datasheet_points(ngspice_path):
    # 1N4148 (onsemi/Vishay): VF ≤ 1.0 V @ 10 mA, typical curve ≈ 0.72 V.
    # 1N4007 (onsemi 1N4001-7): VF ≤ 1.1 V @ 1 A, typical ≈ 0.93 V.
    # 1N5819 (onsemi 1N5817-9): VF ≤ 0.60 V @ 1 A (Schottky).
    # Zener 3.3 V (Nexperia BZX79-C3V3): VZ 3.1–3.5 V @ IZ = 5 mA.
    def build(c):
        c.I("a", "0", "a", 10e-3)
        c.D("a", "a", "0", "d1n4148")
        c.I("b", "0", "b", 1.0)
        c.D("b", "b", "0", "d1n4007")
        c.I("c", "0", "c", 1.0)
        c.D("c", "c", "0", "d1n5819")
        c.I("z", "0", "z", 5e-3)
        c.D("z", "0", "z", "zener_3v3")  # reverse-biased: current into the cathode

    v = _op(build)
    assert 0.65 <= v["v(a)"] <= 0.80
    assert 0.85 <= v["v(b)"] <= 1.10
    assert 0.40 <= v["v(c)"] <= 0.60
    assert 3.1 <= v["v(z)"] <= 3.5


@pytest.mark.spice
def test_bjt_gain_and_saturation(ngspice_path):
    # 2N2222A (onsemi P2N2222A): hFE 100–300 @ IC = 150 mA, VCE = 10 V; VCE(sat) ≤ 0.3 V @ 150/15 mA.
    # 2N3904 (onsemi): hFE 100–300 @ IC = 10 mA, VCE = 1 V; VCE(sat) ≤ 0.2 V @ 10/1 mA.
    for key, ic, vce, ib_sat, vce_sat_max in (("q2n2222", 0.15, 10.0, 0.015, 0.3),
                                              ("q2n3904", 0.01, 1.0, 0.001, 0.2)):
        c = SpiceCircuit("hfe")
        c.V("ce", "c", "0", vce)
        c.I("b", "0", "b", 1e-6)
        c.Q("1", "c", "b", "0", key)
        c.dc("Ib", 1e-6, 5e-3, 1e-6)
        res = run(c)
        icv = -res.vectors["i(vce)"]
        ib = next(iter(res.vectors.values()))  # first vector of a DC plot = the swept source value
        ib_at = float(np.interp(ic, icv, ib))
        hfe = ic / ib_at
        assert 100 <= hfe <= 300, (key, hfe)
        s = _op(lambda c, key=key, ic=ic, ib=ib_sat: (
            c.I("c", "0", "c", ic), c.I("b", "0", "b", ib), c.Q("1", "c", "b", "0", key)))
        assert s["v(c)"] <= vce_sat_max, (key, s["v(c)"])
        assert 0.6 <= s["v(b)"] <= 1.2


# Datasheet conditions: threshold current, VGS(th) range, RDS(on) (VGS, ID, max, typical or None).
MOSFET_SPECS = {
    # onsemi 2N7000: VGS(th) 0.8–3.0 V @ 1 mA; RDS(on) 1.2 typ/5 max Ω @ 10 V/0.5 A,
    # 1.8 typ/5.3 max Ω @ 4.5 V/75 mA.
    "nmos_2n7000": (1e-3, (0.8, 3.0), [(10.0, 0.5, 5.0, 1.2), (4.5, 0.075, 5.3, 1.8)]),
    # onsemi BSS138: VGS(th) 0.8–1.5 V @ 1 mA; RDS(on) 0.7 typ/3.5 max Ω @ 10 V,
    # 1.0 typ/6.0 max Ω @ 4.5 V (0.22 A).
    "nmos_bss138": (1e-3, (0.8, 1.5), [(10.0, 0.22, 3.5, 0.7), (4.5, 0.22, 6.0, 1.0)]),
    # AOS AO3400: VGS(th) 0.65–1.45 V @ 250 µA; RDS(on) < 28/33/52 mΩ @ 10/4.5/2.5 V.
    "nmos_ao3400": (250e-6, (0.65, 1.45),
                    [(10.0, 5.8, 0.028, None), (4.5, 5.0, 0.033, None), (2.5, 4.0, 0.052, None)]),
    # Infineon IRLZ44N: VGS(th) 1–2 V @ 250 µA; RDS(on) ≤ 22/25/35 mΩ @ 10/5/4 V.
    "nmos_irlz44n": (250e-6, (1.0, 2.0),
                     [(10.0, 25.0, 0.022, None), (5.0, 25.0, 0.025, None), (4.0, 21.0, 0.035, None)]),
    # Infineon IRF540N: VGS(th) 2–4 V @ 250 µA; RDS(on) ≤ 44 mΩ @ 10 V/16 A.
    "nmos_irf540n": (250e-6, (2.0, 4.0), [(10.0, 16.0, 0.044, None)]),
}


@pytest.mark.spice
@pytest.mark.parametrize("key", sorted(MOSFET_SPECS))
def test_mosfet_threshold_and_rdson(ngspice_path, key):
    ith, (vth_min, vth_max), rds_points = MOSFET_SPECS[key]

    def build(c):
        c.I("th", "0", "th", ith)  # gate tied to drain: V(th) = VGS(th) at ID = ith
        c.M("th", "th", "th", "0", key)
        for i, (vgs, idrain, _mx, _typ) in enumerate(rds_points):
            c.I(f"d{i}", "0", f"d{i}", idrain)
            c.V(f"g{i}", f"g{i}", "0", vgs)
            c.M(f"r{i}", f"d{i}", f"g{i}", "0", key)
        c.I("bd", "0", "bd", 0.1)  # body diode: source → drain
        c.M("bd", "0", "bd", "bd", key)

    v = _op(build)
    assert vth_min <= v["v(th)"] <= vth_max, (key, v["v(th)"])
    for i, (vgs, idrain, rmax, typ) in enumerate(rds_points):
        rds = v[f"v(d{i})"] / idrain
        assert rds <= rmax, (key, vgs, rds)
        if typ is not None:  # datasheet typical given: reproduce it within 10 %
            assert rds == pytest.approx(typ, rel=0.10), (key, vgs, rds)
        else:  # only a max is given: fitted to ≈ 70–80 % of it, never implausibly low
            assert rds >= 0.4 * rmax, (key, vgs, rds)
    assert 0.5 <= v["v(bd)"] <= 1.1, (key, v["v(bd)"])


@pytest.mark.spice
def test_logic_level_vs_standard_mosfet_at_3v3(ngspice_path):
    # Brief: AO3400 RDS(on) at VGS = 2.5 V < 60 mΩ; IRF540N essentially off at 3.3 V, on at 10 V.
    def build(c):
        c.V("dd", "vdd", "0", 12.0)
        for name, key, vgs in (("a", "nmos_irf540n", 3.3), ("b", "nmos_irf540n", 10.0)):
            c.R(name, "vdd", name, 12.0)  # 1 A load
            c.V(f"g{name}", f"g{name}", "0", vgs)
            c.M(name, name, f"g{name}", "0", key)
        c.I("c", "0", "c", 4.0)
        c.V("gc", "gc", "0", 2.5)
        c.M("c", "c", "gc", "0", "nmos_ao3400")

    v = _op(build)
    i_off = (12.0 - v["v(a)"]) / 12.0
    i_on = (12.0 - v["v(b)"]) / 12.0
    assert i_off < 0.01 * i_on  # IRF540N with a 3.3 V gate: < 1 % of the on-current
    assert v["v(b)"] < 0.05  # fully on at 10 V
    assert v["v(c)"] / 4.0 < 0.060


@pytest.mark.spice
def test_gpio_out_levels(ngspice_path):
    # Pi GPIO at the default 8 mA drive: VOH ≥ 3.0 V sourcing 8 mA, VOL ≤ 0.14 V sinking 8 mA.
    def build(c):
        c.V("dd", "vdd", "0", 3.3)
        c.V("hi", "hi", "0", 3.3)
        c.V("lo", "lo", "0", 0.0)
        c.X("h", ["ph", "vdd", "0", "hi"], "gpio_out")
        c.X("l", ["pl", "vdd", "0", "lo"], "gpio_out")
        c.I("h", "ph", "0", 8e-3)  # 8 mA out of the pin
        c.I("l", "0", "pl", 8e-3)  # 8 mA into the pin

    v = _op(build)
    assert v["v(ph)"] == pytest.approx(3.0, abs=0.01)
    assert v["v(pl)"] == pytest.approx(0.14, abs=0.01)


@pytest.mark.spice
def test_electromechanical_subckts(ngspice_path):
    # Songle SRD-05VDC-SL-C: 70 Ω coil → 71.4 mA at 5 V.
    # Mabuchi FA-130RA-2270 @ 1.5 V: no-load 9100 r/min & 0.20 A, stall current 2.20 A.
    def build(c):
        c.V("r", "r", "0", 5.0)
        c.X("r", ["r", "0"], "relay_coil_5v")
        c.V("m", "m", "0", 1.5)
        c.X("m", ["m", "0"], "dc_motor_small")
        c.V("s", "s", "0", 1.5)
        c.X("s", ["s", "0"], "dc_motor_small", params={"jm": 1e3})  # huge inertia ≈ locked rotor
        c.X("p", ["out", "0"], "psu_cable", params={"vset": 5.1, "rsrc": 0.0, "rcable": 0.2})
        c.R("p", "out", "0", 5.1 / 2.5 - 0.2)  # draws 2.5 A

    v = _op(build)
    assert -v["i(vr)"] == pytest.approx(5 / 70, rel=0.02)
    assert -v["i(vm)"] == pytest.approx(0.20, rel=0.10)
    rpm = v["v(xm.w)"] * 60 / (2 * math.pi)
    assert rpm == pytest.approx(9100, rel=0.05)
    assert v["v(out)"] == pytest.approx(5.1 - 2.5 * 0.2, abs=0.01)

    c = SpiceCircuit("stall")
    c.V("s", "s", "0", 1.5)
    c.X("s", ["s", "0"], "dc_motor_small", params={"jm": 1e3})
    c.tran(1e-5, 5e-3, uic=True)
    res = run(c)
    assert -float(res.vectors["i(vs)"][-1]) == pytest.approx(2.20, rel=0.05)


@pytest.mark.spice
def test_led_reverse_bias_does_not_clamp_at_5v(ngspice_path):
    # VR = 5 V is a rating, not a clamp: at 12 V reverse the LED blocks (BV = 20 V).
    c = SpiceCircuit("led reverse")
    c.V("r", "r", "0", 12.0)
    c.R("s", "r", "k", 1e3)
    c.D("1", "0", "k", "led_red")  # anode to ground: reverse-biased
    c.op()
    res = run(c)
    assert res.vectors["v(k)"][0] > 11.0
