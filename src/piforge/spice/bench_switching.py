"""Benches for switching and supply circuits: MOSFET/BJT low-side switches (with or without a
flyback diode), BSS138 bidirectional level shifter, I2C rise time and PSU/cable brown-out."""

from __future__ import annotations

import math

import numpy as np

from piforge.core.report import Report
from piforge.spice.bench_basic import DRIVE_SPEC
from piforge.spice.benchkit import (
    GPIO_SAFE_A,
    PI_VDD_IO,
    Bench,
    ParamSpec,
    add_gpio,
    at,
    crossing,
    mA,
    step_wave,
    summary,
)
from piforge.spice.models import MODEL_INFO, gpio_resistances
from piforge.spice.netlist import PWL, Pulse, SpiceCircuit, fmt_value
from piforge.spice.runner import SimResult

MOSFETS = ("nmos_ao3400", "nmos_irlz44n", "nmos_2n7000", "nmos_bss138", "nmos_irf540n")
BJTS = ("q2n2222", "q2n3904")
DIODES = ("d1n4148", "d1n4007", "d1n5819")
_RELAY = MODEL_INFO["relay_coil_5v"].ratings
_FAN = MODEL_INFO["fan_5v"].ratings
# Turn-off overshoot that counts as "unclamped": a flyback diode holds the switch node at supply +
# V_F (≤ ≈ 1 V for 1N4148/1N4007/1N5819 at these currents), so a peak more than 1.5 V above the
# supply means the inductive kick is not clamped. src: est — max V_F of the library diodes + margin
FLYBACK_MARGIN_V = 1.5
# src: Songle SRD-05VDC-SL-C — must-operate (pick-up) voltage ≤ 75 % of 5 V → 3.75 V on 70 Ω.
_I_PULLIN = _RELAY["v_pickup"] / _RELAY["r_coil"]
# textbook forward drops for the analytic flyback clamp: silicon ≈ 0.7 V, Schottky ≈ 0.4 V
_VF_TEXTBOOK = {"d1n4148": 0.7, "d1n4007": 0.7, "d1n5819": 0.4}
_T_ON_AT = 0.5e-3  # GPIO switches the load on here


def _sw_times(p: dict) -> tuple[float, float, float]:
    t2 = _T_ON_AT + p["t_on"]
    after = 50e-3 if p.get("load") == "dc_motor_small" else 10e-3
    return _T_ON_AT, t2, t2 + after


def _switch_tran(c: SpiceCircuit, p: dict, t_end: float) -> None:
    tm = min(20e-6, max(p["t_on"] / 500.0, 2e-6))
    # Gear integration: the trapezoidal default leaves a non-physical point-to-point ringing in
    # the coil current after the inductive turn-off (verified: ±2.4 mA with trap, 1e-7 mA gear).
    c.option(method="gear")
    c.tran(tm, t_end, tmax=tm)


def _add_load(c: SpiceCircuit, p: dict, top: str, bottom: str) -> None:
    if p["load"] == "resistor":
        c.R("load", top, bottom, p["r_load"])
    else:
        c.X("load", [top, bottom], p["load"])


def _load_r_dc(p: dict) -> float | None:
    if p["load"] == "resistor":
        return p["r_load"]
    if p["load"] == "relay_coil_5v":
        return _RELAY["r_coil"]
    if p["load"] == "fan_5v":
        return _FAN["r_dc"]
    return None  # motor: current set by back-EMF, no simple DC resistance


def _on_point(p: dict) -> float:
    return _sw_times(p)[1] - min(0.05 * p["t_on"], 0.5e-3)


def _after_off(sim: SimResult, vec: str, t_off: float) -> np.ndarray:
    tr = sim.plot("tran")
    return np.asarray(tr[vec])[tr["time"] >= t_off]


def _decay_time(sim: SimResult, t_off: float, i_on: float) -> float:
    if i_on <= 1e-6:
        return math.nan
    tr = sim.plot("tran")
    return crossing(tr["time"], tr["i(vsense)"], 0.1 * i_on, rising=False, after=t_off) - t_off


def _flyback_finding(rep: Report, part: str, v_peak: float, v_max: float, what: str, *,
                     v_supply: float) -> None:
    """Turn-off spike: ERROR at/above the switch rating (avalanche / breakdown), WARNING when it is
    unclamped (more than :data:`FLYBACK_MARGIN_V` above the supply) but still below the rating."""
    hint = ("add a flyback diode across the load (cathode to +V): 1N4148/1N4007 for relays, "
            "1N5819 for motors and fans")
    if v_peak >= v_max:
        rep.add("SPICE.FLYBACK_OVERVOLTAGE", "error",
                f"{what} reaches {v_peak:.1f} V at turn-off — at or above the {part} rating of {v_max:g} V "
                f"(avalanche)", subject=f"part:{part}", hint=hint, v_peak=v_peak, v_max=v_max,
                v_supply=v_supply)
    elif v_peak > v_supply + FLYBACK_MARGIN_V:
        rep.add("SPICE.FLYBACK_OVERVOLTAGE", "warning",
                f"{what} rings up to {v_peak:.1f} V at turn-off ({v_peak - v_supply:.1f} V above the "
                f"{v_supply:g} V supply) — unclamped, but below the {part} rating of {v_max:g} V",
                subject=f"part:{part}", hint=hint, v_peak=v_peak, v_max=v_max, v_supply=v_supply)


