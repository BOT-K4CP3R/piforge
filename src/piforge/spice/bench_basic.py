"""Benches for indicator and passive circuits: LED driver, voltage divider, RC filter, debounce.

Each bench is ``build(params) -> SpiceCircuit``, ``evaluate(sim, params) -> (measures, analytic,
report)`` and ``traces(sim, params)``; measures are SI values, ``analytic`` the textbook formula
for the same quantity so the GUI/report can show the simulation error.
"""

from __future__ import annotations

import math

import numpy as np

from piforge.core.report import Report
from piforge.spice.benchkit import (
    GPIO_SAFE_A,
    Bench,
    ParamSpec,
    add_gpio,
    at,
    mA,
    schmitt_edges,
    step_wave,
    summary,
)
from piforge.spice.errors import BenchParamError
from piforge.spice.models import MODEL_INFO
from piforge.spice.netlist import PWL, Pulse, SpiceCircuit, fmt_value
from piforge.spice.runner import SimResult

LEDS = ("led_red", "led_green", "led_blue", "led_white", "led_ir")
DRIVES = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0)
# src: raspberrypi/documentation gpio-pad-controls.adoc — drive strength list 2–16 mA, reset 8 mA.
DRIVE_SPEC = ParamSpec(8.0, choices=DRIVES, unit="mA", label="GPIO pad drive strength",
                        help="Pi default after reset is 8 mA; sets the pad output resistance")
# src: assumption — indicator LEDs are specified at 20 mA; below ≈ 1 mA they look dim indoors.
_LED_DIM_A = 1e-3
# src: common 1/4 W rating of through-hole resistors (0603 SMD parts are only 0.1 W).
_RESISTOR_W = 0.25

# ------------------------------------------------------------------------------ LED driver ---
_LED_T1, _LED_T2, _LED_END = 0.5e-3, 1.5e-3, 2.0e-3


def _led_build(p: dict) -> SpiceCircuit:
    c = SpiceCircuit(f"LED driver: {p['led']} through {p['r_series']:g} ohm from a Pi GPIO")
    c.V("io", "vio", "0", p["v_gpio"])
    sink = p["topology"] == "sink"
    add_gpio(c, "1", "pin", step_wave(_LED_T1, _LED_T2, p["v_gpio"], invert=sink), drive_ma=p["drive_ma"])
    if sink:  # 3V3 → LED → R → pin (pin pulled low turns the LED on)
        c.D("led", "vio", "k", p["led"])
        c.V("sense", "k", "kr", 0.0)
        c.R("s", "kr", "pin", p["r_series"])
    else:  # pin → R → LED → GND
        c.R("s", "pin", "a", p["r_series"])
        c.V("sense", "a", "an", 0.0)
        c.D("led", "an", "0", p["led"])
    c.tran(5e-6, _LED_END, tmax=5e-6)
    return c


def _led_vf(sim: SimResult, p: dict) -> np.ndarray:
    tr = sim.plot("tran")
    return tr["v(vio)"] - tr["v(k)"] if p["topology"] == "sink" else tr["v(an)"]


