"""Enclosure thermal model: how hot does it get inside a printed box?

Steady-state lumped model of a box (outer size ``x × y × z`` mm, Z up, standing on a desk) with
``P`` watts dissipated inside. The internal air temperature ``T_i`` solves the energy balance
``P = Q_walls(T_i) + Q_vents(T_i)`` by bracketing root finding (Illinois false position). Per face
group (sides, top, bottom) the wall heat flow passes three resistances in series::

    Q = h_in·A_in·(T_i − T_si) = k/t·A_m·(T_si − T_so) = (h_out + h_rad)·A_out·(T_so − T_a)

- ``h_in``: natural convection of the enclosed air on the inner faces (vertical: Churchill–Chu;
  ceiling: cooled plate facing down; floor: cooled plate facing up). Internal radiation is not
  counted, which over-estimates ``T_i`` (conservative).
- ``h_out``: natural convection of room air on the outer faces (vertical / horizontal-up plates);
  ``h_rad = εσ(T_so² + T_a²)(T_so + T_a)`` is the exact linearisation of grey-body radiation.
- The bottom stands on a desk: instead of convection + radiation it conducts into a semi-infinite
  wooden support (isothermal disc, ``G = 2·k·D``) — i.e. it is mostly insulated.
- Vents (stack effect): ``V = C_d·A_eff·sqrt(2·g·H·ΔT/T_i)``, ``1/A_eff² = 1/A_in² + 1/A_out²``;
  zero when an opening is missing or ``H = 0``. A fan runs where its linear curve
  ``p = p_max·(1 − V/V_free)`` meets the openings' ``p = ρ/2·(V/(C_d·A_eff))²`` (each opening ≥
  ``LEAK_MM2`` of gaps), capped at ``FAN_DERATE·V_free``; it replaces the stack flow when larger.
  ``Q_vents = ρ·c_p·V·ΔT`` (ρ of the inflowing room air).
- Heat that cannot be shed even ``DT_MAX`` above ambient is clamped there and flagged
  (THERMAL.OVERTEMP) — a design/input problem, not an exception.
- Pi SoC: ``T_soc = T_i + rise·min(1, P / P_load)`` with per-board rises measured in open air
  (``PI_THERMAL``), unless ``soc_rise_c`` is given.

Air properties are evaluated at the film temperature. Pure ``math`` — no CAD kernel. Cited
constants and the per-board ``PI_THERMAL`` table live in ``thermal_data`` (re-exported here).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from piforge.analysis.thermal_data import (
    CD_VENT, CFM_TO_M3S, COOLING, CP_AIR, DT_MAX, EPSILON, FAN_30MM_CFM, FAN_30MM_STATIC_PA,
    FAN_DERATE, FAN_INLET_SPEED, FAN_STARVED_FRACTION, G, K_AIR_300, K_SUPPORT, KELVIN, LEAK_MM2,
    MARGIN_C, NU_AIR_300, P_ATM, PI_THERMAL, PR_AIR, R_AIR, SIGMA, PiThermal, get_pi_thermal,
)
from piforge.core.errors import ValidationError
from piforge.core.report import Report, Severity, jsonable
from piforge.fab.profiles import MATERIALS, Material, get_material

__all__ = [
    "ThermalInputs", "ThermalResult", "enclosure_temperature", "softening_limits", "PiThermal",
    "PI_THERMAL", "get_pi_thermal", "COOLING", "CD_VENT", "CFM_TO_M3S", "CP_AIR", "DT_MAX",
    "EPSILON", "FAN_30MM_CFM", "FAN_30MM_STATIC_PA", "FAN_DERATE", "FAN_INLET_SPEED",
    "FAN_STARVED_FRACTION", "G", "K_AIR_300", "K_SUPPORT", "KELVIN", "LEAK_MM2", "MARGIN_C",
    "NU_AIR_300", "P_ATM", "PR_AIR", "R_AIR", "SIGMA",
]

# Churchill & Chu (1975) Prandtl factors — Incropera eqs. 9.26 (all Ra) and 9.27 (Ra ≤ 1e9).
_CC_FACTOR = 1.0 + (0.492 / PR_AIR) ** (9.0 / 16.0)
_CC_LAMINAR = _CC_FACTOR ** (4.0 / 9.0)
_CC_FULL = _CC_FACTOR ** (8.0 / 27.0)


# -- inputs and results ----------------------------------------------------------------------------
@dataclass
class ThermalInputs:
    """A box and what heats it. Lengths mm (Z up), areas mm², power W, airflow cfm, Pa, °C.

    ``vent_height_mm``: vertical distance between inlet and outlet vent centres. ``fan_cfm`` /
    ``fan_static_pa``: the fan's free-air flow and maximum static pressure (datasheet); a fan's
    own open aperture counts towards the vent area on its side (an exhaust fan → ``vent_out_mm2``).
    ``material`` may also be a ``Material`` instance.
    """

    power_w: float
    outer_mm: tuple[float, float, float]
    wall_mm: float = 2.0
    material: str | Material = "PETG"
    vent_in_mm2: float = 0.0
    vent_out_mm2: float = 0.0
    vent_height_mm: float = 0.0
    fan_cfm: float = 0.0
    ambient_c: float = 25.0
    fan_static_pa: float = FAN_30MM_STATIC_PA

    def __post_init__(self) -> None:
        _validate(self)


@dataclass
class ThermalResult:
    """Steady state: ``q_walls_w + q_vents_w == power_w`` unless ``details["clamped"]``
    (THERMAL.OVERTEMP); ``soc_c`` is None without a board."""

    internal_c: float
    delta_c: float
    q_walls_w: float
    q_vents_w: float
    airflow_m3s: float
    report: Report
    soc_c: float | None = None
    details: dict = field(default_factory=dict)  # per-face heat/temperatures, flows, SoC inputs

    def to_dict(self) -> dict:
        return jsonable({"internal_c": self.internal_c, "delta_c": self.delta_c,
                         "q_walls_w": self.q_walls_w, "q_vents_w": self.q_vents_w,
                         "airflow_m3s": self.airflow_m3s, "soc_c": self.soc_c,
                         "details": self.details, "report": self.report.to_dict()})


def softening_limits(material: str | Material) -> tuple[float, float]:
    """``(warn_above_c, error_at_c)`` for the internal air temperature of a printed enclosure.

    WARNING above ``max_service_c − 5``; ERROR at ``glass_transition_c − 5`` for amorphous
    plastics. Semi-crystalline/elastomeric materials whose Tg lies below their service temperature
    (TPU, PA-CF) do not soften at Tg, so their ERROR limit is ``max_service_c`` itself.
    """
    m = get_material(material)
    warn = m.max_service_c - MARGIN_C
    if m.glass_transition_c >= m.max_service_c:
        return warn, m.glass_transition_c - MARGIN_C
    return warn, m.max_service_c


def _validate(inp: ThermalInputs) -> None:
    try:
        dims = tuple(float(v) for v in inp.outer_mm)
    except (TypeError, ValueError):
        dims = ()
    if len(dims) != 3 or not all(math.isfinite(d) and d > 0 for d in dims):
        raise ValidationError(f"outer_mm must be three positive lengths (x, y, z) in mm; "
                              f"got {inp.outer_mm!r}")
    inp.outer_mm = dims  # type: ignore[assignment]
    for name in ("power_w", "wall_mm", "vent_in_mm2", "vent_out_mm2", "vent_height_mm", "fan_cfm",
                 "fan_static_pa"):
        v = getattr(inp, name)
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValidationError(f"ThermalInputs.{name} must be a finite number ≥ 0; got {v!r}")
    if not 0 < inp.wall_mm < min(dims) / 2:
        raise ValidationError(f"wall_mm = {inp.wall_mm} mm must be > 0 and leave a cavity "
                              f"(2·wall < smallest outer size {min(dims):g} mm)")
    a = inp.ambient_c
    if not isinstance(a, (int, float)) or not math.isfinite(a) or a <= -KELVIN:
        raise ValidationError(f"ambient_c must be a finite temperature above −273.15 °C; got {a!r}")


# -- numerics and heat-transfer correlations -----------------------------------------------------
def _root(f: Callable[[float], float], lo: float, hi: float, xtol: float) -> float:
    """Root of ``f`` on ``[lo, hi]`` (sign change required) — Illinois false position."""
    flo, fhi = f(lo), f(hi)
    if flo == 0.0 or fhi == 0.0:
        return lo if flo == 0.0 else hi
    if (flo > 0.0) == (fhi > 0.0):
        raise ValueError("root not bracketed")
    side = 0
    for _ in range(300):
        x = (lo * fhi - hi * flo) / (fhi - flo)
        if not lo < x < hi:  # rounding trouble: bisect instead
            x = 0.5 * (lo + hi)
        fx = f(x)
        if fx == 0.0:
            return x
        if (fx > 0.0) == (fhi > 0.0):
            hi, fhi = x, fx
            if side == -1:
                flo *= 0.5
            side = -1
        else:
            lo, flo = x, fx
            if side == 1:
                fhi *= 0.5
            side = 1
        if hi - lo <= xtol:
            break
    return 0.5 * (lo + hi)


def _rayleigh(dt: float, length: float, t_film: float) -> tuple[float, float]:
    """(Ra, k) of air: Ra = g·β·ΔT·L³/(ν·α) with β = 1/T_film (ideal gas), α = ν/Pr."""
    r = t_film / 300.0
    k = K_AIR_300 * r**0.885
    nu = NU_AIR_300 * r**1.78
    return G * dt * length**3 * PR_AIR / (t_film * nu * nu), k


def _h_vertical(dt: float, length: float, t_film: float) -> float:
    """Vertical plate, W/(m²·K) — Churchill & Chu (Incropera eqs. 9.27 / 9.26)."""
    if dt <= 0.0:
        return 0.0
    ra, k = _rayleigh(dt, length, t_film)
    if ra <= 1e9:
        nu = 0.68 + 0.670 * ra**0.25 / _CC_LAMINAR
    else:
        nu = (0.825 + 0.387 * ra ** (1.0 / 6.0) / _CC_FULL) ** 2
    return nu * k / length


def _h_horizontal(dt: float, length: float, t_film: float, *, unstable: bool) -> float:
    """Horizontal plate, L = A/P, W/(m²·K) — Incropera eqs. 9.30–9.32 (Lloyd & Moran).
    ``unstable``: hot face up / cold face down; otherwise hot face down / cold face up."""
    if dt <= 0.0:
        return 0.0
    ra, k = _rayleigh(dt, length, t_film)
    if unstable:
        nu = 0.54 * ra**0.25 if ra <= 1e7 else 0.15 * ra ** (1.0 / 3.0)
    else:
        nu = 0.52 * ra**0.2
    return nu * k / length


def _h_rad(t_s: float, t_a: float) -> float:
    """Radiation coefficient of a grey surface in large surroundings — exact linearisation."""
    return EPSILON * SIGMA * (t_s * t_s + t_a * t_a) * (t_s + t_a)


@dataclass(frozen=True)
class _Face:
    name: str  # "sides" | "top" | "bottom"
    a_out: float  # m²
    a_in: float  # m²
    l_out: float  # characteristic length, m
    l_in: float


def _faces(inp: ThermalInputs) -> tuple[_Face, ...]:
    x, y, z = (d / 1000.0 for d in inp.outer_mm)
    t = inp.wall_mm / 1000.0
    xi, yi, zi = x - 2 * t, y - 2 * t, z - 2 * t
    top_o, top_i = x * y, xi * yi
    l_o, l_i = top_o / (2 * (x + y)), top_i / (2 * (xi + yi))
    return (_Face("sides", 2 * (x + y) * z, 2 * (xi + yi) * zi, z, zi),
            _Face("top", top_o, top_i, l_o, l_i),
            _Face("bottom", top_o, top_i, l_o, l_i))


def _face_heat(face: _Face, t_i: float, t_a: float, wall_m: float,
               k_wall: float) -> tuple[float, float, float]:
    """Heat through one face group (W) and its inner/outer surface temperatures (K)."""
    if t_i <= t_a:
        return 0.0, t_a, t_a
    r_wall = wall_m / (k_wall * math.sqrt(face.a_in * face.a_out))  # geometric-mean area, K/W
    # src: Incropera Table 4.1 case 10 — isothermal disc on a semi-infinite medium, S = 2·D,
    # with D the diameter of the disc of equal area (the box bottom on a wooden desk).
    g_desk = 2.0 * K_SUPPORT * 2.0 * math.sqrt(face.a_out / math.pi)

    def outside(t_so: float) -> float:
        dt = t_so - t_a
        if face.name == "bottom":
            return g_desk * dt
        t_f = 0.5 * (t_so + t_a)
        h = (_h_vertical(dt, face.l_out, t_f) if face.name == "sides"
             else _h_horizontal(dt, face.l_out, t_f, unstable=True))
        return (h + _h_rad(t_so, t_a)) * face.a_out * dt

    def inside(t_si: float) -> float:
        dt = t_i - t_si
        t_f = 0.5 * (t_i + t_si)
        if face.name == "sides":
            h = _h_vertical(dt, face.l_in, t_f)
        else:  # cold ceiling under warm air mixes (unstable); cold floor stratifies (stable)
            h = _h_horizontal(dt, face.l_in, t_f, unstable=face.name == "top")
        return h * face.a_in * dt

    def excess(t_so: float) -> float:
        q = outside(t_so)
        return inside(t_so + q * r_wall) - q

    t_so = _root(excess, t_a, t_i, 1e-9)
    q = outside(t_so)
    return q, t_so + q * r_wall, t_so


def _stack_flow(inp: ThermalInputs, dt: float, t_in: float) -> float:
    """Chimney flow through inlet + outlet vents, m³/s (CIBSE Guide A / ASHRAE stack equation)."""
    if dt <= 0.0 or inp.vent_in_mm2 <= 0 or inp.vent_out_mm2 <= 0 or inp.vent_height_mm <= 0:
        return 0.0
    a_in, a_out = inp.vent_in_mm2 * 1e-6, inp.vent_out_mm2 * 1e-6
    a_eff = 1.0 / math.sqrt(1.0 / a_in**2 + 1.0 / a_out**2)  # openings in series
    return CD_VENT * a_eff * math.sqrt(2.0 * G * inp.vent_height_mm * 1e-3 * dt / t_in)


def _fan_flow(inp: ThermalInputs, rho: float) -> float:
    """Fan operating point, m³/s: linear fan curve ``p = p_max·(1 − V/V_free)`` against the series
    openings (each ≥ LEAK_MM2) ``p = ρ/2·(V/(C_d·A_eff))²``, capped at ``FAN_DERATE·V_free``."""
    v_free, p_max = inp.fan_cfm * CFM_TO_M3S, inp.fan_static_pa
    if v_free <= 0.0 or p_max <= 0.0:
        return 0.0
    a_in = max(inp.vent_in_mm2, LEAK_MM2) * 1e-6
    a_out = max(inp.vent_out_mm2, LEAK_MM2) * 1e-6
    a_eff = 1.0 / math.sqrt(1.0 / a_in**2 + 1.0 / a_out**2)
    k, b = rho / (2.0 * (CD_VENT * a_eff) ** 2), p_max / v_free
    v_op = 2.0 * p_max / (b + math.sqrt(b * b + 4.0 * k * p_max))  # root of k·V² + b·V = p_max
    return min(FAN_DERATE * v_free, v_op)


def _solve(inp: ThermalInputs) -> ThermalResult:
    """Solve the energy balance; the result's report is empty and details hold faces/flows."""
    _validate(inp)
    mat = get_material(inp.material)
    t_a = inp.ambient_c + KELVIN
    wall = inp.wall_mm / 1000.0
    faces = _faces(inp)
    rho = P_ATM / (R_AIR * t_a)  # inflowing (ambient) air
    v_fan = _fan_flow(inp, rho)

    def balance(dt: float) -> float:
        t_i = t_a + dt
        q_walls = sum(_face_heat(f, t_i, t_a, wall, mat.thermal_conductivity)[0] for f in faces)
        flow = max(_stack_flow(inp, dt, t_i), v_fan)
        return q_walls + rho * CP_AIR * flow * dt - inp.power_w

    dt, clamped = 0.0, False
    if inp.power_w > 0.0:
        hi = 50.0
        while balance(hi) < 0.0 and hi < DT_MAX:
            hi = min(2.0 * hi, DT_MAX)
        clamped = balance(hi) < 0.0  # not even DT_MAX sheds the power: clamp, flag later
        dt = DT_MAX if clamped else _root(balance, 0.0, hi, 1e-8)
    t_i = t_a + dt
    stack = _stack_flow(inp, dt, t_i)
    flow = max(stack, v_fan)
    out = {}
    for f in faces:
        q, t_si, t_so = _face_heat(f, t_i, t_a, wall, mat.thermal_conductivity)
        out[f.name] = {"q_w": q, "inner_c": t_si - KELVIN, "outer_c": t_so - KELVIN,
                       "area_mm2": f.a_out * 1e6}
    details = {"faces": out, "stack_m3s": stack, "fan_m3s": v_fan, "clamped": clamped}
    return ThermalResult(internal_c=t_i - KELVIN, delta_c=dt,
                         q_walls_w=sum(v["q_w"] for v in out.values()),
                         q_vents_w=rho * CP_AIR * flow * dt, airflow_m3s=flow,
                         report=Report(title="thermal"), details=details)