def _relay_finding(rep: Report, p: dict, i_on: float) -> None:
    if p["load"] == "relay_coil_5v" and i_on < _I_PULLIN:
        rep.add("SPICE.RELAY_NO_PULLIN", "error",
                f"coil current {mA(i_on)} is below the {mA(_I_PULLIN)} needed to pull the relay in "
                f"(75 % of 5 V on 70 Ω)", subject="part:relay",
                hint="the switch is not fully on — fix the drive (see other findings)", i=i_on)


# -------------------------------------------------------------------- MOSFET low-side -------
def _ls_build(p: dict) -> SpiceCircuit:
    t1, t2, t_end = _sw_times(p)
    c = SpiceCircuit(f"Low-side {p['mosfet']} switching {p['load']} from a Pi GPIO")
    c.V("dd", "vdd", "0", p["v_supply"])
    c.V("io", "vio", "0", PI_VDD_IO)
    add_gpio(c, "1", "pin", step_wave(t1, t2, PI_VDD_IO), drive_ma=p["drive_ma"])
    c.V("gsense", "pin", "gx", 0.0)
    gate = "g" if p["r_gate"] > 0 else "gx"
    if p["r_gate"] > 0:
        c.R("g", "gx", "g", p["r_gate"])
    c.R("pd", gate, "0", p["r_pulldown"])
    c.V("sense", "vdd", "lt", 0.0)  # load current, also while it circulates through the diode
    _add_load(c, p, "lt", "d")
    c.M("1", "d", gate, "0", p["mosfet"])
    if p["flyback"]:
        c.D("fly", "d", "vdd", p["diode"])
    _switch_tran(c, p, t_end)
    return c


def _ls_gate(p: dict) -> str:
    return "v(g)" if p["r_gate"] > 0 else "v(gx)"


def _ls_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    _t1, t2, _t_end = _sw_times(p)
    tm = _on_point(p)
    key = p["mosfet"]
    rating = MODEL_INFO[key].ratings
    i_on, v_ds = at(sim, "i(vsense)", tm), at(sim, "v(d)", tm)
    tr = sim.plot("tran")
    r_ds = v_ds / i_on if i_on > 1e-9 else math.inf
    v_peak = float(_after_off(sim, "v(d)", t2).max())
    i_peak = float(np.max(tr["i(vsense)"]))
    m = {"i_load_on": i_on, "v_ds_on": v_ds, "r_ds_on": r_ds, "v_drain_peak": v_peak,
         "p_mosfet": v_ds * i_on, "i_peak": i_peak, "i_gate_peak": float(np.max(np.abs(tr["i(vgsense)"]))),
         "t_off": _decay_time(sim, t2, i_on)}
    a: dict[str, float] = {}
    r_dc = _load_r_dc(p)
    if r_dc is not None:
        a["i_load_on"] = p["v_supply"] / r_dc  # ideal switch
    if p["flyback"]:
        a["v_drain_peak"] = p["v_supply"] + _VF_TEXTBOOK[p["diode"]]
    elif p["load"] == "resistor":
        a["v_drain_peak"] = p["v_supply"]
    # unclamped L·di/dt has no closed form: the peak is set by the avalanche model (BV = 1.1 × rating,
    # circular as an "analytic" value), so it is only a model check below, not in the analytic table.
    rep = Report("SPICE MOSFET low-side switch")
    _flyback_finding(rep, key, v_peak, rating["vds_max"], "the drain", v_supply=p["v_supply"])
    if not p["flyback"] and p["load"] != "resistor" and v_peak >= rating["vds_max"]:
        rep.add("SPICE.MODEL_CHECK", "info",
                f"unclamped drain peak {v_peak:.1f} V is set by the MOSFET avalanche model "
                f"(BV = 1.1 × {rating['vds_max']:g} V = {1.1 * rating['vds_max']:.1f} V); it is a model "
                f"check, not an independent analytic value", subject=f"part:{key}",
                v_peak=v_peak, bv_model=1.1 * rating["vds_max"])
    v_motor = MODEL_INFO["dc_motor_small"].ratings["v_max"]
    if p["load"] == "dc_motor_small" and p["v_supply"] > v_motor:
        rep.add("SPICE.MOTOR_OVERVOLTAGE", "warning",
                f"the {p['v_supply']:g} V supply exceeds the {v_motor:g} V maximum of the small DC motor "
                f"model (FA-130RA-2270, 1.5-3 V)", subject="part:motor",
                hint="use a lower supply, a series resistor or PWM to limit the average voltage",
                v_supply=p["v_supply"], v_max=v_motor)
    if v_ds > 0.1 * p["v_supply"]:
        rep.add("SPICE.MOSFET_GATE_DRIVE", "error",
                f"{key} does not turn on from a 3.3 V GPIO: V_DS stays at {v_ds:.2f} V and the load gets "
                f"{mA(i_on)} (VGS(th) up to {rating['vgs_th'][2]:g} V)", subject=f"part:{key}",
                hint="use a logic-level MOSFET (AO3400, IRLZ44N) or a gate driver", v_ds=v_ds, i=i_on)
    elif r_ds > 2 * rating["rds_on_max"]:
        rep.add("SPICE.MOSFET_GATE_DRIVE", "warning",
                f"{key} is only partly enhanced at 3.3 V: R_DS(on) {r_ds:.3g} Ω vs ≤ "
                f"{rating['rds_on_max']:g} Ω at 10 V", subject=f"part:{key}",
                hint="a logic-level MOSFET specified at VGS ≤ 2.5 V runs cooler", r_ds=r_ds)
    _relay_finding(rep, p, i_on)
    if i_peak > rating["id_max"]:
        rep.add("SPICE.MOSFET_OVERCURRENT", "error",
                f"drain current peaks at {i_peak:.2f} A — above the {key} rating of {rating['id_max']:g} A",
                subject=f"part:{key}", hint="choose a larger MOSFET or limit the inrush", i_peak=i_peak)
    if v_ds * i_on > rating["pd_max"]:
        rep.add("SPICE.MOSFET_POWER", "warning",
                f"{key} dissipates {v_ds * i_on:.2f} W when on (≈ {rating['pd_max']:g} W without heatsink)",
                subject=f"part:{key}", hint="lower R_DS(on) (more gate drive / bigger part) or add a heatsink",
                p=v_ds * i_on)
    clamp = f"with {p['diode']}" if p["flyback"] else "no flyback diode"
    summary(rep, f"load {mA(i_on)}, V_DS(on) {v_ds * 1e3:.3g} mV (R_DS {r_ds:.3g} Ω), drain peak "
                 f"{v_peak:.2f} V at turn-off ({clamp})", i_load_on=i_on, v_drain_peak=v_peak)
    return m, a, rep