def _led_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    t = _LED_T2 - 0.1e-3
    tr = sim.plot("tran")
    i = at(sim, "i(vsense)", t)
    v_led = float(np.interp(t, tr["time"], _led_vf(sim, p)))
    v_pin = at(sim, "v(pin)", t)
    r, vdd = p["r_series"], p["v_gpio"]
    rating = MODEL_INFO[p["led"]].ratings
    m = {"i_led": i, "v_led": v_led, "v_pin": v_pin, "p_resistor": i * i * r, "p_led": i * v_led}
    i_text = (vdd - rating["vf_typ"]) / r  # textbook: (VDD − VF datasheet)/R, ideal source
    a = {"i_led": i_text, "p_resistor": i_text ** 2 * r}
    rep = Report("SPICE LED driver")
    # The pad's internal resistance is NOT a current limit (Pi docs), so judge the worst case
    # of an ideal 3.3 V source as well as the simulated current.
    i_worst = max(i, (vdd - v_led) / r)
    if i_worst > GPIO_SAFE_A:
        r_min = (vdd - v_led) / GPIO_SAFE_A
        rep.add("SPICE.GPIO_OVERCURRENT", "error",
                f"GPIO current {mA(i)} simulated, up to {mA(i_worst)} with a strong pad — above the "
                f"16 mA safe pad current", subject="pin:GPIO",
                hint=f"use R ≥ {r_min:.0f} Ω or switch the LED with a transistor",
                i_sim=i, i_worst=i_worst, limit=GPIO_SAFE_A)
    if i > p["drive_ma"] * 1e-3:
        rep.add("SPICE.GPIO_DRIVE", "warning",
                f"{mA(i)} exceeds the {p['drive_ma']:g} mA pad drive strength: logic levels are no "
                f"longer guaranteed (pin at {v_pin:.2f} V)", subject="pin:GPIO",
                hint="raise the pad drive strength or the series resistance", i=i, drive_ma=p["drive_ma"])
    if i > rating["if_max"]:
        rep.add("SPICE.LED_OVERCURRENT", "error",
                f"LED current {mA(i)} exceeds the {mA(rating['if_max'])} rating", subject=f"part:{p['led']}",
                hint="increase the series resistor", i=i, if_max=rating["if_max"])
    if i < _LED_DIM_A:
        rep.add("SPICE.LED_DIM", "warning",
                f"LED current only {mA(i)} at VF {v_led:.2f} V — it will look dim (rated at 20 mA)",
                subject=f"part:{p['led']}",
                hint="lower the series resistor, or switch it from 5 V with a transistor "
                     "(blue/white LEDs need ≈ 3 V)", i=i)
    summary(rep, f"{p['led']}: {mA(i)} at VF {v_led:.2f} V (textbook (VDD − VF)/R = {mA(i_text)}); "
                 f"pin at {v_pin:.2f} V", i_led=i, v_led=v_led)
    return m, a, rep


def _led_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return ("time [s]", tr["time"], {"V(pin) [V]": tr["v(pin)"], "V(LED) [V]": _led_vf(sim, p),
                                     "I(LED) [mA]": tr["i(vsense)"] * 1e3}, "linear")


LED_DRIVER = Bench(
    key="led_driver",
    title="LED on a GPIO pin",
    description="Pi GPIO (3.3 V pad model, 37.5 Ω/17.5 Ω at 8 mA drive) → series resistor → LED. "
                "Checks the 16 mA pad limit, drive strength, LED rating and brightness.",
    params={
        "r_series": ParamSpec(330.0, min=1.0, max=1e6, unit="Ω", label="Series resistor"),
        "led": ParamSpec("led_red", choices=LEDS, label="LED"),
        "v_gpio": ParamSpec(3.3, min=1.8, max=3.6, unit="V", label="GPIO supply (VDD IO)"),
        "drive_ma": DRIVE_SPEC,
        "topology": ParamSpec("source", choices=("source", "sink"), label="Topology",
                              help="source: pin → R → LED → GND; sink: 3V3 → LED → R → pin"),
    },
    build=_led_build, evaluate=_led_eval, traces=_led_traces,
    units={"i_led": "A", "v_led": "V", "v_pin": "V", "p_resistor": "W", "p_led": "W"},
)


# ------------------------------------------------------------------------- voltage divider ---
def _div_build(p: dict) -> SpiceCircuit:
    c = SpiceCircuit("Resistive voltage divider")
    c.V("in", "in", "0", p["v_in"])
    c.R("top", "in", "out", p["r_top"])
    c.R("bot", "out", "0", p["r_bottom"])
    if p["r_load"] > 0:
        c.R("load", "out", "0", p["r_load"])
    c.op()
    c.dc("in", 0.0, p["v_in"], p["v_in"] / 100.0)
    return c


