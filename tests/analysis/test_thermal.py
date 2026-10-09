"""Validation tests for the enclosure thermal model (``piforge.analysis.thermal``).

The model is a steady-state lumped energy balance, so the tests check conservation, monotonicity,
limits, a hand-calculated reference case and the decision thresholds of the findings.
"""

from __future__ import annotations

import dataclasses
import json
import math
import subprocess
import sys

import pytest

from piforge.analysis.thermal import (
    CD_VENT,
    CFM_TO_M3S,
    CP_AIR,
    DT_MAX,
    FAN_30MM_STATIC_PA,
    FAN_DERATE,
    LEAK_MM2,
    P_ATM,
    PI_THERMAL,
    R_AIR,
    ThermalInputs,
    ThermalResult,
    enclosure_temperature,
    get_pi_thermal,
    softening_limits,
)
from piforge.core.errors import NotFoundError, ValidationError
from piforge.core.report import Severity
from piforge.fab.profiles import get_material

# The reference case of the brief: a closed 120 × 80 × 40 mm PETG box, 2 mm walls, 4 W inside.
REF = dict(power_w=4.0, outer_mm=(120.0, 80.0, 40.0), wall_mm=2.0, material="PETG")
VENTS = dict(vent_in_mm2=400.0, vent_out_mm2=400.0, vent_height_mm=30.0)
BIG_VENTS = dict(vent_in_mm2=800.0, vent_out_mm2=800.0, vent_height_mm=30.0)  # ≥ 790 mm² for 5 cfm


def _fan_op(cfm: float, a_in_mm2: float, a_out_mm2: float, p_max: float = FAN_30MM_STATIC_PA,
            ambient_c: float = 25.0) -> float:
    """Independent fan/system-curve intersection, m³/s (quadratic formula), capped at the derating.

    Fan: p = p_max·(1 − V/V_free); openings in series: p = ρ/2·(V/(C_d·A_eff))².
    """
    rho = P_ATM / (R_AIR * (ambient_c + 273.15))
    a_in, a_out = max(a_in_mm2, LEAK_MM2) * 1e-6, max(a_out_mm2, LEAK_MM2) * 1e-6
    a_eff = 1.0 / math.sqrt(1.0 / a_in**2 + 1.0 / a_out**2)
    v_free = cfm * CFM_TO_M3S
    k, b = rho / (2.0 * (CD_VENT * a_eff) ** 2), p_max / v_free
    v = (-b + math.sqrt(b * b + 4.0 * k * p_max)) / (2.0 * k)
    assert p_max * (1 - v / v_free) == pytest.approx(k * v * v)  # on both curves
    return min(v, FAN_DERATE * v_free)


def ref(**kw) -> ThermalInputs:
    return ThermalInputs(**{**REF, **kw})


def run(*, board: str | None = None, **kw) -> ThermalResult:
    """Enclosure physics only (no Pi) unless a board is given."""
    return enclosure_temperature(ref(**kw), board=board)


def _at_internal(target_c: float, **kw) -> ThermalResult:
    """Shift the ambient so the internal air lands on ``target_c`` (ΔT hardly depends on it)."""
    r = run(**kw)
    for _ in range(4):
        kw["ambient_c"] = kw.get("ambient_c", 25.0) + (target_c - r.internal_c)
        r = run(**kw)
    return r


def _strictly_decreasing(xs: list[float]) -> bool:
    return all(a > b for a, b in zip(xs, xs[1:]))


# -- physics validation --------------------------------------------------------------------------