def _ls_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return ("time [s]", tr["time"], {"V(drain) [V]": tr["v(d)"], "V(gate) [V]": tr[_ls_gate(p)],
                                     "I(load) [mA]": tr["i(vsense)"] * 1e3}, "linear")


_SWITCH_PARAMS = {
    "v_supply": ParamSpec(5.0, min=1.0, max=100.0, unit="V", label="Load supply"),
    "load": ParamSpec("relay_coil_5v", choices=("relay_coil_5v", "dc_motor_small", "fan_5v", "resistor"),
                      label="Load", help="relay: Songle SRD-05VDC 70 Ω coil; motor: Mabuchi FA-130 (1.5–3 V!); "
                                         "fan: 30 mm 5 V BLDC fan (≈ 100 mA, 2 mH, 100 nF)"),
    "r_load": ParamSpec(50.0, min=0.1, max=1e6, unit="Ω", label="Load resistance (load = resistor)"),
    "flyback": ParamSpec(True, choices=(False, True), label="Flyback diode across the load"),
    "diode": ParamSpec("d1n4148", choices=DIODES, label="Flyback diode"),
    "t_on": ParamSpec(10e-3, min=1e-4, max=0.5, unit="s", label="GPIO high time"),
    "drive_ma": DRIVE_SPEC,
}

MOSFET_LOWSIDE = Bench(
    key="mosfet_lowside",
    title="MOSFET low-side switch",
    description="Pi GPIO → gate resistor (+ pull-down) → N-MOSFET switching a relay coil, small DC motor, "
                "5 V fan or resistor, with or without a flyback diode. Shows the turn-off spike and whether a "
                "3.3 V gate is enough. With the DC motor, the default t_on = 10 ms is still the spin-up "
                "transient (the rotor has not reached steady speed).",
    params={"mosfet": ParamSpec("nmos_ao3400", choices=MOSFETS, label="MOSFET"),
            **_SWITCH_PARAMS,
            "r_gate": ParamSpec(100.0, min=0.0, max=10e3, unit="Ω", label="Gate resistor (0 = none)"),
            "r_pulldown": ParamSpec(100e3, min=1e3, max=10e6, unit="Ω", label="Gate pull-down")},
    build=_ls_build, evaluate=_ls_eval, traces=_ls_traces,
    units={"i_load_on": "A", "v_ds_on": "V", "r_ds_on": "Ω", "v_drain_peak": "V", "p_mosfet": "W",
           "i_peak": "A", "i_gate_peak": "A", "t_off": "s"},
)


# ------------------------------------------------------------------------- BJT switch -------
def _bjt_build(p: dict) -> SpiceCircuit:
    t1, t2, t_end = _sw_times(p)
    c = SpiceCircuit(f"{p['transistor']} low-side switch for {p['load']} from a Pi GPIO")
    c.V("dd", "vdd", "0", p["v_supply"])
    c.V("io", "vio", "0", PI_VDD_IO)
    add_gpio(c, "1", "pin", step_wave(t1, t2, PI_VDD_IO), drive_ma=p["drive_ma"])
    c.V("bsense", "pin", "bx", 0.0)
    c.R("b", "bx", "b", p["r_base"])
    c.V("sense", "vdd", "lt", 0.0)
    _add_load(c, p, "lt", "c")
    c.Q("1", "c", "b", "0", p["transistor"])
    if p["flyback"]:
        c.D("fly", "c", "vdd", p["diode"])
    _switch_tran(c, p, t_end)
    return c