def _div_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    v_out = float(sim.plot("op")["v(out)"][0])
    vin, rt, rb, rl = p["v_in"], p["r_top"], p["r_bottom"], p["r_load"]
    i_top = (vin - v_out) / rt
    m = {"v_out": v_out, "i_top": i_top, "p_top": i_top ** 2 * rt, "p_bottom": v_out ** 2 / rb}
    rbe = rb * rl / (rb + rl) if rl > 0 else rb
    va, ia = vin * rbe / (rt + rbe), vin / (rt + rbe)
    a = {"v_out": va, "i_top": ia, "p_top": ia ** 2 * rt, "p_bottom": va ** 2 / rb}
    rep = Report("SPICE voltage divider")
    v_open = vin * rb / (rt + rb)
    r_th = rt * rb / (rt + rb)
    if rl > 0 and abs(v_out - v_open) > 0.05 * abs(v_open):
        rep.add("SPICE.DIVIDER_LOADED", "warning",
                f"the {rl:g} Ω load pulls the output from {v_open:.3f} V to {v_out:.3f} V "
                f"({(v_out / v_open - 1) * 100:+.1f} %)", subject="net:out",
                hint=f"make the divider ≥ 10× stiffer than the load (R_th = {r_th:.0f} Ω now) or buffer it",
                v_unloaded=v_open, v_loaded=v_out)
    for name, pw in (("r_top", m["p_top"]), ("r_bottom", m["p_bottom"])):
        if pw > _RESISTOR_W:
            rep.add("SPICE.RESISTOR_POWER", "warning", f"{name} dissipates {pw:.2f} W (> 0.25 W)",
                    subject=f"part:{name}", hint="use larger resistances or a higher-power resistor", p=pw)
    summary(rep, f"V(out) = {v_out:.4g} V (analytic {va:.4g} V), divider current {mA(i_top)}, "
                 f"source resistance R_th = {r_th:.4g} Ω", v_out=v_out, r_th=r_th)
    return m, a, rep


def _div_traces(sim: SimResult, p: dict):
    dc = sim.plot("dc")
    return "V(in) [V]", dc["v(in)"], {"V(out) [V]": dc["v(out)"]}, "linear"


VOLTAGE_DIVIDER = Bench(
    key="voltage_divider",
    title="Voltage divider",
    description="Two resistors with an optional load, e.g. 5 V → 3.3 V for a GPIO input. "
                "Reports output voltage, loading error and resistor power.",
    params={
        "v_in": ParamSpec(5.0, min=0.01, max=1000.0, unit="V", label="Input voltage"),
        "r_top": ParamSpec(1e3, min=1e-3, max=1e9, unit="Ω", label="Top resistor"),
        "r_bottom": ParamSpec(2e3, min=1e-3, max=1e9, unit="Ω", label="Bottom resistor"),
        "r_load": ParamSpec(0.0, min=0.0, max=1e12, unit="Ω", label="Load resistance (0 = none)"),
    },
    build=_div_build, evaluate=_div_eval, traces=_div_traces,
    units={"v_out": "V", "i_top": "A", "p_top": "W", "p_bottom": "W"},
)


# ------------------------------------------------------------------------------ RC filter ----
_TAU_RANGE = (1e-12, 1e4)  # s; beyond ~1e6 s ngspice's step control degenerates (minutes per run)


def _rc_build(p: dict) -> SpiceCircuit:
    tau = p["r"] * p["c"]
    if not _TAU_RANGE[0] <= tau <= _TAU_RANGE[1]:
        raise BenchParamError(f"R·C = {tau:g} s is outside the supported range "
                              f"{_TAU_RANGE[0]:g} … {_TAU_RANGE[1]:g} s")
    c = SpiceCircuit("RC low-pass filter")
    c.R("1", "in", "out", p["r"])
    c.C("1", "out", "0", p["c"])
    if p["analysis"] == "ac":
        fc = 1.0 / (2.0 * math.pi * tau)
        c.V("in", "in", "0", 0.0, ac=1.0)
        c.ac(50, fc / 1000.0, fc * 1000.0)
        c.measure("f_3db", "ac WHEN vdb(out)=-3.0103 FALL=1")
    else:
        v = p["v_step"]
        c.V("in", "in", "0", Pulse(0.0, v, td=0.0, tr=tau / 1000, tf=tau / 1000, pw=20 * tau, per=40 * tau))
        c.tran(tau / 200, 5 * tau, tmax=tau / 200)
        c.measure("v_at_tau", f"tran FIND v(out) AT={fmt_value(tau)}")
        c.measure("t_rise", f"tran TRIG v(out) VAL={fmt_value(0.1 * v)} RISE=1 "
                            f"TARG v(out) VAL={fmt_value(0.9 * v)} RISE=1")
        c.measure("tau", f"tran WHEN v(out)={fmt_value(v * (1 - math.exp(-1)))} RISE=1")
    return c