def test_closed_reference_box_is_physically_plausible():
    # Hand calculation — sealed 120×80×40 mm PETG box, 2 mm walls, 4 W, 25 °C, on a desk:
    #   outer areas: sides 2·(0.12+0.08)·0.04 = 0.016 m², top 0.0096 m², bottom 0.0096 m²
    #   outside: h_conv ≈ 1.42·(ΔT_o/L)^¼ = 1.42·(11/0.04)^¼ ≈ 5.8 W/m²K (vertical, laminar),
    #            h_rad ≈ 4σεT³ = 4·5.67e-8·0.9·304³ ≈ 5.7 W/m²K  →  h_o ≈ 11.5 W/m²K
    #   inside:  h_i ≈ 1.42·(20/0.036)^¼ ≈ 6.9 W/m²K (natural convection of the air only)
    #   wall:    t/k = 0.002/0.20 = 0.010 m²K/W
    #   U = 1/(1/6.9 + 0.010 + 1/11.5) ≈ 4.1 W/m²K over sides + top (0.0256 m²) → 0.105 W/K;
    #   bottom: floor convection (≈ 3 W/m²K·0.0088 m²) in series with the desk
    #   (2·k·D ≈ 2·0.15·0.11 ≈ 0.033 W/K) → ≈ 0.015 W/K.  ΔT ≈ 4 W / 0.12 W/K ≈ 33 K.
    #   Cross-check: the enclosure-industry rule k ≈ 3.5 W/m²K for sealed plastic boxes over
    #   all six faces gives 4 / (3.5·0.0352) ≈ 32 K.  15–45 K brackets both with margin.
    r = run()
    assert 15.0 <= r.delta_c <= 45.0
    assert r.internal_c == pytest.approx(25.0 + r.delta_c)
    assert r.q_vents_w == 0.0 and r.airflow_m3s == 0.0
    assert r.q_walls_w == pytest.approx(4.0, rel=1e-3)
    assert r.report.title == "thermal"


def test_bottom_on_a_desk_is_mostly_insulated():
    # The bottom conducts into a wooden desk (2·k·D spreading conductance) instead of convecting and
    # radiating: it passes less heat per area than the top, yet clearly more than nothing.
    r = run()
    faces = r.details["faces"]
    flux = {name: f["q_w"] / f["area_mm2"] for name, f in faces.items()}
    assert flux["bottom"] < 0.6 * flux["top"]
    assert faces["bottom"]["q_w"] > 0.05 * REF["power_w"]
    assert faces["bottom"]["outer_c"] > faces["top"]["outer_c"]  # the desk side runs hottest
    assert sum(f["q_w"] for f in faces.values()) == pytest.approx(r.q_walls_w)


@pytest.mark.parametrize(
    "kw",
    [
        {},
        VENTS,
        dict(VENTS, fan_cfm=5.0),
        dict(fan_cfm=2.0),
        dict(power_w=0.3),
        dict(power_w=15.0, ambient_c=35.0),
        dict(outer_mm=(60.0, 40.0, 20.0), power_w=2.0, material="PLA"),
        dict(outer_mm=(250.0, 200.0, 120.0), wall_mm=3.0, power_w=12.0, vent_in_mm2=200.0,
             vent_out_mm2=900.0, vent_height_mm=100.0),
    ],
    ids=["closed", "vented", "fan", "fan-no-vents", "low-power", "hot", "tiny-pla", "large"],
)
def test_energy_balance_closes(kw):
    r = run(**kw)
    p = kw.get("power_w", REF["power_w"])
    assert r.q_walls_w + r.q_vents_w == pytest.approx(p, rel=0.01)
    assert r.q_walls_w >= 0.0 and r.q_vents_w >= 0.0


@pytest.mark.parametrize("kw", [{}, VENTS, dict(VENTS, fan_cfm=5.0)],
                         ids=["closed", "vented", "fan"])
def test_zero_power_means_no_temperature_rise(kw):
    r = run(power_w=0.0, **kw)
    assert r.delta_c == pytest.approx(0.0, abs=1e-6)
    assert r.internal_c == pytest.approx(25.0, abs=1e-6)
    assert r.q_walls_w == pytest.approx(0.0, abs=1e-9)
    assert r.q_vents_w == pytest.approx(0.0, abs=1e-9)
    assert r.report.has("THERMAL.OK")


def test_two_400mm2_vents_30mm_apart_lower_delta_noticeably():
    closed = run()
    vented = run(**VENTS)
    assert vented.airflow_m3s > 0.0 and vented.q_vents_w > 0.5
    assert vented.delta_c <= closed.delta_c - 5.0
    assert vented.delta_c <= 0.85 * closed.delta_c


