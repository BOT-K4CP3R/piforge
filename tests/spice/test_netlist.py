"""Netlist builder: value formatting, element lines, validation (no ngspice needed)."""

from __future__ import annotations

import math

import pytest

from piforge.core.errors import ValidationError
from piforge.spice.netlist import (
    PWL,
    Pulse,
    Sine,
    SpiceCircuit,
    SpiceValueError,
    fmt_value,
    parse_value,
)


@pytest.mark.parametrize(
    "x, expected",
    [
        (1000, "1k"),
        (4700.0, "4.7k"),
        (1e-6, "1u"),
        (2.2e-12, "2.2p"),
        (1e6, "1meg"),
        (1.5e9, "1.5g"),
        (0, "0"),
        (-3.3, "-3.3"),
        (0.5, "500m"),
        (3.3, "3.3"),
        (123456, "123.456k"),
        (47e-9, "47n"),
        (1e-18, "1e-18"),
        (999.99999999999, "1k"),  # rounding carries into the next prefix
    ],
)
def test_fmt_value(x, expected):
    assert fmt_value(x) == expected


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "1k", None, True])
def test_fmt_value_rejects_non_numbers(bad):
    with pytest.raises(SpiceValueError):
        fmt_value(bad)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("4.7k", 4700.0),
        ("1meg", 1e6),
        ("1MEG", 1e6),
        ("100n", 1e-7),
        ("100nF", 1e-7),
        ("2.2u", 2.2e-6),
        ("2.2µF", 2.2e-6),
        ("10", 10.0),
        ("1e-3", 1e-3),
        ("5m", 5e-3),  # SPICE: m = milli (case-insensitive)
        ("3.3V", 3.3),
        ("4.7kΩ", 4700.0),
        ("10kohm", 1e4),
        ("-1.5", -1.5),
        (" 220 ", 220.0),
        (330, 330.0),
    ],
)
def test_parse_value(text, expected):
    assert parse_value(text) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("bad", ["", "abc", "4.7kk", "1..2", "nan", "inf", "1 k k"])
def test_parse_value_rejects_garbage(bad):
    with pytest.raises(SpiceValueError):
        parse_value(bad)


def test_errors_are_validation_errors():
    assert issubclass(SpiceValueError, ValidationError)
    assert issubclass(SpiceValueError, ValueError)


def test_element_lines_and_ground_alias():
    c = SpiceCircuit("rc test")
    c.V("1", "in", "gnd", 3.3)
    c.R("1", "in", "out", 1000)
    c.C("load", "out", "GND", 1e-6)
    c.L("1", "out", "x", 10e-6)
    c.R("2", "x", "0", "4.7k")
    c.tran(1e-6, 3e-3)
    text = c.to_netlist()
    lines = text.splitlines()
    assert lines[0] == "rc test"
    assert "V1 in 0 DC 3.3" in lines
    assert "R1 in out 1k" in lines
    assert "Cload out 0 1u" in lines
    assert "L1 out x 10u" in lines
    assert "R2 x 0 4.7k" in lines
    assert ".tran 1u 3m" in lines
    assert lines[-1] == ".end"


def test_prefix_not_duplicated_and_names_case_insensitive():
    c = SpiceCircuit("t")
    c.R("R5", "a", "0", 10)
    c.op()
    assert "R5 a 0 10" in c.to_netlist()
    with pytest.raises(SpiceValueError, match="(?i)duplicate"):
        c.R("r5", "a", "0", 20)


def test_sources_pulse_pwl_sine_ac():
    c = SpiceCircuit("src")
    c.V("p", "a", "0", Pulse(0, 3.3, td=1e-3, tr=1e-9, tf=1e-9, pw=1e-3, per=2e-3))
    c.V("w", "b", "0", PWL(((0, 0), (1e-3, 1), (2e-3, 0))))
    c.V("s", "c", "0", Sine(0, 1, 1e3), ac=1)
    c.I("load", "d", "0", 2.5e-3)
    for n in "abcd":
        c.R(n, n, "0", 1e3)
    c.op()
    text = c.to_netlist()
    assert "Vp a 0 PULSE(0 3.3 1m 1n 1n 1m 2m)" in text
    assert "Vw b 0 PWL(0 0 1m 1 2m 0)" in text
    assert "Vs c 0 SIN(0 1 1k 0) AC 1" in text
    assert "Iload d 0 DC 2.5m" in text
    assert ".op" in text


def test_semiconductors_switch_behavioural_and_subckt_pull_models():
    c = SpiceCircuit("semis")
    c.V("dd", "vdd", "0", 5)
    c.D("1", "vdd", "a", "led_red")
    c.D("2", "a", "0", "led_red")  # second use: model text included once
    c.Q("1", "vdd", "b", "0", "q2n3904")
    c.R("b", "vdd", "b", 10e3)
    c.M("1", "vdd", "g", "0", "nmos_ao3400")
    c.R("g", "g", "0", 1e3)
    c.S("1", "vdd", "s", "ctl", "0", "sw_ideal")
    c.V("ctl", "ctl", "0", 1)
    c.R("s", "s", "0", 100)
    c.B("1", "e", "0", "V(vdd)*0.5")
    c.R("e", "e", "0", 1e3)
    c.X("relay", ["vdd", "k"], "relay_coil_5v")
    c.R("k", "k", "0", 1)
    c.op()
    text = c.to_netlist()
    assert "D1 vdd a led_red" in text
    assert "Q1 vdd b 0 q2n3904" in text
    assert "M1 vdd g 0 nmos_ao3400" in text  # VDMOS: 3 terminals
    assert "S1 vdd s ctl 0 sw_ideal" in text
    assert "B1 e 0 V=V(vdd)*0.5" in text
    assert "Xrelay vdd k relay_coil_5v" in text
    low = text.lower()
    assert low.count(".model led_red ") == 1
    assert ".model q2n3904 npn" in low
    assert ".model nmos_ao3400 vdmos" in low
    assert ".subckt relay_coil_5v" in low