def _bjt_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    _t1, t2, _t_end = _sw_times(p)
    tm = _on_point(p)
    key = p["transistor"]
    rating = MODEL_INFO[key].ratings
    i_b, i_c = at(sim, "i(vbsense)", tm), at(sim, "i(vsense)", tm)
    v_ce, v_be = at(sim, "v(c)", tm), at(sim, "v(b)", tm)
    v_peak = float(_after_off(sim, "v(c)", t2).max())
    m = {"i_b": i_b, "i_c": i_c, "v_ce_sat": v_ce, "v_be": v_be,
         "beta_forced": i_c / i_b if i_b > 1e-12 else math.nan, "v_c_peak": v_peak,
         "p_transistor": v_ce * i_c + v_be * i_b, "t_off": _decay_time(sim, t2, i_c)}
    rh, _rl = gpio_resistances(p["drive_ma"])
    r_dc = _load_r_dc(p) or math.inf
    ib_a = (PI_VDD_IO - 0.7) / (p["r_base"] + rh)  # textbook: VBE ≈ 0.7 V, VCE(sat) ≈ 0.2 V
    ic_a = (p["v_supply"] - 0.2) / r_dc
    a = {"i_b": ib_a, "i_c": ic_a, "beta_forced": ic_a / ib_a}
    rep = Report("SPICE BJT switch")
    i_worst = max(i_b, (PI_VDD_IO - v_be) / p["r_base"])
    if i_worst > GPIO_SAFE_A:
        rep.add("SPICE.GPIO_OVERCURRENT", "error",
                f"base current {mA(i_b)} simulated, up to {mA(i_worst)} with a strong pad — above the 16 mA "
                f"safe pad current", subject="pin:GPIO", hint="increase R_base", i_worst=i_worst)
    if v_ce > 0.4:
        r_need = (PI_VDD_IO - 0.75) / (ic_a / 10.0) - rh if math.isfinite(ic_a) and ic_a > 0 else math.nan
        rep.add("SPICE.BJT_NOT_SATURATED", "warning",
                f"{key} is not saturated: V_CE = {v_ce:.2f} V at I_C = {mA(i_c)} (I_B = {mA(i_b)})",
                subject=f"part:{key}",
                hint=f"reduce R_base to ≈ {r_need:.0f} Ω for a forced β ≈ 10" if math.isfinite(r_need)
                else "increase the base current", v_ce=v_ce)
    if i_c > rating["ic_max"]:
        rep.add("SPICE.BJT_OVERCURRENT", "error",
                f"collector current {i_c:.3f} A exceeds the {key} rating of {rating['ic_max']:g} A",
                subject=f"part:{key}", hint="use a MOSFET or a bigger transistor", i_c=i_c)
    _flyback_finding(rep, key, v_peak, rating["vceo"], "the collector", v_supply=p["v_supply"])
    _relay_finding(rep, p, i_c)
    if m["p_transistor"] > rating["pd_max"]:
        rep.add("SPICE.BJT_POWER", "warning",
                f"{key} dissipates {m['p_transistor']:.2f} W (rating {rating['pd_max']:g} W)",
                subject=f"part:{key}", hint="saturate it harder or use a MOSFET", p=m["p_transistor"])
    summary(rep, f"I_B {mA(i_b)}, I_C {mA(i_c)} (forced β {m['beta_forced']:.3g}), V_CE {v_ce * 1e3:.3g} mV, "
                 f"collector peak {v_peak:.2f} V at turn-off", i_c=i_c, v_ce=v_ce)
    return m, a, rep


def _bjt_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return ("time [s]", tr["time"], {"V(collector) [V]": tr["v(c)"], "V(base) [V]": tr["v(b)"],
                                     "I(C) [mA]": tr["i(vsense)"] * 1e3}, "linear")


BJT_SWITCH = Bench(
    key="bjt_switch",
    title="BJT low-side switch",
    description="Pi GPIO → base resistor → NPN (2N2222/2N3904) switching a relay coil or resistor, "
                "with or without a flyback diode. Checks saturation, base current and spikes.",
    params={"transistor": ParamSpec("q2n2222", choices=BJTS, label="Transistor"),
            "r_base": ParamSpec(1e3, min=10.0, max=1e6, unit="Ω", label="Base resistor"),
            **{k: v for k, v in _SWITCH_PARAMS.items() if k != "load"},
            "load": ParamSpec("relay_coil_5v", choices=("relay_coil_5v", "resistor"), label="Load")},
    build=_bjt_build, evaluate=_bjt_eval, traces=_bjt_traces,
    units={"i_b": "A", "i_c": "A", "v_ce_sat": "V", "v_be": "V", "beta_forced": "", "v_c_peak": "V",
           "p_transistor": "W", "t_off": "s"},
)


# -------------------------------------------------------------------- BSS138 level shifter --
# src: NXP UM10204 I2C-bus specification rev. 7 (2021), Table 10 — t_r max, t_f max, C_b max and
# VOL ≤ 0.4 V at the given sink current IOL.  (name, t_r max, t_f max, C_b max, IOL)
_I2C_MODES = {100e3: ("Standard-mode", 1000e-9, 300e-9, 400e-12, 3e-3),
              400e3: ("Fast-mode", 300e-9, 300e-9, 400e-12, 3e-3),
              1e6: ("Fast-mode Plus", 120e-9, 120e-9, 550e-12, 20e-3)}
_PI_I2C_PULLUP = 1.8e3  # src: Raspberry Pi schematics — GPIO2/GPIO3 have 1.8 kΩ pull-ups to 3.3 V
_LN73 = math.log(7.0 / 3.0)  # 30 % → 70 % of VDD on an RC charge curve


def _lvl_nodes(p: dict) -> tuple[str, str]:
    return ("lo", "hi") if p["direction"] == "low_to_high" else ("hi", "lo")  # (tx, rx)