def test_30mm_fan_brings_delta_under_10_c():
    r = run(fan_cfm=5.0, **VENTS)
    assert r.delta_c < 10.0
    # 2 × 400 mm² openings throttle the fan slightly below its derated free-air flow
    assert r.airflow_m3s == pytest.approx(_fan_op(5.0, 400.0, 400.0), rel=1e-6)
    assert 0.9 * FAN_DERATE * 5.0 * CFM_TO_M3S < r.airflow_m3s < FAN_DERATE * 5.0 * CFM_TO_M3S


def test_fan_through_tiny_vents_is_not_a_free_lunch():
    # The fan must push its air through the openings: 20/20 mm² allows ≈ 0.08 L/s, not 1.2 L/s.
    tiny = run(fan_cfm=5.0, vent_in_mm2=20.0, vent_out_mm2=20.0, vent_height_mm=30.0)
    assert tiny.delta_c >= 10.0
    assert tiny.airflow_m3s == pytest.approx(_fan_op(5.0, 20.0, 20.0), rel=1e-6)
    assert tiny.report.has("THERMAL.FAN_STARVED", "warning")
    good = run(fan_cfm=5.0, **VENTS).delta_c
    for vin, vout in ((400.0, 0.0), (0.0, 400.0), (5.0, 5.0), (5.0, 0.0)):
        r = run(fan_cfm=5.0, vent_in_mm2=vin, vent_out_mm2=vout, vent_height_mm=30.0)
        assert r.delta_c > good + 5.0, (vin, vout)
        assert r.report.has("THERMAL.FAN_STARVED")


def test_fan_flow_rises_with_vent_area_and_saturates_at_derated_flow():
    derated = FAN_DERATE * 5.0 * CFM_TO_M3S
    areas = (0.0, 10.0, 20.0, 50.0, 100.0, 200.0, 400.0, 800.0, 1600.0, 5000.0)
    flows = [run(fan_cfm=5.0, vent_in_mm2=a, vent_out_mm2=a, vent_height_mm=30.0)
             .details["fan_m3s"] for a in areas]
    assert all(a <= b for a, b in zip(flows, flows[1:]))
    assert flows[0] == flows[1] == flows[2] == pytest.approx(_fan_op(5.0, 0.0, 0.0))  # gap floor
    assert all(a < b for a, b in zip(flows[2:7], flows[3:8]))  # strictly rising above the floor
    assert flows[-3:] == pytest.approx([derated] * 3)  # saturated: derated free-air flow


def test_fan_static_pressure_is_a_parameter():
    weak = run(fan_cfm=5.0, fan_static_pa=20.0, vent_in_mm2=100.0, vent_out_mm2=100.0)
    strong = run(fan_cfm=5.0, vent_in_mm2=100.0, vent_out_mm2=100.0)
    assert weak.airflow_m3s == pytest.approx(_fan_op(5.0, 100.0, 100.0, p_max=20.0), rel=1e-6)
    assert weak.airflow_m3s < strong.airflow_m3s
    assert run(fan_cfm=5.0, fan_static_pa=0.0, **VENTS).details["fan_m3s"] == 0.0


def test_vertical_plate_correlation_reproduces_textbook_example():
    # Incropera & DeWitt, Example 9.2: glass fireplace door L = 0.71 m at 232 °C in a 23 °C room
    # → Ra ≈ 1.8e9, Nu ≈ 147 (Churchill–Chu), h ≈ 7.0 W/m²K.
    from piforge.analysis.thermal import _h_vertical

    ts, ta = 232.0 + 273.15, 23.0 + 273.15
    assert _h_vertical(ts - ta, 0.71, 0.5 * (ts + ta)) == pytest.approx(7.0, rel=0.05)


