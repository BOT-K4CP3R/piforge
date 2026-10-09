"""Building blocks for SPICE benches: parameter specs, bench/result types and waveform helpers.

The concrete benches live in :mod:`piforge.spice.bench_basic` and
:mod:`piforge.spice.bench_switching`; :mod:`piforge.spice.benches` registers them and runs them.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from piforge.core.errors import suggest
from piforge.core.report import Report, jsonable
from piforge.spice.errors import BenchParamError, SpiceValueError
from piforge.spice.models import gpio_resistances
from piforge.spice.netlist import PWL, SpiceCircuit, parse_value
from piforge.spice.runner import SimResult

MAX_TRACE_POINTS = 2000
_TRUE = {"true", "1", "yes", "on", "y", "t"}
_FALSE = {"false", "0", "no", "off", "n", "f"}

# src: raspberrypi/documentation gpio-pad-controls.adoc — "All the electronics of the pads are
# designed for 16 mA. This is a safe value under which you will not damage the device."
GPIO_SAFE_A = 0.016
# src: same document — the 3.3 V GPIO supply level of every current Pi.
PI_VDD_IO = 3.3


@dataclass(frozen=True)
class ParamSpec:
    """One bench input, fully described for a GUI form or the CLI.

    ``default`` decides the type: bool → checkbox, str/number with ``choices`` → select,
    number → numeric field limited to [``min``, ``max``] (inclusive) in ``unit``.
    """

    default: float | str | bool
    min: float | None = None
    max: float | None = None
    unit: str = ""
    choices: tuple = ()
    label: str = ""
    help: str = ""

    @property
    def kind(self) -> str:
        """``bool``, ``choice``, ``number`` or ``text``."""
        if isinstance(self.default, bool):
            return "bool"
        if self.choices:
            return "choice"
        if isinstance(self.default, (int, float)):
            return "number"
        return "text"

    def to_dict(self) -> dict:
        """JSON-ready description (type, default, range, unit, choices, label, help)."""
        return jsonable({"type": self.kind, "default": self.default, "min": self.min, "max": self.max,
                         "unit": self.unit, "choices": list(self.choices), "label": self.label,
                         "help": self.help})

    def coerce(self, name: str, value: Any) -> float | str | bool:
        """Convert a Python/GUI/CLI value (``"4.7k"``, ``"false"``) to the param type, or raise."""
        kind = self.kind
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
                return value.strip().lower() in _TRUE
            raise BenchParamError(f"{name} must be true/false, got {value!r}")
        if kind == "choice":
            return self._choice(name, value)
        if kind == "text":
            return str(value)
        if isinstance(value, bool):
            raise BenchParamError(f"{name} must be a number{self._unit_txt()}, got {value!r}")
        try:
            v = parse_value(value)
        except SpiceValueError:
            raise BenchParamError(f"{name} must be a number{self._unit_txt()} (e.g. 4.7k, 100n), "
                                  f"got {value!r}") from None
        lo = -math.inf if self.min is None else self.min
        hi = math.inf if self.max is None else self.max
        if not lo <= v <= hi:
            raise BenchParamError(f"{name} = {v:g}{self._unit_txt()} is outside the allowed range "
                                  f"[{lo:g}, {hi:g}]{self._unit_txt()}")
        return v

    def _unit_txt(self) -> str:
        return f" {self.unit}" if self.unit else ""

    def _choice(self, name: str, value: Any) -> float | str:
        if all(isinstance(c, str) for c in self.choices):
            s = str(value).strip().lower()
            for c in self.choices:
                if c.lower() == s:
                    return c
            hint = suggest(str(value), list(self.choices))
            raise BenchParamError(f"{name} = {value!r} is not one of {list(self.choices)}"
                                  + (f"; did you mean {', '.join(hint)}?" if hint else ""))
        try:
            v = parse_value(value) if not isinstance(value, bool) else math.nan
        except SpiceValueError:
            v = math.nan
        for c in self.choices:
            if not isinstance(c, str) and math.isclose(v, float(c), rel_tol=1e-9):
                return c
        raise BenchParamError(f"{name} = {value!r} is not one of {list(self.choices)}{self._unit_txt()}")


# evaluate(sim, params) -> (measures, analytic, report);
# traces(sim, params) -> (x_label, x, {series label: values}, x_scale)
Evaluate = Callable[[SimResult, dict], tuple[dict, dict, Report]]
TraceFn = Callable[[SimResult, dict], tuple[str, np.ndarray, dict[str, np.ndarray], str]]


@dataclass(frozen=True)
class Bench:
    """A parametrised test circuit: ``build(params)`` → circuit, ``evaluate`` → findings."""

    key: str
    title: str
    description: str
    params: dict[str, ParamSpec]
    build: Callable[[dict], SpiceCircuit]
    evaluate: Evaluate
    traces: TraceFn | None = None
    units: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0

    def validate(self, values: dict[str, Any]) -> dict[str, Any]:
        """Defaults filled in, values coerced; unknown names/out-of-range → :class:`BenchParamError`."""
        unknown = [k for k in values if k not in self.params]
        if unknown:
            hint = suggest(unknown[0], list(self.params))
            raise BenchParamError(f"unknown parameter {unknown[0]!r} for bench {self.key!r}"
                                  + (f"; did you mean {', '.join(hint)}?" if hint else
                                     f"; parameters: {', '.join(self.params)}"))
        out: dict[str, Any] = {}
        for name, spec in self.params.items():
            out[name] = spec.coerce(name, values[name]) if name in values else spec.default
        return out

    def to_dict(self) -> dict:
        """JSON-ready catalogue entry for the GUI (key, title, description, params, units)."""
        return {"key": self.key, "title": self.title, "description": self.description,
                "params": {k: p.to_dict() for k, p in self.params.items()}, "units": dict(self.units)}


@dataclass
class BenchResult:
    """Outcome of :func:`piforge.spice.benches.run_bench`.

    ``measures`` are simulated values in SI units (``units`` names them), ``analytic`` the
    textbook predictions for the same keys. ``traces`` is JSON-ready: the shared x axis under
    ``x_label`` plus series labelled ``"name [unit]"``, each ≤ 2000 floats.
    """

    key: str
    params: dict
    measures: dict[str, float]
    analytic: dict[str, float]
    report: Report
    traces: dict[str, list[float]]
    x_label: str
    sim: SimResult
    title: str = ""
    units: dict[str, str] = field(default_factory=dict)
    x_scale: str = "linear"

    def errors_pct(self) -> dict[str, float]:
        """(simulated − analytic)/analytic in % for every key with a non-zero analytic value."""
        out = {}
        for k, a in self.analytic.items():
            m = self.measures.get(k)
            if m is not None and a and math.isfinite(a) and math.isfinite(m):
                out[k] = (m - a) / abs(a) * 100.0
        return out

    def to_dict(self) -> dict:
        """Strict-JSON-ready dict (NaN/inf → null) without the raw simulation."""
        err = self.errors_pct()
        table = [{"name": k, "value": v, "unit": self.units.get(k, ""), "analytic": self.analytic.get(k),
                  "error_pct": err.get(k)} for k, v in self.measures.items()]
        return jsonable({"key": self.key, "title": self.title, "params": self.params,
                         "measures": self.measures, "analytic": self.analytic, "units": self.units,
                         "table": table, "report": self.report.to_dict(), "traces": self.traces,
                         "x_label": self.x_label, "x_scale": self.x_scale})


# ---------------------------------------------------------------------------- helpers -------
def downsample(x: np.ndarray, ys: dict[str, np.ndarray], max_points: int = MAX_TRACE_POINTS
               ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Reduce to ≤ ``max_points`` samples, keeping each series' min and max in every bucket.

    Peaks (e.g. a one-sample flyback spike) survive, the x order is kept, the first and last
    samples are always included.
    """
    x = np.asarray(x)
    n = len(x)
    if n <= max_points:
        return x, {k: np.asarray(v) for k, v in ys.items()}
    per_bucket = 1 + 2 * max(1, len(ys))
    nb = max(1, (max_points - 2) // per_bucket)
    edges = np.linspace(0, n, nb + 1).astype(int)
    keep = {0, n - 1}
    arrays = [np.asarray(v).real for v in ys.values()]
    for b in range(nb):
        lo, hi = int(edges[b]), int(edges[b + 1])
        if hi <= lo:
            continue
        keep.add(lo)
        for arr in arrays:
            seg = arr[lo:hi]
            keep.add(lo + int(np.argmin(seg)))
            keep.add(lo + int(np.argmax(seg)))
    idx = np.array(sorted(keep))
    if len(idx) > max_points:  # safety net; cannot happen with the bucket arithmetic above
        idx = idx[np.linspace(0, len(idx) - 1, max_points).astype(int)]
    return x[idx], {k: np.asarray(v)[idx] for k, v in ys.items()}


def floats(arr: np.ndarray, digits: int = 7) -> list[float]:
    """Plain Python floats rounded to ``digits`` significant digits (compact JSON)."""
    return [float(f"{float(v):.{digits}g}") for v in np.asarray(arr).real]


def at(sim: SimResult, vec: str, t: float, kind: str = "tran") -> float:
    """Value of transient vector ``vec`` at time ``t`` (linear interpolation)."""
    p = sim.plot(kind)
    return float(np.interp(t, p["time"], np.asarray(p[vec]).real))


def window(sim: SimResult, vec: str, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray]:
    """(time, values) of transient vector ``vec`` restricted to t0 ≤ t ≤ t1."""
    p = sim.plot("tran")
    t, v = p["time"], np.asarray(p[vec]).real
    m = (t >= t0) & (t <= t1)
    return t[m], v[m]


def crossing(t: np.ndarray, v: np.ndarray, level: float, rising: bool, after: float = -math.inf) -> float:
    """First time after ``after`` where ``v`` crosses ``level`` upward (or downward); NaN if never."""
    s = v - level
    for i in range(1, len(t)):
        if t[i] < after:
            continue
        a, b = s[i - 1], s[i]
        if (rising and a < 0 <= b) or (not rising and a > 0 >= b):
            return float(t[i - 1] + (t[i] - t[i - 1]) * (a / (a - b) if a != b else 0.0))
    return math.nan


def schmitt_edges(t: np.ndarray, v: np.ndarray, vil: float, vih: float) -> list[tuple[float, int]]:
    """Logic edges seen by a Schmitt-trigger input: [(time, new_level)], hysteresis band [vil, vih]."""
    state = 1 if v[0] >= (vil + vih) / 2 else 0
    edges = []
    for i in range(1, len(t)):
        vi = v[i]
        if (state == 1 and vi < vil) or (state == 0 and vi > vih):
            thr = vil if state == 1 else vih
            dv = vi - v[i - 1]
            frac = (thr - v[i - 1]) / dv if dv else 1.0  # interpolate the threshold crossing
            edges.append((float(t[i - 1] + (t[i] - t[i - 1]) * min(max(frac, 0.0), 1.0)), 1 - state))
            state = 1 - state
    return edges


def step_wave(t_on: float, t_off: float | None, level: float, *, edge: float = 1e-7,
              invert: bool = False) -> PWL:
    """PWL that is 0 (or ``level`` when ``invert``) except ``level`` (or 0) during [t_on, t_off]."""
    lo, hi = (level, 0.0) if invert else (0.0, level)
    pts = [(0.0, lo), (t_on, lo), (t_on + edge, hi)]
    if t_off is not None:
        pts += [(t_off, hi), (t_off + edge, lo)]
    return PWL(tuple(pts))


def add_gpio(c: SpiceCircuit, name: str, pin: str, wave: PWL, *, drive_ma: float = 8.0,
             vdd: str = "vio") -> None:
    """Pi GPIO pad ``gpio_out`` on node ``pin`` with control source ``Vctl<name>`` (needs ``vdd``)."""
    rh, rl = gpio_resistances(drive_ma)
    c.V(f"ctl{name}", f"ctl{name}", "0", wave)
    c.X(f"gpio{name}", [pin, vdd, "0", f"ctl{name}"], "gpio_out", params={"rh": rh, "rl": rl})


def summary(report: Report, message: str, **data: Any) -> None:
    """Add the bench's one-line ``SPICE.SUMMARY`` INFO finding."""
    report.add("SPICE.SUMMARY", "info", message, **data)


def mA(x: float) -> str:
    """Format amperes as milliamperes for messages."""
    return f"{x * 1e3:.3g} mA"