def _lvl_build(p: dict) -> SpiceCircuit:
    period = 1.0 / p["freq"]
    tx, _rx = _lvl_nodes(p)
    c = SpiceCircuit(f"Bidirectional level shifter {p['v_low']:g} V <-> {p['v_high']:g} V ({p['mosfet']})")
    c.V("lv", "vl", "0", p["v_low"])
    c.V("hv", "vh", "0", p["v_high"])
    c.R("pl", "vl", "lo", p["r_pull_low"])
    c.R("ph", "vh", "hi", p["r_pull_high"])
    if p["c_low"] > 0:
        c.C("lo", "lo", "0", p["c_low"])
    if p["c_high"] > 0:
        c.C("hi", "hi", "0", p["c_high"])
    c.M("1", "hi", "vl", "lo", p["mosfet"])  # gate on the low rail, source low side, drain high side
    edge = period / 1000
    c.V("drive", "ctl", "0", Pulse(0.0, 1.0, td=0.0, tr=edge, tf=edge, pw=period / 2 - edge, per=period))
    c.S("drv", tx, "dn", "ctl", "0", "sw_ideal")  # open-drain driver: pulls low while ctl = 1
    c.R("drv", "dn", "0", p["r_driver"])
    c.tran(period / 400, 3 * period, tmax=period / 400)
    return c


def _lvl_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    period = 1.0 / p["freq"]
    tx, rx = _lvl_nodes(p)
    v_rail = p["v_high"] if rx == "hi" else p["v_low"]
    t_low, t_high = 2.5 * period - period / 50, 3 * period - period / 50
    tr = sim.plot("tran")
    t, v = tr["time"], tr[f"v({rx})"]
    # edges measured like UM10204: rise 30 % → 70 %, fall 70 % → 30 % of the receiving rail
    t_rise = (crossing(t, v, 0.7 * v_rail, True, after=2.5 * period)
              - crossing(t, v, 0.3 * v_rail, True, after=2.5 * period))
    t_fall = (crossing(t, v, 0.3 * v_rail, False, after=2 * period)
              - crossing(t, v, 0.7 * v_rail, False, after=2 * period))
    m = {"v_rx_high": at(sim, f"v({rx})", t_high), "v_rx_low": at(sim, f"v({rx})", t_low),
         "v_tx_low": at(sim, f"v({tx})", t_low), "t_rise": t_rise, "t_fall": t_fall}
    r_rx = p["r_pull_high"] if rx == "hi" else p["r_pull_low"]
    c_rx = p["c_high"] if rx == "hi" else p["c_low"]
    # both pull-up currents flow through the driver; the MOSFET's R_DS(on) is ignored
    v_low_a = p["r_driver"] * (p["v_low"] / p["r_pull_low"] + p["v_high"] / p["r_pull_high"])
    a = {"v_rx_high": v_rail, "v_rx_low": v_low_a, "v_tx_low": v_low_a}
    if c_rx > 0:
        a["t_rise"] = _LN73 * r_rx * c_rx  # pull-up · bus capacitance (MOSFET capacitance ignored)
    limit = _edge_limit(p["freq"])
    rep = Report("SPICE level shifter")
    side = "high (5 V)" if rx == "hi" else "low (3.3 V)"
    # src: NXP UM10204 (I2C spec): VIH = 0.7·VDD, VIL = 0.3·VDD, VOL ≤ 0.4 V.
    if m["v_rx_high"] < 0.7 * v_rail:
        rep.add("SPICE.LEVEL_SHIFT_HIGH", "warning",
                f"{side} side only reaches {m['v_rx_high']:.2f} V (< 0.7·{v_rail:g} V)", subject=f"net:{rx}",
                hint="stronger pull-ups or a lower frequency", v=m["v_rx_high"])
    if m["v_rx_low"] > min(0.4, 0.3 * v_rail):
        rep.add("SPICE.LEVEL_SHIFT_LOW", "warning",
                f"{side} side only falls to {m['v_rx_low']:.2f} V (> 0.4 V)", subject=f"net:{rx}",
                hint="weaker pull-ups or a stronger driver", v=m["v_rx_low"])
    if not math.isfinite(t_rise) or t_rise > limit:
        rep.add("SPICE.LEVEL_SHIFT_SLOW", "warning",
                f"{side} side rise time {t_rise * 1e9:.0f} ns (30→70 %) exceeds {limit * 1e9:.0f} ns allowed "
                f"at {p['freq'] / 1e3:g} kHz", subject=f"net:{rx}",
                hint="reduce the pull-up resistance or the bus capacitance", t_rise=t_rise, limit=limit)
    if p["v_high"] < p["v_low"]:
        rep.add("SPICE.LEVEL_SHIFT_RAILS", "warning", "the high-side rail is below the low-side rail",
                hint="the MOSFET gate must sit on the LOWER rail")
    summary(rep, f"{p['direction']}: receiving side swings {m['v_rx_low']:.3f} V … {m['v_rx_high']:.3f} V, "
                 f"rise {t_rise * 1e9:.0f} ns, fall {t_fall * 1e9:.0f} ns", **m)
    return m, a, rep


def _edge_limit(freq: float) -> float:
    """Allowed 30→70 % rise time: UM10204 limit of the I2C speed class containing ``freq``
    (≤ 100 kHz: 1000 ns, ≤ 400 kHz: 300 ns, ≤ 1 MHz: 120 ns); above that 12 % of the period."""
    for f_max in sorted(_I2C_MODES):
        if freq <= f_max:
            return _I2C_MODES[f_max][1]
    return 0.12 / freq


def _lvl_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return ("time [s]", tr["time"], {f"V(low side, {p['v_low']:g} V) [V]": tr["v(lo)"],
                                     f"V(high side, {p['v_high']:g} V) [V]": tr["v(hi)"]}, "linear")