def test_stack_flow_matches_orifice_equation():
    r = run(vent_in_mm2=300.0, vent_out_mm2=600.0, vent_height_mm=35.0)
    a_eff = 1.0 / math.sqrt(1.0 / 300e-6**2 + 1.0 / 600e-6**2)  # inlet and outlet in series
    t_in_k = r.internal_c + 273.15
    expected = CD_VENT * a_eff * math.sqrt(2.0 * 9.80665 * 0.035 * r.delta_c / t_in_k)
    assert r.airflow_m3s == pytest.approx(expected, rel=1e-9)
    rho_ambient = P_ATM / (R_AIR * (25.0 + 273.15))
    assert r.q_vents_w == pytest.approx(rho_ambient * CP_AIR * r.airflow_m3s * r.delta_c, rel=1e-9)


def test_vents_need_both_openings_and_a_height_difference():
    for kw in (dict(vent_in_mm2=400.0, vent_out_mm2=400.0, vent_height_mm=0.0),
               dict(vent_in_mm2=400.0, vent_out_mm2=0.0, vent_height_mm=30.0)):
        r = run(**kw)
        assert r.airflow_m3s == 0.0 and r.q_vents_w == 0.0
        assert r.delta_c == pytest.approx(run().delta_c)


def test_fan_flow_replaces_stack_flow_only_when_larger():
    stack = run(**VENTS)
    weak = run(fan_cfm=0.01, **VENTS)  # ≈ 2.4e-6 m³/s after derating: below the chimney flow
    assert weak.airflow_m3s == pytest.approx(stack.airflow_m3s, rel=1e-6)
    assert weak.delta_c == pytest.approx(stack.delta_c, rel=1e-6)
    strong = run(fan_cfm=5.0, **VENTS)
    assert strong.airflow_m3s == pytest.approx(_fan_op(5.0, 400.0, 400.0), rel=1e-6)
    assert strong.details["airflow_source"] == "fan" and weak.details["airflow_source"] == "stack"


# -- monotonicity ------------------------------------------------------------------------------

def test_monotonic_in_vent_area():
    d = [run(vent_in_mm2=a, vent_out_mm2=a, vent_height_mm=30.0).delta_c
         for a in (0.0, 50.0, 100.0, 200.0, 400.0, 800.0, 1600.0)]
    assert _strictly_decreasing(d)


def test_monotonic_in_vent_height():
    d = [run(vent_in_mm2=400.0, vent_out_mm2=400.0, vent_height_mm=h).delta_c
         for h in (5.0, 10.0, 20.0, 40.0, 80.0)]
    assert _strictly_decreasing(d)


def test_monotonic_in_fan_flow():
    d = [run(fan_cfm=c, **VENTS).delta_c for c in (0.0, 0.5, 1.0, 2.0, 5.0, 10.0)]
    assert _strictly_decreasing(d)


def test_monotonic_in_power():
    d = [run(power_w=p, **VENTS).delta_c for p in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)]
    assert _strictly_decreasing(d[::-1])


def test_monotonic_in_wall_conductivity():
    petg = get_material("PETG")
    d = [run(material=dataclasses.replace(petg, thermal_conductivity=k)).delta_c
         for k in (0.05, 0.1, 0.2, 0.5, 2.0, 50.0)]
    assert _strictly_decreasing(d)


def test_thicker_walls_run_hotter_and_bigger_boxes_cooler():
    walls = [run(wall_mm=t).delta_c for t in (1.0, 2.0, 4.0, 8.0)]
    assert _strictly_decreasing(walls[::-1])
    sizes = [run(outer_mm=(120.0 * s, 80.0 * s, 40.0 * s)).delta_c for s in (0.75, 1.0, 1.5, 2.0)]
    assert _strictly_decreasing(sizes)


# -- material softening ------------------------------------------------------------------------

def test_softening_limits_follow_material_data():
    petg = get_material("PETG")
    assert softening_limits("PETG") == (petg.max_service_c - 5.0, petg.glass_transition_c - 5.0)
    assert softening_limits(petg) == softening_limits("petg")
    # TPU (elastomer, Tg −30 °C) and PA-CF (semi-crystalline) do not soften at Tg: the error limit
    # falls back to the rated service temperature instead of flagging every enclosure.
    for name in ("TPU95A", "PA-CF"):
        warn, err = softening_limits(name)
        assert err == get_material(name).max_service_c and err > warn