def _bode(sim: SimResult) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ac = sim.plot("ac")
    h = ac["v(out)"]
    return ac["frequency"], 20 * np.log10(np.abs(h)), np.degrees(np.unwrap(np.angle(h)))


def _rc_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    tau = p["r"] * p["c"]
    rep = Report("SPICE RC filter")
    if p["analysis"] == "ac":
        fc = 1.0 / (2.0 * math.pi * tau)
        f, db, ph = _bode(sim)
        lf = np.log10(f)
        f3 = sim.measures.get("f_3db", math.nan)
        if not math.isfinite(f3):
            f3 = float(10 ** np.interp(-3.0103, db[::-1], lf[::-1]))
        m = {"f_3db": f3, "gain_10fc": float(np.interp(math.log10(10 * fc), lf, db)),
             "phase_fc": float(np.interp(math.log10(fc), lf, ph))}
        a = {"f_3db": fc, "gain_10fc": -10 * math.log10(101.0), "phase_fc": -45.0}
        summary(rep, f"−3 dB at {f3:.4g} Hz (1/(2πRC) = {fc:.4g} Hz); {m['gain_10fc']:.1f} dB at 10·fc",
                f_3db=f3)
    else:
        v = p["v_step"]
        m = {k: sim.measures.get(k, math.nan) for k in ("v_at_tau", "t_rise", "tau")}
        a = {"v_at_tau": v * (1 - math.exp(-1)), "t_rise": math.log(9) * tau, "tau": tau}
        summary(rep, f"τ = {m['tau']:.4g} s (RC = {tau:.4g} s), 10–90 % rise {m['t_rise']:.4g} s "
                     f"(2.197·RC = {a['t_rise']:.4g} s)", tau=m["tau"])
    return m, a, rep


def _rc_traces(sim: SimResult, p: dict):
    if p["analysis"] == "ac":
        f, db, ph = _bode(sim)
        return "frequency [Hz]", f, {"|H| [dB]": db, "phase [deg]": ph}, "log"
    tr = sim.plot("tran")
    return "time [s]", tr["time"], {"V(in) [V]": tr["v(in)"], "V(out) [V]": tr["v(out)"]}, "linear"


RC_FILTER = Bench(
    key="rc_filter",
    title="RC low-pass filter",
    description="First-order RC: Bode plot (ac) or step response (step), compared with "
                "f_c = 1/(2πRC) and τ = RC.",
    params={
        "r": ParamSpec(1e3, min=1e-3, max=1e9, unit="Ω", label="Resistance"),
        "c": ParamSpec(1e-6, min=1e-15, max=10.0, unit="F", label="Capacitance"),
        "analysis": ParamSpec("ac", choices=("ac", "step"), label="Analysis"),
        "v_step": ParamSpec(3.3, min=1e-3, max=1000.0, unit="V", label="Step amplitude (step only)"),
    },
    build=_rc_build, evaluate=_rc_eval, traces=_rc_traces,
    units={"f_3db": "Hz", "gain_10fc": "dB", "phase_fc": "deg", "v_at_tau": "V", "t_rise": "s", "tau": "s"},
)


# ------------------------------------------------------------------------- button debounce ---
_PRESS_AT = 1e-3
# Contact bounce pattern (fractions of the bounce time): press = closed/open alternating, then
# closed; release = open/closed alternating, then open. src: J. Ganssle, "A Guide to Debouncing"
# (2004) — measured bounce ≈ 1.5 ms typical, up to ≈ 6 ms; release bounce is shorter.
_PRESS_PATTERN = (0.05, 0.125, 0.075, 0.15, 0.125, 0.1875, 0.1875, 0.10)
_RELEASE_PATTERN = (0.4, 0.133, 0.4, 0.067)
_PIN_C = 5e-12  # src: Raspberry Pi docs GPIO voltage table, C_IN typ 5 pF (BCM2835)
_SLOW_S = 20e-3  # src: assumption — > 20 ms detection delay starts to feel sluggish