LEVEL_SHIFTER = Bench(
    key="level_shifter_bss138",
    title="BSS138 bidirectional level shifter",
    description="Classic one-MOSFET I2C/GPIO level shifter (gate on the low rail, pull-ups on both "
                "sides); an open-drain driver toggles one side and the other side is observed.",
    params={
        "v_low": ParamSpec(3.3, min=1.2, max=5.0, unit="V", label="Low-side rail (Pi)"),
        "v_high": ParamSpec(5.0, min=1.8, max=15.0, unit="V", label="High-side rail"),
        "r_pull_low": ParamSpec(10e3, min=100.0, max=1e6, unit="Ω", label="Low-side pull-up"),
        "r_pull_high": ParamSpec(10e3, min=100.0, max=1e6, unit="Ω", label="High-side pull-up"),
        "c_low": ParamSpec(50e-12, min=0.0, max=10e-9, unit="F", label="Low-side capacitance (0 = none)"),
        "c_high": ParamSpec(50e-12, min=0.0, max=10e-9, unit="F", label="High-side capacitance (0 = none)"),
        "freq": ParamSpec(100e3, min=100.0, max=5e6, unit="Hz", label="Toggle frequency"),
        "direction": ParamSpec("low_to_high", choices=("low_to_high", "high_to_low"), label="Driven side"),
        "mosfet": ParamSpec("nmos_bss138", choices=("nmos_bss138", "nmos_2n7000"), label="MOSFET"),
        # src: Pi pad low side at 8 mA drive = 0.14 V / 8 mA = 17.5 Ω (gpio_resistances)
        "r_driver": ParamSpec(17.5, min=1.0, max=1e3, unit="Ω", label="Open-drain driver on-resistance"),
    },
    build=_lvl_build, evaluate=_lvl_eval, traces=_lvl_traces,
    units={"v_rx_high": "V", "v_rx_low": "V", "v_tx_low": "V", "t_rise": "s", "t_fall": "s"},
)


# ------------------------------------------------------------------------ I2C rise time -----
def _i2c_r(p: dict) -> float:
    r = p["r_pullup"]
    return r * _PI_I2C_PULLUP / (r + _PI_I2C_PULLUP) if p["pi_pullup"] else r


def _i2c_times(p: dict) -> tuple[float, float, float, float]:
    tau_r = _i2c_r(p) * p["c_bus"]
    tau_f = (p["r_driver"] + 0.01) * p["c_bus"]
    t1 = 50e-9
    t2 = t1 + max(20 * tau_f, 200e-9)
    return t1, t2, t2 + 8 * tau_r, tau_r


def _i2c_build(p: dict) -> SpiceCircuit:
    t1, t2, t_end, tau_r = _i2c_times(p)
    vdd = p["v_dd"]
    c = SpiceCircuit("I2C line: pull-up, bus capacitance, open-drain driver")
    c.V("dd", "vdd", "0", vdd)
    c.R("pu", "vdd", "sda", p["r_pullup"])
    if p["pi_pullup"]:
        c.R("pi", "vdd", "sda", _PI_I2C_PULLUP)
    c.C("bus", "sda", "0", p["c_bus"])
    c.V("drive", "ctl", "0", step_wave(t1, t2, 1.0, edge=1e-9))
    c.S("drv", "sda", "dn", "ctl", "0", "sw_ideal")
    c.R("drv", "dn", "0", p["r_driver"])
    tm = max(tau_r / 200, t_end / 20000)  # ≤ 20k points even for a slow driver
    c.tran(tm, t_end, tmax=tm)
    c.measure("t_r", f"tran TRIG v(sda) VAL={fmt_value(0.3 * vdd)} RISE=1 TARG v(sda) VAL={fmt_value(0.7 * vdd)} RISE=1")
    c.measure("t_f", f"tran TRIG v(sda) VAL={fmt_value(0.7 * vdd)} FALL=1 TARG v(sda) VAL={fmt_value(0.3 * vdd)} FALL=1")
    c.measure("v_ol", f"tran FIND v(sda) AT={fmt_value(t2 - 1e-9)}")
    return c


def _i2c_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    r, cb, vdd, rd = _i2c_r(p), p["c_bus"], p["v_dd"], p["r_driver"]
    name, tr_max, tf_max, cb_max, iol = _I2C_MODES[p["speed"]]
    t_r, t_f, v_ol = (sim.measures.get(k, math.nan) for k in ("t_r", "t_f", "v_ol"))
    m = {"t_r": t_r, "t_f": t_f, "v_ol": v_ol, "i_sink": v_ol / rd}
    a = {"t_r": _LN73 * r * cb, "t_f": _LN73 * (r * rd / (r + rd)) * cb, "v_ol": vdd * rd / (r + rd),
         "i_sink": vdd / (r + rd)}
    r_min = (vdd - 0.4) / iol
    r_max = tr_max / (_LN73 * cb)
    rep = Report("SPICE I2C rise time")
    if not math.isfinite(t_r) or t_r > tr_max:
        rep.add("SPICE.I2C_RISE_TIME", "warning",
                f"rise time {t_r * 1e9:.0f} ns exceeds {tr_max * 1e9:.0f} ns allowed in {name} "
                f"({p['speed'] / 1e3:g} kHz)", subject="net:SDA/SCL",
                hint=f"use a pull-up ≤ {r_max:.0f} Ω for C_b = {cb * 1e12:.0f} pF (≥ {r_min:.0f} Ω), "
                     "lower the bus capacitance or the clock", t_r=t_r, limit=tr_max)
    if math.isfinite(t_f) and t_f > tf_max:
        rep.add("SPICE.I2C_FALL_TIME", "warning",
                f"fall time {t_f * 1e9:.0f} ns exceeds {tf_max * 1e9:.0f} ns ({name})", subject="net:SDA/SCL",
                hint="the driver is too weak for this capacitance", t_f=t_f)
    if m["i_sink"] > iol:
        rep.add("SPICE.I2C_PULLUP_STRONG", "warning",
                f"the driver must sink {mA(m['i_sink'])} (> {mA(iol)} the spec guarantees at VOL ≤ 0.4 V)",
                subject="net:SDA/SCL", hint=f"use a pull-up ≥ {r_min:.0f} Ω", i_sink=m["i_sink"])
    if cb > cb_max:
        rep.add("SPICE.I2C_BUS_CAPACITANCE", "warning",
                f"bus capacitance {cb * 1e12:.0f} pF exceeds {cb_max * 1e12:.0f} pF ({name})",
                subject="net:SDA/SCL", hint="shorten the wiring or add a bus buffer", c_bus=cb)
    summary(rep, f"t_r = {t_r * 1e9:.0f} ns (limit {tr_max * 1e9:.0f} ns, {name}), V_OL = {v_ol:.3f} V; "
                 f"pull-up range {r_min:.0f}–{r_max:.0f} Ω for {cb * 1e12:.0f} pF", t_r=t_r)
    return m, a, rep