# -- hints -----------------------------------------------------------------------------------------
def _ceil_to(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


def _sentence(parts: list[str]) -> str:
    """Join non-empty hint parts with '; ', upper-case the first letter, end with a period."""
    s = "; ".join(p for p in parts if p)
    return s[:1].upper() + s[1:] + "."


_OVERTEMP_FIRST = "Fix THERMAL.OVERTEMP first (check power_w, outer_mm and wall_mm)."


def _smallest(make: Callable[[float], ThermalInputs], target_c: float, lo: float, hi: float,
              xtol: float) -> float | None:
    """Smallest x in [lo, hi] whose box keeps the internal air ≤ target_c (None if hi fails)."""
    def excess(x: float) -> float:
        return _solve(make(x)).internal_c - target_c

    if excess(hi) > 0.0:
        return None
    if excess(lo) <= 0.0:
        return lo
    return _root(excess, lo, hi, xtol)


def _fan_inlet_mm2(cfm: float) -> float:
    return _ceil_to(cfm * CFM_TO_M3S * FAN_DERATE / FAN_INLET_SPEED * 1e6, 10.0)


def _airflow_options(inp: ThermalInputs, target_c: float) -> list[str]:
    """Quantified ventilation changes that keep the internal air ≤ target_c."""
    if target_c <= inp.ambient_c + 0.5:
        return [f"no ventilation can cool below the {inp.ambient_c:.0f} °C ambient"]

    def fan(c: float) -> ThermalInputs:  # a c-cfm fan with openings it is not starved by
        need = _fan_inlet_mm2(c)
        return replace(inp, fan_cfm=c, vent_in_mm2=max(inp.vent_in_mm2, need),
                       vent_out_mm2=max(inp.vent_out_mm2, need))

    if inp.fan_cfm > 0.0:
        need = _fan_inlet_mm2(inp.fan_cfm)
        if min(inp.vent_in_mm2, inp.vent_out_mm2) < need:
            t = _solve(fan(inp.fan_cfm)).internal_c
            if t <= target_c:
                return [f"give the fan ≥ {need:.0f} mm² of inlet and of outlet → ≈ {t:.0f} °C"]
        cfm = _smallest(fan, target_c, inp.fan_cfm, 50.0, 0.05)
        if cfm is None:
            return ["even a 50 cfm fan is not enough — reduce the power or split the box"]
        c = _ceil_to(cfm, 0.5)
        return [f"a stronger fan (≥ {c:.1f} cfm free air) with ≥ {_fan_inlet_mm2(c):.0f} mm² "
                f"of inlet and of outlet"]
    x, y, z = inp.outer_mm
    h_mm = inp.vent_height_mm if inp.vent_height_mm > 0 else round(0.75 * z)
    lo = min(inp.vent_in_mm2, inp.vent_out_mm2) if inp.vent_height_mm > 0 else 0.0
    hi = max(0.25 * x * y, lo)  # cap: a quarter of the top face per opening

    def vented(a: float) -> ThermalInputs:
        return replace(inp, vent_in_mm2=a, vent_out_mm2=a, vent_height_mm=h_mm)

    area = _smallest(vented, target_c, lo, hi, 1.0)
    if area is None:
        opts = [f"vents alone are not enough (2 × {hi:.0f} mm² → "
                f"{_solve(vented(hi)).internal_c:.0f} °C)"]
    else:
        opts = [f"add vents of ≥ {_ceil_to(area, 10.0):.0f} mm² each (inlet low, outlet high, "
                f"≥ {h_mm:.0f} mm apart)"]
    t_fan = _solve(replace(fan(FAN_30MM_CFM), fan_static_pa=FAN_30MM_STATIC_PA)).internal_c
    opts.append(f"a 30 mm fan (~{FAN_30MM_CFM:.0f} cfm) with ≥ "
                f"{_fan_inlet_mm2(FAN_30MM_CFM):.0f} mm² of inlet and of outlet → ≈ {t_fan:.0f} °C")
    return opts


def _better_materials(internal_c: float) -> str:
    """Up to three rigid materials that stay below their warning limit, cheapest first."""
    order = list(MATERIALS)
    ok = [m for m in MATERIALS.values()
          if m.name != "TPU95A" and softening_limits(m)[0] >= internal_c + MARGIN_C]
    ok = sorted(ok, key=lambda m: (m.cost_per_kg, order.index(m.name)))[:3]
    if not ok:
        return ""
    limits = "/".join(f"{softening_limits(m)[0]:.0f}" for m in ok)
    return f"print in {'/'.join(m.name for m in ok)} (limits {limits} °C)"


# -- findings --------------------------------------------------------------------------------------
def _check_material(report: Report, inp: ThermalInputs, sol: ThermalResult,
                    numbers: dict) -> bool:
    """THERMAL.MATERIAL_SOFTENING; returns True when raised."""
    m, ti = get_material(inp.material), sol.internal_c
    warn_c, err_c = softening_limits(m)
    if ti <= warn_c:
        return False
    err = ti >= err_c
    if not err:
        why = (f"above the {m.name} long-term limit of {warn_c:.0f} °C (max service "
               f"{m.max_service_c:.0f} °C − 5 K)")
    elif m.glass_transition_c >= m.max_service_c:
        why = (f"at or above {err_c:.0f} °C ({m.name} glass transition "
               f"{m.glass_transition_c:.0f} °C − 5 K): the walls will soften and creep")
    else:
        why = f"at or above the {m.name} maximum service temperature of {err_c:.0f} °C"
    hint = (_OVERTEMP_FIRST if sol.details["clamped"] else
            _sentence(_airflow_options(inp, warn_c - 0.5) + [_better_materials(ti)]))
    report.add("THERMAL.MATERIAL_SOFTENING", Severity.ERROR if err else Severity.WARNING,
               f"Inside air reaches {ti:.1f} °C (+{sol.delta_c:.1f} K over {inp.ambient_c:.0f} °C "
               f"at {inp.power_w:.1f} W), {why}.", subject=f"material:{m.name}", hint=hint,
               material=m.name, warn_above_c=warn_c, error_at_c=err_c, **numbers)
    return True


def _check_pi(report: Report, inp: ThermalInputs, sol: ThermalResult, pi: PiThermal, level: str,
              soc_rise_c: float | None, numbers: dict) -> tuple[float, float, bool]:
    """THERMAL.PI_THROTTLE / POWER_BELOW_IDLE; returns (soc_c, rise, problem)."""
    ti, ta, p = sol.internal_c, inp.ambient_c, inp.power_w
    share = min(1.0, p / pi.load_w) if pi.load_w > 0 else 1.0
    rise = float(soc_rise_c) if soc_rise_c is not None else pi.soc_rise(level) * share
    soc = ti + rise
    data = dict(board=pi.board, cooling=level, soc_c=soc, soc_rise_c=rise, throttle_c=pi.throttle_c,
                limit_c=pi.limit_c, soft_limit_c=pi.soft_limit_c, **numbers)
    est = (f"Estimated {pi.board} SoC {soc:.0f} °C (inside {ti:.1f} °C + {rise:.0f} K "
           f"{level if soc_rise_c is None else 'given'} SoC rise at {p:.1f} W)")
    problem = soc >= pi.throttle_c
    if problem:
        hard = soc >= pi.limit_c
        if soc_rise_c is None:
            opts = [f"{'a heatsink' if lv == 'heatsink' else 'a fan on the SoC'} → ≈ "
                    f"{ti + pi.soc_rise(lv) * share:.0f} °C"
                    for lv in COOLING[COOLING.index(level) + 1:]]
        else:
            opts = ["better SoC cooling (heatsink or fan)"]
        target = pi.throttle_c - rise - 0.5
        if sol.details["clamped"]:
            opts = [_OVERTEMP_FIRST[:-1]]
        elif target > ta + 0.5:
            opts.append(f"keep the inside ≤ {target:.0f} °C: "
                        + " or ".join(_airflow_options(inp, target)))
        else:
            opts.append(f"ventilation alone cannot help (a {rise:.0f} K SoC rise over "
                        f"{ta:.0f} °C ambient already reaches {pi.throttle_c:.0f} °C)")
        report.add("THERMAL.PI_THROTTLE", Severity.ERROR if hard else Severity.WARNING,
                   f"{est} ≥ {pi.limit_c if hard else pi.throttle_c:.0f} °C: "
                   + ("Arm cores and GPU throttle hard." if hard
                      else "the Arm cores throttle under sustained load."),
                   subject=f"board:{pi.board}", hint=_sentence(opts), **data)
    elif pi.soft_limit_c is not None and soc >= pi.soft_limit_c:
        report.add("THERMAL.PI_THROTTLE", Severity.INFO,
                   f"{est} ≥ {pi.soft_limit_c:.0f} °C soft limit: the CPU steps from 1.4 to "
                   f"1.2 GHz under sustained load.", subject=f"board:{pi.board}",
                   hint="Expected on the 3B+; raise temp_soft_limit (max 70) in config.txt or add "
                        "a heatsink/fan to stay at 1.4 GHz.", **data)
    if p < pi.idle_w:
        report.add("THERMAL.POWER_BELOW_IDLE", Severity.WARNING,
                   f"power_w = {p:.1f} W is below the {pi.board} idle draw of {pi.idle_w:.1f} W, "
                   f"so these temperatures are optimistic.", subject=f"board:{pi.board}",
                   hint=f"Include the Pi itself ({pi.idle_w:.1f} W idle, {pi.load_w:.1f} W at full "
                        f"load) plus every other load inside the box.",
                   idle_w=pi.idle_w, load_w=pi.load_w, power_w=p)
    return soc, rise, problem


# -- public API ----------------------------------------------------------------------------------
def enclosure_temperature(inp: ThermalInputs, *, board: str | PiThermal | None = "rpi4b",
                          soc_rise_c: float | None = None,
                          cooling: str | None = None) -> ThermalResult:
    """Steady-state internal air temperature of a box, with softening and Pi-throttle checks.

    ``board=None`` skips the SoC estimate. ``soc_rise_c`` overrides the SoC rise above the internal
    air (used as given); otherwise the board's rise for ``cooling`` (default: "fan" when the box
    has a fan, else "bare") is scaled by ``min(1, power_w / load_w)``. Findings (THERMAL.*):
    OVERTEMP, MATERIAL_SOFTENING, PI_THROTTLE, FAN_STARVED, POWER_BELOW_IDLE, OK (nothing hot).
    """
    if cooling is not None and cooling not in COOLING:
        raise ValidationError(f"cooling must be one of {', '.join(COOLING)}; got {cooling!r}")
    if soc_rise_c is not None and (not math.isfinite(soc_rise_c) or soc_rise_c < 0):
        raise ValidationError(f"soc_rise_c must be a finite rise ≥ 0 K; got {soc_rise_c!r}")
    pi = get_pi_thermal(board) if board is not None else None
    res = _solve(inp)
    m, ti, ta, p = get_material(inp.material), res.internal_c, inp.ambient_c, inp.power_w
    warn_c, err_c = softening_limits(m)
    report, stack, fan = res.report, res.details["stack_m3s"], res.details["fan_m3s"]
    numbers = dict(internal_c=ti, delta_c=res.delta_c, ambient_c=ta, power_w=p,
                   q_walls_w=res.q_walls_w, q_vents_w=res.q_vents_w, airflow_m3s=res.airflow_m3s)
    clamped = res.details["clamped"]
    if clamped:
        x, y, z = inp.outer_mm
        report.add("THERMAL.OVERTEMP", Severity.ERROR,
                   f"{p:g} W cannot be shed by this {x:g} × {y:g} × {z:g} mm box: even at "
                   f"{ti:.0f} °C inside ({DT_MAX:.0f} K over ambient) walls and vents carry only "
                   f"{res.q_walls_w + res.q_vents_w:.1f} W, so the results are clamped there.",
                   subject="enclosure", hint="Check the inputs first — power_w in W (not mW), "
                   "outer_mm and wall_mm in mm; if they are right, the box needs far more surface "
                   "or forced cooling.", shed_w=res.q_walls_w + res.q_vents_w, **numbers)
    problem = _check_material(report, inp, res, numbers) or clamped
    rise = level = None
    if pi is not None:
        level = cooling or ("fan" if inp.fan_cfm > 0 else "bare")
        res.soc_c, rise, pi_problem = _check_pi(report, inp, res, pi, level, soc_rise_c, numbers)
        problem = problem or pi_problem

    derated = FAN_DERATE * inp.fan_cfm * CFM_TO_M3S  # what the fan gives through ample openings
    if derated > 0 and fan < FAN_STARVED_FRACTION * derated:  # flow-based (controller ruling)
        need, smallest = _fan_inlet_mm2(inp.fan_cfm), min(inp.vent_in_mm2, inp.vent_out_mm2)
        report.add("THERMAL.FAN_STARVED", Severity.WARNING,
                   f"The {inp.fan_cfm:.1f} cfm fan is starved: it moves {fan * 1000:.2f} L/s, "
                   f"{fan / derated:.0%} of the {derated * 1000:.2f} L/s it gives through ample "
                   f"openings (< {FAN_STARVED_FRACTION:.0%}); smallest opening {smallest:.0f} mm², "
                   f"recommended ≥ {need:.0f} mm² (inside {ti:.1f} °C).", subject="fan",
                   hint=f"Give it ≥ {need:.0f} mm² of inlet and of outlet (its own aperture counts "
                        f"on its side; air ≤ {FAN_INLET_SPEED:g} m/s); without vents only port/lid "
                        f"gaps (≈ {LEAK_MM2:.0f} mm² assumed) feed it.",
                   fan_cfm=inp.fan_cfm, fan_m3s=fan, derated_m3s=derated,
                   flow_fraction=fan / derated, need_mm2=need, smallest_mm2=smallest, **numbers)

    if not problem:
        msg = (f"Inside air {ti:.1f} °C (+{res.delta_c:.1f} K over {ta:.0f} °C) at {p:.1f} W: "
               f"walls {res.q_walls_w:.2f} W, vents {res.q_vents_w:.2f} W "
               f"({res.airflow_m3s * 1000:.2f} L/s); below the {m.name} limit of {warn_c:.0f} °C")
        hint = f"No action needed: {warn_c - ti:.0f} K margin to softening"
        if pi is not None and res.soc_c is not None:
            msg += f"; est. {pi.board} SoC {res.soc_c:.0f} °C < {pi.throttle_c:.0f} °C throttle"
            hint += f", {pi.throttle_c - res.soc_c:.0f} K to throttling"
        report.add("THERMAL.OK", Severity.INFO, msg + ".", subject="enclosure", hint=hint + ".",
                   material=m.name, warn_above_c=warn_c, soc_c=res.soc_c, **numbers)

    res.details.update(numbers, material=m.name, warn_above_c=warn_c, error_at_c=err_c,
                       airflow_source="fan" if fan > stack else "stack" if stack > 0 else "none",
                       board=pi.board if pi else None, cooling=level, soc_rise_c=rise,
                       soc_c=res.soc_c)
    return res
