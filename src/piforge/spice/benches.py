"""Parametrised SPICE test benches for the small analog parts of a Raspberry Pi project.

    >>> from piforge.spice.benches import BENCHES, run_bench
    >>> res = run_bench("led_driver", r_series=330, led="led_red")
    >>> res.measures["i_led"], res.analytic["i_led"], res.report.ok

Benches: ``led_driver``, ``voltage_divider``, ``rc_filter``, ``button_debounce``,
``mosfet_lowside``, ``bjt_switch``, ``level_shifter_bss138``, ``i2c_rise_time``, ``power_path``.
Every input is described by a :class:`ParamSpec` (GUI form / CLI ``-p k=v``); values are
validated before ngspice starts (:class:`BenchParamError`). Results carry SI ``measures``, textbook
``analytic`` values, a :class:`~piforge.core.report.Report` of ``SPICE.*`` findings and JSON-ready
``traces`` (≤ 2000 points).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from piforge.core.errors import NotFoundError
from piforge.spice.bench_basic import BASIC_BENCHES
from piforge.spice.bench_switching import SWITCHING_BENCHES
from piforge.spice.benchkit import (
    MAX_TRACE_POINTS,
    Bench,
    BenchResult,
    ParamSpec,
    downsample,
    floats,
)
from piforge.spice.errors import BenchParamError, NgspiceNotFoundError, SpiceError
from piforge.spice.runner import run

__all__ = ["BENCHES", "Bench", "BenchParamError", "BenchResult", "NgspiceNotFoundError", "ParamSpec",
           "SpiceError", "bench_catalog", "downsample", "get_bench", "run_bench"]

log = logging.getLogger(__name__)

BENCHES: dict[str, Bench] = {b.key: b for b in (*BASIC_BENCHES, *SWITCHING_BENCHES)}


def get_bench(key: str) -> Bench:
    """The :class:`Bench` registered under ``key`` (NotFoundError lists close matches)."""
    if key not in BENCHES:
        raise NotFoundError("SPICE bench", key, BENCHES)
    return BENCHES[key]


def bench_catalog() -> list[dict]:
    """JSON-ready description of every bench (key, title, description, params, units) for the GUI."""
    return [b.to_dict() for b in BENCHES.values()]


def run_bench(key: str, *, timeout: float | None = None, **params: Any) -> BenchResult:
    """Validate ``params``, build and simulate bench ``key`` and evaluate it.

    Raises :class:`BenchParamError` (bad parameter, before simulating), NotFoundError (unknown
    bench), :class:`~piforge.spice.errors.SpiceError` (ngspice failure, with log) and
    :class:`~piforge.spice.errors.NgspiceNotFoundError`.
    """
    bench = get_bench(key)
    values = bench.validate(params)
    circuit = bench.build(values)
    log.info("running SPICE bench %s with %s", key, values)
    sim = run(circuit, timeout=timeout if timeout is not None else bench.timeout)
    measures, analytic, report = bench.evaluate(sim, values)
    measures = {k: float(v) for k, v in measures.items()}
    analytic = {k: float(v) for k, v in analytic.items() if v is not None}
    if bench.traces is not None:
        x_label, x, series, x_scale = bench.traces(sim, values)
    else:  # fall back to every vector of the last analysis against its first vector
        names = list(sim.vectors)
        x_label, x, x_scale = names[0], sim.vectors[names[0]], "linear"
        series = {n: sim.vectors[n] for n in names[1:]}
    xs, ys = downsample(x, series, MAX_TRACE_POINTS)
    traces = {x_label: floats(xs), **{k: floats(v) for k, v in ys.items()}}
    for k, v in measures.items():
        if not math.isfinite(v):
            log.info("bench %s: measure %s is %s", key, k, v)
    return BenchResult(key=key, params=values, measures=measures, analytic=analytic, report=report,
                       traces=traces, x_label=x_label, sim=sim, title=bench.title,
                       units=dict(bench.units), x_scale=x_scale)