def _i2c_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return "time [s]", tr["time"], {"V(SDA) [V]": tr["v(sda)"]}, "linear"


I2C_RISE_TIME = Bench(
    key="i2c_rise_time",
    title="I2C rise time",
    description="One I2C line: pull-up to VDD, total bus capacitance, open-drain driver. Rise time "
                "30→70 % (= 0.847·RC) is checked against the NXP UM10204 limit for the chosen speed.",
    params={
        "v_dd": ParamSpec(3.3, min=1.8, max=5.5, unit="V", label="Bus supply"),
        "r_pullup": ParamSpec(4.7e3, min=100.0, max=100e3, unit="Ω", label="Pull-up resistor"),
        "c_bus": ParamSpec(200e-12, min=1e-12, max=2e-9, unit="F", label="Bus capacitance (total)"),
        "speed": ParamSpec(100e3, choices=tuple(_I2C_MODES), unit="Hz", label="Bus speed"),
        "pi_pullup": ParamSpec(False, choices=(False, True), label="Add the Pi's 1.8 kΩ GPIO2/3 pull-up"),
        # src: Pi pad low side at 8 mA drive: 0.14 V / 8 mA = 17.5 Ω
        "r_driver": ParamSpec(17.5, min=1.0, max=1e3, unit="Ω", label="Driver on-resistance"),
    },
    build=_i2c_build, evaluate=_i2c_eval, traces=_i2c_traces,
    units={"t_r": "s", "t_f": "s", "v_ol": "V", "i_sink": "A"},
)


# ------------------------------------------------------------------------- power path -------
_PI_BROWNOUT = 4.63  # src: Raspberry Pi docs power-supplies.adoc — under-voltage below 4.63 V (±5 %)
_PSU = MODEL_INFO["psu_cable"].ratings
_LOAD_STEP_AT = 1e-3
_PI_V_COLLAPSE = (2.0, 3.0)  # src: assumption — rail voltages between which the Pi's load current collapses to 0
_C_IN_FLOOR = 1e-6  # src: assumption — minimum ceramic decoupling at a Pi's 5 V input (c_in = 0 means "only this")
_ESR = 0.01  # src: assumption — ESR of the bulk input capacitor


def _pp_times(p: dict) -> tuple[float, float, float]:
    t2 = _LOAD_STEP_AT + p["t_rise"] + p["t_hold"]
    return _LOAD_STEP_AT, t2, t2 + p["t_rise"] + 1e-3


def _pp_build(p: dict) -> SpiceCircuit:
    t1, t2, t_end = _pp_times(p)
    c = SpiceCircuit("Pi 5 V input: PSU + cable + load step")
    c.X("psu", ["vp", "0"], "psu_cable", params={"vset": p["v_psu"], "rsrc": p["r_source"],
                                                  "rcable": p["r_cable"], "lcable": p["l_cable"]})
    c.V("sense", "vp", "vin", 0.0)
    # a real Pi board always has some ceramic decoupling at the input: never model it as 0
    c.R("esr", "vin", "cx", _ESR)
    c.C("in", "cx", "0", max(p["c_in"], _C_IN_FLOOR))
    tr = p["t_rise"]
    # Load profile as a "current in volts" source; the Pi is a switch-mode load that cannot pull
    # the rail below ~2 V: its current collapses smoothly (tanh) to 0 around 2.5 V (a bare ideal
    # current source would drive the cable inductance to a non-physical negative voltage).
    c.V("iprof", "ip", "0", PWL(((0.0, p["i_idle"]), (t1, p["i_idle"]), (t1 + tr, p["i_load"]),
                                 (t2, p["i_load"]), (t2 + tr, p["i_idle"]))))
    mid, width = sum(_PI_V_COLLAPSE) / 2, (_PI_V_COLLAPSE[1] - _PI_V_COLLAPSE[0]) / 4
    c.B("load", "vin", "0", f"V(ip)*0.5*(1+tanh((V(vin)-{mid:g})/{width:g}))", kind="I")
    c.option(method="gear")
    tm = max(min(tr / 2, t_end / 2000), t_end / 20000)  # ≤ 20k points; PWL corners are breakpoints
    c.tran(tm, t_end, tmax=tm)
    return c