def test_subckt_params_rendered():
    c = SpiceCircuit("p")
    c.X("psu", ["out", "0"], "psu_cable", params={"vset": 5.1, "rcable": 0.2})
    c.R("load", "out", "0", 10)
    c.op()
    assert "Xpsu out 0 psu_cable vset=5.1 rcable=200m" in c.to_netlist()


def test_vdmos_rejects_separate_body_node():
    c = SpiceCircuit("m")
    with pytest.raises(SpiceValueError, match="(?i)body|bulk|3-terminal"):
        c.M("1", "d", "g", "s", "nmos_bss138", b="x")


def test_element_model_kind_checked():
    c = SpiceCircuit("k")
    with pytest.raises(SpiceValueError, match="(?i)diode|model"):
        c.D("1", "a", "0", "q2n2222")
    with pytest.raises(SpiceValueError, match="(?i)pins|nodes"):
        c.X("1", ["a"], "gpio_out")  # gpio_out has 4 pins


def test_unknown_model_suggests_close_match():
    c = SpiceCircuit("u")
    c.V("1", "a", "0", 1)
    c.D("1", "a", "0", "led_rd")
    c.op()
    with pytest.raises(SpiceValueError, match="led_red"):
        c.to_netlist()


def test_custom_model_text_accepted():
    c = SpiceCircuit("custom")
    c.add_model_text(".model mydiode D(IS=1e-14 N=1)")
    c.V("1", "a", "0", 1)
    c.R("1", "a", "b", 100)
    c.D("1", "b", "0", "mydiode")
    c.op()
    assert ".model mydiode D(IS=1e-14 N=1)" in c.to_netlist()


@pytest.mark.parametrize(
    "build",
    [
        lambda c: c.R("1", "a", "0", 0),
        lambda c: c.R("1", "a", "0", -10),
        lambda c: c.R("1", "a", "0", math.nan),
        lambda c: c.C("1", "a", "0", -1e-6),
        lambda c: c.C("1", "a", "0", 0),
        lambda c: c.L("1", "a", "0", math.inf),
        lambda c: c.V("1", "a", "0", math.nan),
        lambda c: c.V("1", "a", "0", Pulse(0, 1, per=0)),
        lambda c: c.V("1", "a", "0", Pulse(0, 1, tr=-1e-9)),
        lambda c: c.V("1", "a", "0", PWL(((0, 0), (1e-3, 1), (1e-3, 0)))),
        lambda c: c.V("1", "a", "0", PWL(())),
        lambda c: c.V("1", "a", "0", Sine(0, 1, 0)),
        lambda c: c.R("bad name", "a", "0", 1),
        lambda c: c.R("1", "a b", "0", 1),
        lambda c: c.R("1", "a(", "0", 1),
        lambda c: c.B("1", "a", "0", "V(x)", kind="Q"),
        lambda c: c.tran(0, 1e-3),
        lambda c: c.tran(1e-6, 0),
        lambda c: c.ac(10, 0, 1e3),
        lambda c: c.ac(10, 1e3, 1e2),
        lambda c: c.ac(10, 1, 1e3, sweep="log"),
        lambda c: c.dc("Vnope", 0, 1, 0.1),
        lambda c: c.measure("m1", "op FIND v(a)"),
        lambda c: c.measure("bad name", "tran FIND v(a) AT=1m"),
    ],
)
def test_invalid_values_raise_before_running(build):
    c = SpiceCircuit("bad")
    with pytest.raises(SpiceValueError):
        build(c)


def test_netlist_requires_analysis_and_ground():
    c = SpiceCircuit("no analysis")
    c.V("1", "a", "0", 1)
    c.R("1", "a", "0", 1)
    with pytest.raises(SpiceValueError, match="(?i)analysis"):
        c.to_netlist()
    d = SpiceCircuit("no ground")
    d.V("1", "a", "b", 1)
    d.R("1", "a", "b", 1)
    d.op()
    with pytest.raises(SpiceValueError, match="(?i)ground"):
        d.to_netlist()


def test_measure_save_options_ic_render():
    c = SpiceCircuit("m")
    c.V("1", "in", "0", Pulse(0, 3.3, pw=10e-3, per=20e-3))
    c.R("1", "in", "out", 1e3)
    c.C("1", "out", "0", 1e-6)
    c.tran(1e-6, 3e-3, uic=True)
    c.measure("vtau", "tran FIND v(out) AT=1m")
    c.save("v(out)", "i(v1)")
    c.option(reltol=1e-4)
    c.ic("out", 0.0)
    text = c.to_netlist()
    assert ".meas tran vtau FIND v(out) AT=1m" in text
    assert ".save v(out) i(v1)" in text
    assert ".options reltol=100u" in text
    assert ".ic v(out)=0" in text
    assert ".tran 1u 3m uic" in text
    assert c.analyses == ["tran 1u 3m uic"]
    assert c.measure_names == ["vtau"]
    with pytest.raises(SpiceValueError, match="(?i)duplicate"):
        c.measure("VTAU", "tran FIND v(out) AT=2m")


def test_dc_sweep_resolves_source_name():
    c = SpiceCircuit("dc")
    c.V("in", "a", "0", 0)
    c.R("1", "a", "0", 1e3)
    c.dc("in", 0, 5, 0.5)
    c.dc("Vin", 0, 1, 0.5)
    assert c.analyses == ["dc Vin 0 5 500m", "dc Vin 0 1 500m"]