def _debounce_timeline(p: dict) -> tuple[PWL, float, float, float]:
    b = p["bounce_ms"] * 1e-3
    seg_min = min(_PRESS_PATTERN + _RELEASE_PATTERN) * b * 0.5 if b > 0 else 1e-3
    edge = min(1e-6, seg_min / 10)
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    level = 0.0

    def go(t: float, new: float) -> None:
        nonlocal level
        pts.extend([(t, level), (t + edge, new)])
        level = new

    t = _PRESS_AT
    for k, frac in enumerate(_PRESS_PATTERN if b > 0 else ()):
        go(t, 1.0 if k % 2 == 0 else 0.0)
        t += frac * b
    go(t, 1.0)
    t_release = t + p["hold_ms"] * 1e-3
    t = t_release
    for k, frac in enumerate(_RELEASE_PATTERN if b > 0 else ()):
        go(t, 0.0 if k % 2 == 0 else 1.0)
        t += frac * b * 0.5
    go(t, 0.0)
    tau = (p["r_pullup"] + p["r_series"]) * (p["c"] + _PIN_C)
    t_end = t + max(2e-3, min(6 * tau, 1.0))
    return PWL(tuple(pts)), _PRESS_AT, t_release, t_end


def _db_in_node(p: dict) -> str:
    return "in" if p["r_series"] > 0 else "sw"


def _db_build(p: dict) -> SpiceCircuit:
    if not p["v_il"] < p["v_ih"] < p["v_dd"]:
        raise BenchParamError(f"input thresholds must satisfy v_il < v_ih < v_dd, got v_il = {p['v_il']:g} V, "
                              f"v_ih = {p['v_ih']:g} V, v_dd = {p['v_dd']:g} V")
    wave, _t1, _t2, t_end = _debounce_timeline(p)
    c = SpiceCircuit("Push-button with RC debounce into a Pi GPIO")
    c.V("dd", "vdd", "0", p["v_dd"])
    c.R("pu", "vdd", "sw", p["r_pullup"])
    c.V("contact", "ctl", "0", wave)
    c.S("btn", "sw", "0", "ctl", "0", "sw_ideal")
    node = _db_in_node(p)
    if p["r_series"] > 0:
        c.R("s", "sw", "in", p["r_series"])
    if p["c"] > 0:
        c.C("deb", node, "0", p["c"])
    c.C("pin", node, "0", _PIN_C)
    b = p["bounce_ms"] * 1e-3
    tau_down = (p["r_series"] + 0.01) * (p["c"] + _PIN_C)
    tmax = max(2e-6, t_end / 20000, min(20e-6, (b * 0.05 / 5) if b > 0 else 20e-6, tau_down / 10))
    c.tran(tmax, t_end, tmax=tmax)
    return c


