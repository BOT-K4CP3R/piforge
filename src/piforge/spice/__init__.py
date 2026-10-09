"""SPICE simulation with ngspice: netlist builder, device models, runner, benches and plots.

Quick start::

    from piforge.spice import SpiceCircuit, Pulse, run, run_bench, plot_result

    c = SpiceCircuit("RC step")
    c.V("1", "in", "0", Pulse(0, 3.3, pw=1, per=2))
    c.R("1", "in", "out", 1e3)
    c.C("1", "out", "0", 1e-6)
    c.tran(1e-6, 3e-3)
    c.measure("vtau", "tran FIND v(out) AT=1m")
    res = run(c)                      # res.measures["vtau"] ≈ 2.086 V, res.vectors["v(out)"]

    bench = run_bench("led_driver", r_series=330, led="led_red")
    print(bench.report.to_markdown()); plot_result(bench, "led.png")

Modules: :mod:`.netlist` (builder), :mod:`.models` (device library + datasheet values),
:mod:`.runner` (ngspice batch runner, raw parser), :mod:`.benches` (parametrised benches),
:mod:`.plot` (PNG plots; matplotlib is imported lazily).
"""

from piforge.spice.benches import BENCHES, Bench, BenchResult, ParamSpec, bench_catalog, run_bench
from piforge.spice.errors import BenchParamError, NgspiceNotFoundError, SpiceError, SpiceValueError
from piforge.spice.models import MODEL_INFO, MODELS, gpio_resistances, list_models, model_info, model_text
from piforge.spice.netlist import PWL, Pulse, Sine, SpiceCircuit, fmt_value, parse_value
from piforge.spice.plot import plot_result, plot_sim
from piforge.spice.runner import SimResult, find_ngspice, parse_ascii_raw, run

__all__ = [
    "BENCHES", "MODELS", "MODEL_INFO", "PWL", "Bench", "BenchParamError", "BenchResult",
    "NgspiceNotFoundError", "ParamSpec", "Pulse", "SimResult", "Sine", "SpiceCircuit", "SpiceError",
    "SpiceValueError", "bench_catalog", "find_ngspice", "fmt_value", "gpio_resistances", "list_models",
    "model_info", "model_text", "parse_ascii_raw", "parse_value", "plot_result", "plot_sim", "run",
    "run_bench",
]