@pytest.mark.parametrize(
    "target_c, severity",
    [(55.0, None), (70.0, Severity.WARNING), (80.0, Severity.ERROR)],
    ids=["below", "warning", "error"],
)
def test_material_softening_thresholds_petg(target_c, severity):
    # PETG: max service 70 °C, Tg 80 °C → WARNING above 65 °C, ERROR at/above 75 °C.
    r = _at_internal(target_c)
    assert r.internal_c == pytest.approx(target_c, abs=0.5)
    found = r.report.by_code("THERMAL.MATERIAL_SOFTENING")
    if severity is None:
        assert not found and r.report.has("THERMAL.OK")
        return
    assert [f.severity for f in found] == [severity]
    f = found[0]
    assert "°C" in f.message and "PETG" in f.message
    assert "mm²" in f.hint and "fan" in f.hint
    assert f.data["internal_c"] == pytest.approx(r.internal_c)
    assert not r.report.has("THERMAL.OK")


def test_softening_hint_suggests_vents_fan_and_better_materials():
    r = run(material="PLA", power_w=4.0)  # ≈ 59 °C inside, PLA softens at 55 °C
    f = r.report.by_code("THERMAL.MATERIAL_SOFTENING")[0]
    assert f.severity == Severity.ERROR
    assert "mm²" in f.hint and "30 mm fan" in f.hint
    assert any(name in f.hint for name in ("PETG", "ASA", "ABS", "PC"))
    assert "PLA" not in f.hint.split("print in")[-1]


# -- Raspberry Pi SoC ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "soc_c, severity",
    [(70.0, None), (82.0, Severity.WARNING), (90.0, Severity.ERROR)],
    ids=["cool", "throttle", "hard-limit"],
)
def test_pi_throttle_thresholds(soc_c, severity):
    internal = run().internal_c
    r = enclosure_temperature(ref(), board="rpi4b", soc_rise_c=soc_c - internal)
    assert r.soc_c == pytest.approx(soc_c, abs=1e-6)
    found = r.report.by_code("THERMAL.PI_THROTTLE")
    if severity is None:
        assert not found
        return
    assert [f.severity for f in found] == [severity]
    f = found[0]
    assert "°C" in f.message and f.subject == "board:rpi4b"
    assert f.hint and ("heatsink" in f.hint or "fan" in f.hint)
    assert f.data["soc_c"] == pytest.approx(soc_c, abs=1e-6)


def test_pi3bp_soft_limit_is_reported_as_info():
    internal = run().internal_c
    r = enclosure_temperature(ref(), board="rpi3bp", soc_rise_c=65.0 - internal)
    found = r.report.by_code("THERMAL.PI_THROTTLE")
    assert [f.severity for f in found] == [Severity.INFO]
    assert "60" in found[0].message and "1.2 GHz" in found[0].message


def test_no_board_means_no_soc_estimate():
    r = enclosure_temperature(ref(power_w=30.0), board=None)
    assert r.soc_c is None
    assert not r.report.has("THERMAL.PI_THROTTLE")


def test_default_soc_rise_scales_with_power_and_cooling():
    pi = PI_THERMAL["rpi4b"]
    half = enclosure_temperature(ref(power_w=pi.load_w / 2), board="rpi4b")
    assert half.soc_c - half.internal_c == pytest.approx(pi.soc_rise("bare") / 2)
    over = enclosure_temperature(ref(power_w=2 * pi.load_w), board="rpi4b")
    assert over.soc_c - over.internal_c == pytest.approx(pi.soc_rise("bare"))
    hs = enclosure_temperature(ref(power_w=pi.load_w), board="rpi4b", cooling="heatsink")
    assert hs.soc_c - hs.internal_c == pytest.approx(pi.soc_rise("heatsink"))
    # a fan in the box defaults to the "fan" SoC rise (forced air over the board)
    fan = enclosure_temperature(ref(power_w=pi.load_w, fan_cfm=5.0, **VENTS), board="rpi4b")
    assert fan.soc_c - fan.internal_c == pytest.approx(pi.soc_rise("fan"))
    assert fan.details["cooling"] == "fan"