def _time_below(t: np.ndarray, v: np.ndarray, thr: float) -> float:
    """Total time ``v`` spends below ``thr`` with threshold crossings interpolated linearly."""
    total = 0.0
    for i in range(1, len(t)):
        a, b, dt = v[i - 1] - thr, v[i] - thr, float(t[i] - t[i - 1])
        if a < 0 and b < 0:
            total += dt
        elif a < 0 <= b:
            total += dt * (-a / (b - a))
        elif b < 0 <= a:
            total += dt * (-b / (a - b))
    return total


def _pp_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    t1, t2, _t_end = _pp_times(p)
    tr = sim.plot("tran")
    t, v, i = tr["time"], tr["v(vin)"], tr["i(vsense)"]
    thr = p["v_brownout"]
    t_settled = t2 - min(0.1 * p["t_hold"], 1e-4)
    m = {"v_idle": at(sim, "v(vin)", t1 - 1e-5), "v_loaded": at(sim, "v(vin)", t_settled),
         "v_min": float(v[t >= t1 - 1e-6].min()), "t_below": _time_below(t, v, thr),
         "i_peak": float(i.max())}
    i_settled = at(sim, "i(vsense)", t_settled)
    m["droop"] = m["v_idle"] - m["v_min"]
    r_tot = p["r_source"] + p["r_cable"]
    a = {"v_idle": p["v_psu"] - p["i_idle"] * r_tot, "v_loaded": p["v_psu"] - p["i_load"] * r_tot}
    rep = Report("SPICE power path")
    if m["v_min"] < thr:
        rep.add("SPICE.BROWNOUT", "warning",
                f"Pi 5 V input dips to {m['v_min']:.3f} V, below the {thr:g} V under-voltage threshold for "
                f"{m['t_below'] * 1e3:.2f} ms (warning icon / throttling)", subject="net:5V",
                hint="shorter or thicker cable (official 1.5 m 18 AWG ≈ 63 mΩ), 5.1 V supply, more bulk "
                     "capacitance near the Pi, or lower peak load", v_min=m["v_min"], threshold=thr)
    elif m["v_min"] < thr * 1.05:
        rep.add("SPICE.BROWNOUT_MARGIN", "info",
                f"minimum {m['v_min']:.3f} V is within the ±5 % tolerance of the {thr:g} V detector",
                subject="net:5V", v_min=m["v_min"])
    if i_settled > _PSU["i_max"]:  # settled load, not the µs LC overshoot of the cable current
        rep.add("SPICE.PSU_OVERLOAD", "warning",
                f"the load draws {i_settled:.2f} A — above the {_PSU['i_max']:g} A rating of the "
                f"official 15 W PSU", subject="part:psu", hint="use a 5 A supply (27 W PSU for Pi 5)",
                i_load=i_settled)
    summary(rep, f"idle {m['v_idle']:.3f} V, loaded {m['v_loaded']:.3f} V, minimum {m['v_min']:.3f} V during "
                 f"the {p['i_load']:g} A step (cable + PSU {r_tot * 1e3:.0f} mΩ)", **m)
    return m, a, rep


def _pp_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return ("time [s]", tr["time"], {"V(Pi 5V in) [V]": tr["v(vin)"], "I(cable) [A]": tr["i(vsense)"]},
            "linear")


POWER_PATH = Bench(
    key="power_path",
    title="Power path brown-out",
    description="USB PSU + cable (R, L) into the Pi's 5 V input with bulk capacitance and a load step; "
                "flags dips below the Pi's 4.63 V under-voltage detector.",
    params={
        "v_psu": ParamSpec(5.1, min=3.0, max=6.0, unit="V", label="PSU voltage"),
        "r_cable": ParamSpec(0.0627, min=0.0, max=5.0, unit="Ω", label="Cable resistance (round trip)",
                             help="official 1.5 m 18 AWG ≈ 0.063 Ω; cheap 1 m 28 AWG ≈ 0.43 Ω"),
        "l_cable": ParamSpec(1.2e-6, min=0.0, max=1e-4, unit="H", label="Cable inductance (≈ 0.8 µH/m)"),
        "r_source": ParamSpec(0.02, min=0.0, max=1.0, unit="Ω", label="PSU output resistance"),
        "c_in": ParamSpec(47e-6, min=0.0, max=10e-3, unit="F", label="Bulk capacitance at the Pi (0 = minimum)",
                          help="assumption — typical bulk capacitance at a single-board computer input; values below "
                               "1 µF are raised to 1 µF (ceramic decoupling every board has; an ideal 0 F input "
                               "would ring non-physically)"),
        # src: Raspberry Pi docs — Pi 4B typical bare-board active current ≈ 600 mA
        "i_idle": ParamSpec(0.6, min=0.0, max=10.0, unit="A", label="Idle current"),
        "i_load": ParamSpec(2.5, min=0.0, max=10.0, unit="A", label="Peak current"),
        "t_rise": ParamSpec(10e-6, min=1e-7, max=1e-2, unit="s", label="Load step rise time"),
        "t_hold": ParamSpec(2e-3, min=1e-5, max=0.5, unit="s", label="Peak duration"),
        "v_brownout": ParamSpec(_PI_BROWNOUT, min=3.0, max=5.5, unit="V", label="Under-voltage threshold"),
    },
    build=_pp_build, evaluate=_pp_eval, traces=_pp_traces,
    units={"v_idle": "V", "v_loaded": "V", "v_min": "V", "t_below": "s", "i_peak": "A", "droop": "V"},
)

SWITCHING_BENCHES = (MOSFET_LOWSIDE, BJT_SWITCH, LEVEL_SHIFTER, I2C_RISE_TIME, POWER_PATH)