def _db_eval(sim: SimResult, p: dict) -> tuple[dict, dict, Report]:
    _w, t_press, t_rel, _t_end = _debounce_timeline(p)
    tr = sim.plot("tran")
    t, v = tr["time"], tr[f"v({_db_in_node(p)})"]
    edges = schmitt_edges(t, v, p["v_il"], p["v_ih"])
    press = [e for e in edges if t_press <= e[0] < t_rel]
    release = [e for e in edges if e[0] >= t_rel]
    falls = [e[0] for e in press if e[1] == 0]
    rises = [e[0] for e in release if e[1] == 1]
    m = {"edges_press": float(len(press)), "edges_release": float(len(release)),
         "t_press_detect": (falls[-1] - t_press) if falls else math.nan,
         "t_release_detect": (rises[-1] - t_rel) if rises else math.nan}
    c_tot = p["c"] + _PIN_C
    tau_up = (p["r_pullup"] + p["r_series"]) * c_tot
    tau_down = (p["r_series"] + 0.01) * c_tot  # 0.01 Ω = sw_ideal contact resistance
    vdd = p["v_dd"]
    # analytic = clean (bounce-free) contact: C charged to VDD at the press, ≈ 0 V at the release
    a = {"edges_press": 1.0, "edges_release": 1.0,
         "t_press_detect": tau_down * math.log(vdd / p["v_il"]),
         "t_release_detect": tau_up * math.log(vdd / (vdd - p["v_ih"])) if p["v_ih"] < vdd else math.nan}
    rep = Report("SPICE button debounce")
    if len(press) > 1 or len(release) > 1:
        rep.add("SPICE.DEBOUNCE_BOUNCE", "warning",
                f"the Pi sees {len(press)} edge(s) per press and {len(release)} per release — "
                f"contact bounce ({p['bounce_ms']:g} ms) gets through", subject="net:button",
                hint="use τ = (R_pullup + R_series)·C well above the bounce time (e.g. 10 kΩ + 100 nF "
                     "= 1 ms) or debounce in software (gpiozero bounce_time)",
                edges_press=len(press), edges_release=len(release))
    for name in ("t_press_detect", "t_release_detect"):
        val = m[name]
        if not math.isfinite(val) or val > _SLOW_S:
            what = "never" if not math.isfinite(val) else f"only after {val * 1e3:.1f} ms"
            rep.add("SPICE.DEBOUNCE_SLOW", "warning",
                    f"{'press' if 'press' in name else 'release'} is detected {what}",
                    subject="net:button", hint="reduce the RC time constant", t=val)
    summary(rep, f"press seen after {m['t_press_detect'] * 1e3:.3g} ms ({len(press)} edge(s)), release "
                 f"after {m['t_release_detect'] * 1e3:.3g} ms ({len(release)} edge(s)); "
                 f"τ_up = {tau_up * 1e3:.3g} ms", tau_up=tau_up)
    return m, a, rep


def _db_traces(sim: SimResult, p: dict):
    tr = sim.plot("tran")
    return ("time [s]", tr["time"], {"V(switch) [V]": tr["v(sw)"],
                                     "V(GPIO) [V]": tr[f"v({_db_in_node(p)})"]}, "linear")


BUTTON_DEBOUNCE = Bench(
    key="button_debounce",
    title="Push-button debounce",
    description="Bouncing push-button to GND with pull-up, series R and capacitor into a GPIO "
                "(Schmitt input). Counts the edges the Pi would see per press and release; detection "
                "times are measured from the first contact change (analytic values: clean contact).",
    params={
        "v_dd": ParamSpec(3.3, min=1.8, max=5.0, unit="V", label="Pull-up supply"),
        "r_pullup": ParamSpec(10e3, min=100.0, max=1e6, unit="Ω", label="Pull-up resistor"),
        "r_series": ParamSpec(1e3, min=0.0, max=1e6, unit="Ω", label="Series resistor (0 = none)"),
        "c": ParamSpec(100e-9, min=0.0, max=10e-6, unit="F", label="Debounce capacitor (0 = none)"),
        "bounce_ms": ParamSpec(2.0, min=0.0, max=20.0, unit="ms", label="Contact bounce duration"),
        "hold_ms": ParamSpec(20.0, min=1.0, max=1000.0, unit="ms", label="Button held for"),
        # src: Raspberry Pi docs GPIO tables — BCM2711 VIL ≤ 0.8 V / VIH ≥ 2.0 V (BCM2835: 0.9/1.6).
        "v_il": ParamSpec(0.8, min=0.1, max=3.0, unit="V", label="Input low threshold VIL"),
        "v_ih": ParamSpec(2.0, min=0.2, max=4.5, unit="V", label="Input high threshold VIH"),
    },
    build=_db_build, evaluate=_db_eval, traces=_db_traces,
    units={"edges_press": "", "edges_release": "", "t_press_detect": "s", "t_release_detect": "s"},
)

BASIC_BENCHES = (LED_DRIVER, VOLTAGE_DIVIDER, RC_FILTER, BUTTON_DEBOUNCE)