def test_ventilated_fan_cooled_pi4_box_is_ok():
    r = enclosure_temperature(ref(power_w=6.4, fan_cfm=5.0, **BIG_VENTS), board="rpi4b")
    assert r.report.ok and not r.report.warnings
    ok = r.report.by_code("THERMAL.OK")
    assert len(ok) == 1 and ok[0].severity == Severity.INFO
    assert "°C" in ok[0].message and "W" in ok[0].message and ok[0].hint
    assert ok[0].data["soc_c"] == pytest.approx(r.soc_c)


@pytest.mark.parametrize("key", ["rpi5", "rpi4b", "rpi3bp", "rpizero2w"])
def test_pi_thermal_table_is_physical(key):
    pi = PI_THERMAL[key]
    assert pi.board == key
    assert 0.0 < pi.idle_w < pi.load_w <= 12.0
    assert pi.soc_rise("bare") >= pi.soc_rise("heatsink") > pi.soc_rise("fan") > 0.0
    assert pi.throttle_c == 80.0 and pi.limit_c == 85.0
    row = pi.to_dict()
    assert row["board"] == key and row["load_w"] == pi.load_w and row["source"]
    json.dumps(row)


def test_soft_limit_only_on_pi3bp():
    assert PI_THERMAL["rpi3bp"].soft_limit_c == 60.0
    assert all(PI_THERMAL[k].soft_limit_c is None for k in ("rpi5", "rpi4b", "rpizero2w"))


def test_board_lookup_is_forgiving_and_suggests():
    assert get_pi_thermal("RPi4") is PI_THERMAL["rpi4b"]
    assert get_pi_thermal("pi5") is PI_THERMAL["rpi5"]
    assert get_pi_thermal("rpi3b+") is PI_THERMAL["rpi3bp"]
    assert get_pi_thermal("Zero 2 W") is PI_THERMAL["rpizero2w"]
    with pytest.raises(NotFoundError, match="rpi4b"):
        enclosure_temperature(ref(), board="rpi4c")


# -- input sanity findings ---------------------------------------------------------------------

def test_starved_fan_warns():
    r = run(fan_cfm=5.0)  # no vents at all: only port/lid gaps feed the fan
    found = r.report.by_code("THERMAL.FAN_STARVED")
    assert [f.severity for f in found] == [Severity.WARNING]
    f = found[0]
    assert "mm²" in f.hint and "cfm" in f.message and "L/s" in f.message and "%" in f.message
    assert f.data["fan_m3s"] == pytest.approx(_fan_op(5.0, 0.0, 0.0), rel=1e-6)
    assert f.data["need_mm2"] == 790.0 and str(int(f.data["need_mm2"])) in f.hint
    assert not run(**VENTS).report.has("THERMAL.FAN_STARVED")  # no fan, no fan warning


@pytest.mark.parametrize(
    "area, starved",
    [(20.0, True), (100.0, True), (250.0, True), (350.0, False), (400.0, False), (800.0, False)],
)
def test_fan_starved_is_flow_based(area, starved):
    # Warn when the fan achieves < 80 % of its derated free-air flow: 2 × 400 mm² with the 30 mm
    # reference fan gets 95 % and must stay quiet even though 790 mm² is the recommended size.
    r = run(fan_cfm=5.0, vent_in_mm2=area, vent_out_mm2=area, vent_height_mm=30.0)
    fraction = r.details["fan_m3s"] / (FAN_DERATE * 5.0 * CFM_TO_M3S)
    assert (fraction < 0.8) is starved
    found = r.report.by_code("THERMAL.FAN_STARVED")
    assert bool(found) is starved
    if starved:
        assert found[0].data["flow_fraction"] == pytest.approx(fraction)
        assert "790 mm²" in found[0].hint


def test_power_below_pi_idle_warns():
    r = enclosure_temperature(ref(power_w=1.0), board="rpi4b")
    found = r.report.by_code("THERMAL.POWER_BELOW_IDLE")
    assert [f.severity for f in found] == [Severity.WARNING]
    assert "W" in found[0].message and found[0].hint
    assert not enclosure_temperature(ref(power_w=3.0), board="rpi4b").report.has(
        "THERMAL.POWER_BELOW_IDLE")


