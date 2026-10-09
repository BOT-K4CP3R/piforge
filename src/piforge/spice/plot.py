"""PNG plots of SPICE bench results and raw simulations.

matplotlib is imported inside the functions only (the object-oriented ``Figure`` API with the Agg
canvas, no pyplot global state), so importing :mod:`piforge.spice` stays light and thread-safe.
Series are grouped by the unit in their ``"name [unit]"`` label: one stacked panel per unit, a
side panel lists parameters, measured vs analytic values and the findings.
"""

from __future__ import annotations

import math
import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from piforge.spice.benchkit import BenchResult
    from piforge.spice.runner import SimResult

__all__ = ["plot_result", "plot_sim", "split_label"]

_LABEL_RE = re.compile(r"^(.*?)\s*\[([^\]]*)\]\s*$")
_SI_UNITS = {"V", "A", "s", "Hz", "Ω", "W", "F", "H"}
_SEV_COLOUR = {"error": "#c62828", "warning": "#e65100", "info": "#1565c0"}
_QUANTITY = {"V": "voltage", "A": "current", "mA": "current", "s": "time", "Hz": "frequency",
             "dB": "gain", "deg": "phase", "W": "power", "Ω": "resistance", "F": "capacitance"}
_MAX_PANELS = 4


def split_label(label: str) -> tuple[str, str]:
    """``"V(out) [V]"`` → (``"V(out)"``, ``"V"``); a label without brackets has unit ``""``."""
    m = _LABEL_RE.match(label)
    return (m.group(1), m.group(2)) if m else (label, "")


def _fmt(value: float, unit: str) -> str:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    if unit in _SI_UNITS:
        from matplotlib.ticker import EngFormatter

        return EngFormatter(unit=unit, places=3, sep=" ")(value).replace("−", "-")
    return f"{value:.4g} {unit}".strip()


def _draw(path: Path, title: str, subtitle: str, x: np.ndarray, x_label: str, x_scale: str,
          series: dict[str, np.ndarray], side: list[tuple[str, str]]) -> Path:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import EngFormatter

    groups: dict[str, list[tuple[str, np.ndarray]]] = {}
    for label, y in series.items():
        name, unit = split_label(label)
        groups.setdefault(unit, []).append((name, np.asarray(y, dtype=float)))
    units = list(groups)[:_MAX_PANELS]
    n = max(1, len(units))
    height = 2.7 * n + 1.9  # inches: panels + title band + x-axis label
    fig = Figure(figsize=(12.5, height), dpi=110)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(n, 2, width_ratios=[3.1, 1.6], hspace=0.10, wspace=0.08,
                          left=0.085, right=0.985, top=1 - 1.0 / height, bottom=0.65 / height)
    x_name, x_unit = split_label(x_label)
    axes = []
    for i, unit in enumerate(units or [""]):
        ax = fig.add_subplot(gs[i, 0], sharex=axes[0] if axes else None)
        axes.append(ax)
        for name, y in groups.get(unit, []):
            ax.plot(x, y, lw=1.5, label=name)
        ax.set_ylabel(f"{_QUANTITY.get(unit, 'value')} [{unit}]" if unit else "value")
        if unit in _SI_UNITS:
            ax.yaxis.set_major_formatter(EngFormatter(unit=unit, sep=" "))
        ax.grid(True, which="both", alpha=0.3)
        if groups.get(unit):
            ax.legend(loc="best", fontsize=9, framealpha=0.85)
        if x_scale == "log":
            ax.set_xscale("log")
        if i < n - 1:
            ax.tick_params(labelbottom=False)
    if x_unit in _SI_UNITS:
        axes[-1].xaxis.set_major_formatter(EngFormatter(unit=x_unit, sep=" "))
    axes[-1].set_xlabel(f"{x_name} [{x_unit}]" if x_unit else x_name)
    fig.text(0.085, 1 - 0.32 / height, title, fontsize=14, fontweight="bold", ha="left", va="center")
    fig.text(0.085, 1 - 0.66 / height, subtitle, fontsize=9, color="#444444", ha="left", va="center")
    side_ax = fig.add_subplot(gs[:, 1])
    side_ax.axis("off")
    y = 1.0
    line_h = 0.032 if n > 1 else 0.05
    for text, colour in side:
        for k, line in enumerate(textwrap.wrap(text, 58) or [""]):
            weight = "bold" if colour == "head" and k == 0 else "normal"
            side_ax.text(0.0, y, line, fontsize=8.6, va="top", ha="left", family="DejaVu Sans",
                         color="#111111" if colour in ("head", "") else colour, weight=weight,
                         transform=side_ax.transAxes)
            y -= line_h
            if y < 0.0:
                break
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="png")
    return path


def plot_result(result: BenchResult, path: str | Path) -> Path:
    """Write a PNG of a bench result (traces by unit + parameters, measures, findings); returns the path."""
    path = Path(path)
    x = np.asarray(result.traces[result.x_label], dtype=float)
    series = {k: np.asarray(v, dtype=float) for k, v in result.traces.items() if k != result.x_label}
    err = result.errors_pct()
    side: list[tuple[str, str]] = [("Measured  (analytic, error)", "head")]
    for k, v in result.measures.items():
        unit = result.units.get(k, "")
        line = f"{k} = {_fmt(v, unit)}"
        if k in result.analytic:
            line += f"  ({_fmt(result.analytic[k], unit)}"
            line += f", {err[k]:+.1f} %)" if k in err else ")"
        side.append((line, ""))
    side.append(("", ""))
    side.append(("Findings", "head"))
    for f in sorted(result.report.findings, key=lambda f: -int(f.severity)):
        side.append((f"{f.severity.name}: {f.code} — {f.message}", _SEV_COLOUR[f.severity.label]))
    params = ", ".join(f"{k}={_param_txt(v)}" for k, v in result.params.items())
    subtitle = textwrap.shorten(f"{result.key}: {params}", 160)
    status = "FAIL" if not result.report.ok else ("PASS with warnings" if result.report.warnings else "PASS")
    return _draw(path, f"{result.title} — {status}", subtitle, x, result.x_label, result.x_scale, series, side)


def _param_txt(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    return f"{v:g}"


def plot_sim(sim: SimResult, path: str | Path, vectors: list[str] | None = None, *, title: str = "",
             kind: str | None = None) -> Path:
    """Plot vectors of one analysis of a raw :class:`SimResult` (default: last analysis, all node
    voltages and source currents; AC vectors are shown as magnitude in dB)."""
    plot = sim.plot(kind) if kind else sim.vectors
    names = list(plot)
    if not names:
        raise ValueError("simulation has no vectors to plot")
    x_name = names[0]
    x = np.asarray(plot[x_name]).real
    chosen = vectors or [n for n in names[1:] if n.startswith(("v(", "i("))]
    series: dict[str, np.ndarray] = {}
    for n in chosen:
        y = np.asarray(plot[n.lower()])
        if np.iscomplexobj(y):
            series[f"|{n}| [dB]"] = 20 * np.log10(np.maximum(np.abs(y), 1e-30))
        else:
            series[f"{n} [{'A' if n.startswith('i(') else 'V'}]"] = y
    x_unit = {"time": "s", "frequency": "Hz"}.get(x_name, "V")
    x_scale = "log" if x_name == "frequency" else "linear"
    return _draw(Path(path), title or "SPICE simulation", f"{len(chosen)} vector(s) vs {x_name}", x,
                 f"{x_name} [{x_unit}]", x_scale, series, [])
