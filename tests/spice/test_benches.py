"""Parametrised SPICE benches: physics/datasheet validation, findings, GUI-ready output, plots."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from piforge.core.errors import NotFoundError, ValidationError
from piforge.core.report import Report
from piforge.spice import bench_switching, benches
from piforge.spice.benches import (
    BENCHES,
    Bench,
    BenchParamError,
    BenchResult,
    ParamSpec,
    bench_catalog,
    downsample,
    run_bench,
)
from piforge.spice.plot import plot_result

EXPECTED_BENCHES = {
    "led_driver", "voltage_divider", "rc_filter", "button_debounce", "mosfet_lowside",
    "bjt_switch", "level_shifter_bss138", "i2c_rise_time", "power_path",
}


# ---------------------------------------------------------------- API shape (no ngspice) ----
def test_registry_and_param_specs_are_gui_ready():
    assert EXPECTED_BENCHES <= set(BENCHES)
    for key, bench in BENCHES.items():
        assert isinstance(bench, Bench) and bench.key == key
        assert bench.title and bench.description
        assert "timeout" not in bench.params
        for name, spec in bench.params.items():
            assert isinstance(spec, ParamSpec)
            assert spec.label, (key, name)
            d = spec.to_dict()
            json.dumps(d)
            assert d["type"] in {"number", "choice", "bool", "text"}
            if spec.choices:
                assert spec.default in spec.choices, (key, name)
            elif isinstance(spec.default, (int, float)) and not isinstance(spec.default, bool):
                assert spec.min is None or spec.default >= spec.min, (key, name)
                assert spec.max is None or spec.default <= spec.max, (key, name)
    cat = bench_catalog()
    json.dumps(cat)
    assert {c["key"] for c in cat} == set(BENCHES)
    assert all("params" in c and "description" in c for c in cat)


def _no_sim(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("simulation must not start for invalid parameters")

    monkeypatch.setattr(benches, "run", boom)


def test_invalid_values(monkeypatch):
    # Review Focus #5: invalid inputs are rejected before ngspice is started.
    _no_sim(monkeypatch)
    with pytest.raises(BenchParamError, match="r_series"):
        run_bench("led_driver", r_series=0)  # below min (1 Ω)
    with pytest.raises(BenchParamError, match="led_red"):
        run_bench("led_driver", led="led_rde")  # not a choice → close match suggested
    with pytest.raises(BenchParamError, match="r_series"):
        run_bench("led_driver", rseries=330)  # unknown parameter → suggestion
    with pytest.raises(BenchParamError):
        run_bench("i2c_rise_time", c_bus=float("nan"))
    with pytest.raises(BenchParamError):
        run_bench("i2c_rise_time", r_pullup="lots")
    with pytest.raises(BenchParamError):
        run_bench("mosfet_lowside", flyback="maybe")
    with pytest.raises(BenchParamError, match="R·C"):
        run_bench("rc_filter", r=1e9, c=10.0)  # each value in range, τ = 1e10 s is not
    with pytest.raises(NotFoundError, match="led_driver"):
        run_bench("led_drivr")
    assert issubclass(BenchParamError, ValidationError)


def test_param_coercion_accepts_gui_and_cli_strings():
    spec = ParamSpec(4.7e3, min=100.0, max=1e6, unit="Ω", label="R")
    assert spec.coerce("r", "4.7k") == pytest.approx(4700.0)
    assert spec.coerce("r", 330) == 330.0
    flag = ParamSpec(True, choices=(False, True), label="flag")
    assert flag.coerce("f", "false") is False and flag.coerce("f", "1") is True
    choice = ParamSpec(100e3, choices=(100e3, 400e3, 1e6), unit="Hz", label="speed")
    assert choice.coerce("s", "400k") == 400e3
    with pytest.raises(BenchParamError):
        choice.coerce("s", 200e3)
    with pytest.raises(BenchParamError):
        spec.coerce("r", True)


def test_downsample_keeps_peaks_and_limits_points():
    x = np.linspace(0, 1, 100_001)
    y = np.zeros_like(x)
    y[54_321] = 40.0  # a single-sample flyback spike must survive decimation
    z = np.sin(2 * np.pi * 5 * x)
    xs, ys = downsample(x, {"y": y, "z": z}, max_points=2000)
    assert len(xs) <= 2000 and len(ys["y"]) == len(xs)
    assert ys["y"].max() == 40.0
    assert np.all(np.diff(xs) > 0)
    assert xs[0] == 0 and xs[-1] == 1
    small_x, small = downsample(x[:10], {"y": y[:10]}, max_points=2000)
    assert len(small_x) == 10


# ---------------------------------------------------------------- simulations ---------------
def _assert_gui_ready(res: BenchResult) -> None:
    assert res.x_label in res.traces
    n = len(res.traces[res.x_label])
    assert 2 <= n <= 2000
    for name, values in res.traces.items():
        assert isinstance(values, list) and len(values) == n, name
        assert all(isinstance(v, float) for v in values), name
    d = res.to_dict()
    text = json.dumps(d)  # strict JSON: no NaN/inf
    assert "NaN" not in text and "Infinity" not in text
    assert d["report"]["title"]
    assert isinstance(res.report, Report)


@pytest.mark.spice
def test_led_current(ngspice_path):
    # Analytic: I = (3.3 V − VF)/R ≈ (3.3 − 2.0)/330 = 3.9 mA (VF from Everlight 333-2SURD).
    res = run_bench("led_driver")
    assert res.params["r_series"] == 330.0 and res.params["led"] == "led_red"
    assert 3.0e-3 <= res.measures["i_led"] <= 4.5e-3
    assert 1.8 <= res.measures["v_led"] <= 2.1
    assert res.analytic["i_led"] == pytest.approx((3.3 - 2.0) / 330, rel=1e-6)
    assert res.measures["i_led"] == pytest.approx(res.analytic["i_led"], rel=0.10)
    assert res.report.ok, res.report.to_markdown()
    assert not res.report.has("SPICE.GPIO_OVERCURRENT")
    _assert_gui_ready(res)


@pytest.mark.spice
def test_led_overcurrent_flagged(ngspice_path):
    res = run_bench("led_driver", r_series=47)
    assert res.report.has("SPICE.GPIO_OVERCURRENT", "error"), res.report.to_markdown()
    assert not res.report.ok


@pytest.mark.spice
def test_led_sink_topology_and_blue_led_dim(ngspice_path):
    res = run_bench("led_driver", topology="sink")
    assert 3.0e-3 <= res.measures["i_led"] <= 4.5e-3
    blue = run_bench("led_driver", led="led_blue", r_series=1000)
    assert blue.measures["i_led"] < 1e-3
    assert blue.report.has("SPICE.LED_DIM", "warning")


@pytest.mark.spice
def test_divider(ngspice_path):
    res = run_bench("voltage_divider", v_in=5.0, r_top=1e3, r_bottom=2e3)
    assert res.measures["v_out"] == pytest.approx(5 * 2 / 3, rel=0.005)
    loaded = run_bench("voltage_divider", v_in=5.0, r_top=1e3, r_bottom=2e3, r_load=10e3)
    assert loaded.measures["v_out"] == pytest.approx(3.125, rel=0.005)
    assert loaded.analytic["v_out"] == pytest.approx(3.125, rel=1e-9)
    assert loaded.report.has("SPICE.DIVIDER_LOADED", "warning")
    _assert_gui_ready(loaded)


@pytest.mark.spice
def test_rc_filter_bench(ngspice_path):
    ac = run_bench("rc_filter", r=1e3, c=1e-6, analysis="ac")
    fc = 1 / (2 * math.pi * 1e-3)
    assert ac.measures["f_3db"] == pytest.approx(fc, rel=0.03)
    assert ac.x_scale == "log"
    _assert_gui_ready(ac)
    step = run_bench("rc_filter", r=1e3, c=1e-6, analysis="step", v_step=3.3)
    assert step.measures["v_at_tau"] == pytest.approx(3.3 * (1 - math.exp(-1)), rel=0.01)
    assert step.measures["tau"] == pytest.approx(1e-3, rel=0.01)
    assert step.measures["t_rise"] == pytest.approx(math.log(9) * 1e-3, rel=0.02)


@pytest.mark.spice
def test_flyback(ngspice_path):
    bare = run_bench("mosfet_lowside", load="relay_coil_5v", flyback=False)
    assert bare.measures["v_drain_peak"] > 30.0
    assert bare.report.has("SPICE.FLYBACK_OVERVOLTAGE", "error")
    clamped = run_bench("mosfet_lowside", load="relay_coil_5v", flyback=True)
    assert clamped.measures["v_drain_peak"] < 5.0 + 1.5
    assert not clamped.report.has("SPICE.FLYBACK_OVERVOLTAGE")
    assert clamped.measures["i_load_on"] == pytest.approx(5.0 / 70.0, rel=0.05)
    assert clamped.report.ok, clamped.report.to_markdown()
    _assert_gui_ready(bare)


@pytest.mark.spice
def test_irf540n_not_driven_by_gpio(ngspice_path):
    res = run_bench("mosfet_lowside", mosfet="nmos_irf540n")
    assert res.measures["i_load_on"] < 0.01
    assert res.report.has("SPICE.MOSFET_GATE_DRIVE", "error")
    assert res.report.has("SPICE.RELAY_NO_PULLIN", "error")


@pytest.mark.spice
def test_bjt_switch(ngspice_path):
    res = run_bench("bjt_switch")
    assert res.measures["v_ce_sat"] < 0.3
    assert res.measures["i_c"] == pytest.approx(5.0 / 70.0, rel=0.06)
    assert res.report.ok, res.report.to_markdown()
    weak = run_bench("bjt_switch", r_base=100e3)
    assert weak.report.has("SPICE.BJT_NOT_SATURATED", "warning")


@pytest.mark.spice
def test_i2c_rise_time(ngspice_path):
    # NXP UM10204: t_r measured 30 % → 70 % of VDD: t_r = RC·ln(7/3) = 0.8473·RC.
    rc = 4.7e3 * 200e-12
    fast = run_bench("i2c_rise_time", r_pullup=4.7e3, c_bus=200e-12, speed=400e3)
    assert fast.measures["t_r"] == pytest.approx(0.8473 * rc, rel=0.05)
    assert fast.analytic["t_r"] == pytest.approx(0.8473 * rc, rel=1e-3)
    assert fast.report.has("SPICE.I2C_RISE_TIME", "warning")  # limit 300 ns in Fast-mode
    std = run_bench("i2c_rise_time", r_pullup=4.7e3, c_bus=200e-12, speed=100e3)
    assert not std.report.has("SPICE.I2C_RISE_TIME")  # limit 1000 ns in Standard-mode
    assert std.report.ok
    _assert_gui_ready(std)


@pytest.mark.spice
def test_level_shifter(ngspice_path):
    up = run_bench("level_shifter_bss138", direction="low_to_high", freq=100e3)
    assert up.measures["v_rx_high"] > 4.5  # 5 V side reaches > 4.5 V
    assert up.measures["v_rx_low"] < 0.4  # 5 V side follows the 3.3 V side low
    assert up.measures["v_tx_low"] < 0.4  # 3.3 V side when driven low
    assert up.report.ok, up.report.to_markdown()
    down = run_bench("level_shifter_bss138", direction="high_to_low", freq=100e3)
    assert down.measures["v_rx_low"] < 0.4  # 3.3 V side pulled low via body diode + channel
    assert down.measures["v_rx_high"] > 3.0
    _assert_gui_ready(down)


@pytest.mark.spice
def test_power_path(ngspice_path):
    # 5.1 V − 2.5 A·(0.2 Ω cable + PSU source resistance) < 4.63 V (Pi under-voltage detector).
    bad = run_bench("power_path", v_psu=5.1, r_cable=0.2, i_load=2.5)
    assert bad.measures["v_min"] < 4.63
    assert bad.report.has("SPICE.BROWNOUT", "warning")
    good = run_bench("power_path", v_psu=5.1, i_load=2.5)  # official 1.5 m 18 AWG cable
    assert good.measures["v_min"] > 4.63
    assert not good.report.has("SPICE.BROWNOUT", "warning")
    assert good.measures["v_loaded"] == pytest.approx(good.analytic["v_loaded"], rel=0.005)


@pytest.mark.spice
def test_button_debounce(ngspice_path):
    res = run_bench("button_debounce")  # 10 kΩ pull-up, 1 kΩ series, 100 nF, 2 ms bounce
    assert res.measures["edges_press"] == 1 and res.measures["edges_release"] == 1
    assert not res.report.has("SPICE.DEBOUNCE_BOUNCE")
    # with bounce the release is seen later than the clean-contact formula (re-closures discharge C)
    assert res.measures["t_release_detect"] > res.analytic["t_release_detect"]
    # clean contact: t = (R_pu + R_s)·C·ln(VDD/(VDD − VIH)) and t = R_s·C·ln(VDD/VIL)
    clean = run_bench("button_debounce", bounce_ms=0)
    assert clean.measures["t_release_detect"] == pytest.approx(clean.analytic["t_release_detect"],
                                                               rel=0.03)
    assert clean.measures["t_press_detect"] == pytest.approx(clean.analytic["t_press_detect"], rel=0.05)
    raw = run_bench("button_debounce", c=0)
    assert raw.measures["edges_press"] > 1
    assert raw.report.has("SPICE.DEBOUNCE_BOUNCE", "warning")


@pytest.mark.spice
@pytest.mark.parametrize("key", sorted(EXPECTED_BENCHES))
def test_every_bench_runs_with_defaults(ngspice_path, key):
    res = run_bench(key)
    assert res.key == key
    assert res.measures and all(isinstance(v, float) for v in res.measures.values())
    assert set(res.analytic) <= set(res.measures)
    assert res.report.has("SPICE.SUMMARY", "info")
    _assert_gui_ready(res)


@pytest.mark.spice
def test_plot_png(ngspice_path, spaced_tmp):
    res = run_bench("mosfet_lowside", flyback=False)
    out = plot_result(res, spaced_tmp / "plots" / "flyback.png")
    assert out.is_file() and out.stat().st_size > 10_000
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    ac = plot_result(run_bench("rc_filter"), spaced_tmp / "rc.png")
    assert ac.stat().st_size > 10_000


# ------------------------------------------------------------------ fix round 1 -------
def test_debounce_rejects_inverted_thresholds(monkeypatch):
    _no_sim(monkeypatch)
    with pytest.raises(BenchParamError, match="v_il"):
        run_bench("button_debounce", v_il=2.5, v_ih=1.0)
    with pytest.raises(BenchParamError, match="v_il"):
        run_bench("button_debounce", v_il=1.0, v_ih=1.0)
    with pytest.raises(BenchParamError, match="v_ih"):
        run_bench("button_debounce", v_dd=3.3, v_il=0.8, v_ih=3.3)  # VIH must be below VDD


@pytest.mark.spice
def test_power_path_zero_cin_cannot_go_negative(ngspice_path):
    res = run_bench("power_path", c_in=0, t_rise=1e-7)
    assert res.measures["v_min"] >= 0.0
    assert res.measures["v_min"] < 4.63
    assert res.measures["t_below"] > 0.0  # partial samples count
    assert res.report.has("SPICE.BROWNOUT", "warning")


def test_time_below_interpolates_partial_samples():
    t = np.array([0.0, 1.0, 2.0, 3.0])
    v = np.array([5.0, 3.0, 5.0, 5.0])  # one isolated sample under the threshold of 4
    assert bench_switching._time_below(t, v, 4.0) == pytest.approx(1.0)  # 0.5 down + 0.5 up
    assert bench_switching._time_below(t, np.full(4, 5.0), 4.0) == 0.0
    assert bench_switching._time_below(t, np.full(4, 3.0), 4.0) == pytest.approx(3.0)


@pytest.mark.spice
def test_unclamped_inductive_peak_not_in_analytic_table(ngspice_path):
    bare = run_bench("mosfet_lowside", load="relay_coil_5v", flyback=False)
    assert "v_drain_peak" not in bare.analytic
    assert bare.measures["v_drain_peak"] > 30.0
    assert bare.report.has("SPICE.FLYBACK_OVERVOLTAGE", "error")


@pytest.mark.spice
def test_motor_overvoltage_warning(ngspice_path):
    res = run_bench("mosfet_lowside", load="dc_motor_small", v_supply=5.0, flyback=True)
    assert res.report.has("SPICE.MOTOR_OVERVOLTAGE", "warning")
    assert "spin-up" in benches.BENCHES["mosfet_lowside"].description
    ok = run_bench("mosfet_lowside", load="dc_motor_small", v_supply=3.0, flyback=True)
    assert not ok.report.has("SPICE.MOTOR_OVERVOLTAGE")


def test_flyback_finding_severity_depends_on_rating():
    # no ngspice: the classification alone
    def sev(v_peak, v_supply=5.0, v_max=30.0):
        rep = Report("x")
        bench_switching._flyback_finding(rep, "nmos_ao3400", v_peak, v_max, "the drain", v_supply=v_supply)
        f = rep.by_code("SPICE.FLYBACK_OVERVOLTAGE")
        return f[0].severity.label if f else None

    assert sev(33.0) == "error"  # avalanche (≥ rated V_DS)
    assert sev(30.0) == "error"
    assert sev(15.0) == "warning"  # above supply + margin, below the rating
    assert sev(5.4) is None  # clamped by a diode
    assert sev(5.0) is None  # resistive load


def test_fan_load_is_a_choice():
    assert "fan_5v" in BENCHES["mosfet_lowside"].params["load"].choices


@pytest.mark.spice
def test_fan_flyback_off_is_warning(ngspice_path):
    bare = run_bench("mosfet_lowside", load="fan_5v", flyback=False)
    assert 0.08 < bare.measures["i_load_on"] < 0.2
    assert 5.0 + 1.5 < bare.measures["v_drain_peak"] < 30.0
    assert bare.report.has("SPICE.FLYBACK_OVERVOLTAGE", "warning")
    assert not bare.report.has("SPICE.FLYBACK_OVERVOLTAGE", "error")
    assert not bare.report.errors, bare.report.to_markdown()
    clamped = run_bench("mosfet_lowside", load="fan_5v", flyback=True, diode="d1n5819")
    assert clamped.measures["v_drain_peak"] < 5.0 + 1.0
    assert clamped.report.ok and not clamped.report.has("SPICE.FLYBACK_OVERVOLTAGE")