def test_every_problem_finding_has_numbers_and_a_hint():
    # PLA box, Pi 5 at full load, a weak fan and no vents: softening + throttling + fan warning
    r = enclosure_temperature(ref(material="PLA", power_w=8.8, fan_cfm=0.5), board="rpi5")
    assert len(r.report.errors) == 2 and r.report.has("THERMAL.FAN_STARVED")
    for f in r.report:
        assert "°C" in f.message or "W" in f.message
        if f.severity >= Severity.WARNING:
            assert f.hint


# -- usage errors ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kw",
    [
        dict(power_w=-1.0),
        dict(power_w=float("nan")),
        dict(outer_mm=(120.0, 80.0)),
        dict(outer_mm=(120.0, 0.0, 40.0)),
        dict(wall_mm=0.0),
        dict(wall_mm=20.0),
        dict(vent_in_mm2=-1.0),
        dict(vent_height_mm=-5.0),
        dict(fan_cfm=-0.1),
        dict(fan_static_pa=-1.0),
        dict(ambient_c=-300.0),
    ],
    ids=["neg-power", "nan-power", "2d", "zero-dim", "zero-wall", "no-cavity", "neg-vent",
         "neg-height", "neg-fan", "neg-static", "below-0K"],
)
def test_invalid_inputs_raise(kw):
    with pytest.raises(ValidationError):
        enclosure_temperature(ref(**kw))


@pytest.mark.parametrize("kw", [dict(outer_mm=(5.0, 5.0, 5.0)), dict(power_w=500.0)],
                         ids=["5mm-box-4W", "500W"])
def test_impossible_heat_load_is_a_finding_not_an_exception(kw):
    # Valid inputs whose heat cannot be shed even DT_MAX above ambient (formerly a ValidationError):
    # the result is clamped and flagged, like any other design problem.
    r = enclosure_temperature(ref(**kw), board="rpi4b")
    found = r.report.by_code("THERMAL.OVERTEMP")
    assert [f.severity for f in found] == [Severity.ERROR]
    assert "W" in found[0].message and "°C" in found[0].message
    assert "check" in found[0].hint.lower() and "power_w" in found[0].hint
    assert r.delta_c == pytest.approx(DT_MAX) and r.details["clamped"]
    assert r.q_walls_w + r.q_vents_w < r.details["power_w"]  # energy cannot balance
    assert r.report.has("THERMAL.MATERIAL_SOFTENING", "error")
    assert not r.report.has("THERMAL.OK") and not r.report.ok
    assert not run().details["clamped"]


def test_invalid_options_raise():
    with pytest.raises(ValidationError):
        enclosure_temperature(ref(), cooling="water")
    with pytest.raises(ValidationError):
        enclosure_temperature(ref(), soc_rise_c=-1.0)
    with pytest.raises(NotFoundError):
        enclosure_temperature(ref(material="unobtainium"))


# -- packaging ---------------------------------------------------------------------------------

def test_result_is_json_serialisable():
    r = enclosure_temperature(ref(**VENTS), board="rpi4b")
    d = r.to_dict()
    assert d["internal_c"] == pytest.approx(r.internal_c)
    assert set(d["details"]["faces"]) == {"sides", "top", "bottom"}
    json.dumps(d)


def test_cited_data_lives_in_thermal_data_and_is_reexported():
    from piforge.analysis import thermal, thermal_data

    for name in ("PI_THERMAL", "PiThermal", "get_pi_thermal", "COOLING", "CFM_TO_M3S", "DT_MAX",
                 "FAN_DERATE", "FAN_30MM_STATIC_PA", "LEAK_MM2", "CD_VENT", "P_ATM", "R_AIR"):
        assert getattr(thermal, name) is getattr(thermal_data, name), name
        assert name in thermal.__all__


def test_thermal_does_not_import_cad_kernel():
    code = ("import sys, piforge.analysis.thermal\n"
            "bad = [m for m in ('build123d', 'OCP') if m in sys.modules]\n"
            "assert not bad, bad\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-2000:]
