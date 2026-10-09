"""ngspice runner: raw-file parsing, measures, error handling, paths with spaces."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from piforge.core.errors import PiForgeError, ToolNotFoundError
from piforge.spice import runner
from piforge.spice.netlist import Pulse, SpiceCircuit, SpiceValueError
from piforge.spice.runner import (
    NgspiceNotFoundError,
    SimResult,
    SpiceError,
    find_ngspice,
    parse_ascii_raw,
    run,
)

# Two analyses in one ASCII raw file, the way ngspice writes them with `set appendwrite`.
RAW_TWO_PLOTS = """Title: two plots
Date: Sun Oct  4 04:50:21  2026
Command: ngspice-47, Build
Plotname: Operating Point
Flags: real
No. Variables: 3
No. Points: 1
Variables:
\t0\tv(in)\tvoltage
\t1\tv(out)\tvoltage
\t2\ti(v1)\tcurrent
Values:
 0\t3.300000000000000e+00
\t2.200000000000000e+00
\t-1.100000000000000e-03

Title: two plots
Date: Sun Oct  4 04:50:21  2026
Command: ngspice-47, Build
Plotname: AC Analysis
Flags: complex
No. Variables: 2
No. Points: 3
Variables:
\t0\tfrequency\tfrequency grid=3
\t1\tV(OUT)\tvoltage
Values:
 0\t1.000000000000000e+00,0.000000000000000e+00
\t1.000000000000000e+00,-1.000000000000000e-02
 1\t1.000000000000000e+01,0.000000000000000e+00
\t9.000000000000000e-01,-3.000000000000000e-01
 2\t1.000000000000000e+02,0.000000000000000e+00
\t1.000000000000000e-01,-3.000000000000000e-01
"""


def test_parse_ascii_raw_multi():
    plots = parse_ascii_raw(RAW_TWO_PLOTS)
    assert [p["kind"] for p in plots] == ["op", "ac"]
    assert plots[0]["name"] == "Operating Point"
    op = plots[0]["vectors"]
    assert set(op) == {"v(in)", "v(out)", "i(v1)"}
    assert op["v(out)"][0] == pytest.approx(2.2)
    ac = plots[1]["vectors"]
    assert list(ac) == ["frequency", "v(out)"]  # names lower-cased, order kept
    assert ac["frequency"].dtype.kind == "f"  # frequency axis made real
    np.testing.assert_allclose(ac["frequency"], [1.0, 10.0, 100.0])
    assert ac["v(out)"].dtype.kind == "c"
    assert ac["v(out)"][1] == pytest.approx(0.9 - 0.3j)
    assert plots[1]["types"]["v(out)"] == "voltage"


def test_parse_ascii_raw_rejects_binary_and_garbage():
    with pytest.raises(SpiceError):
        parse_ascii_raw("Title: x\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 1\n"
                        "No. Points: 1\nVariables:\n\t0\ttime\ttime\nBinary:\n\x00\x01")
    with pytest.raises(SpiceError):
        parse_ascii_raw("Title: x\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 2\n"
                        "No. Points: 1\nVariables:\n\t0\ttime\ttime\n\t1\tv(a)\tvoltage\n"
                        "Values:\n 0\t0.0\n\tnot-a-number\n")
    assert parse_ascii_raw("") == []


def test_error_classes():
    assert issubclass(NgspiceNotFoundError, ToolNotFoundError)
    assert issubclass(SpiceError, PiForgeError)


def test_ngspice_not_found(monkeypatch):
    monkeypatch.setattr(runner, "find_ngspice", lambda: None)
    c = SpiceCircuit("x")
    c.V("1", "a", "0", 1)
    c.R("1", "a", "0", 1)
    c.op()
    with pytest.raises(NgspiceNotFoundError, match="brew install ngspice"):
        run(c)


def test_invalid_circuit_rejected_before_ngspice(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("ngspice must not be started for an invalid circuit")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    c = SpiceCircuit("no analysis")
    c.V("1", "a", "0", 1)
    c.R("1", "a", "0", 1)
    with pytest.raises(SpiceValueError):
        run(c)
    with pytest.raises(SpiceValueError, match="(?i)analysis"):
        run("title\nV1 a 0 1\nR1 a 0 1\n.end\n")
    with pytest.raises(SpiceValueError, match="(?i)control"):
        run("title\nV1 a 0 1\nR1 a 0 1\n.op\n.control\nrun\n.endc\n.end\n")


def _rc_step(r: float = 1e3, c: float = 1e-6, v: float = 3.3) -> SpiceCircuit:
    ckt = SpiceCircuit("rc step")
    ckt.V("1", "in", "0", Pulse(0, v, td=0, tr=1e-9, tf=1e-9, pw=1.0, per=2.0))
    ckt.R("1", "in", "out", r)
    ckt.C("1", "out", "0", c)
    ckt.tran(1e-6, 3e-3)
    ckt.measure("vtau", "tran FIND v(out) AT=1m")
    ckt.measure("never", "tran FIND v(out) WHEN v(out)=5 RISE=1")
    return ckt


@pytest.mark.spice
def test_rc_tau(ngspice_path):
    # Physics: v(t) = V·(1 − e^(−t/RC)); at t = RC = 1 ms → 3.3·(1 − e⁻¹) = 2.0860 V.
    res = run(_rc_step())
    expected = 3.3 * (1 - math.exp(-1))
    assert res.measures["vtau"] == pytest.approx(expected, rel=0.01)
    t, vout = res.vectors["time"], res.vectors["v(out)"]
    assert float(np.interp(1e-3, t, vout)) == pytest.approx(expected, rel=0.01)
    assert math.isnan(res.measures["never"])  # failed .meas → NaN, not an exception
    assert res["v(out)"] is vout and res.plot("tran")["time"] is t
    assert "ngspice" in res.log.lower() and res.netlist.startswith("rc step")


@pytest.mark.spice
def test_rc_ac_corner(ngspice_path):
    # Physics: first-order RC low-pass, −3 dB at f_c = 1/(2πRC) = 159.15 Hz for 1 kΩ / 1 µF.
    ckt = SpiceCircuit("rc ac")
    ckt.V("1", "in", "0", 0.0, ac=1)
    ckt.R("1", "in", "out", 1e3)
    ckt.C("1", "out", "0", 1e-6)
    ckt.ac(50, 1, 1e5)
    ckt.measure("f3db", "ac WHEN vdb(out)=-3.0103 FALL=1")
    res = run(ckt)
    fc = 1 / (2 * math.pi * 1e3 * 1e-6)
    assert res.measures["f3db"] == pytest.approx(fc, rel=0.03)
    f, h = res.vectors["frequency"], res.vectors["v(out)"]
    db = 20 * np.log10(np.abs(h))
    assert float(np.interp(-3.0103, db[::-1], f[::-1])) == pytest.approx(fc, rel=0.03)


@pytest.mark.spice
def test_multiple_analyses_all_parsed(ngspice_path):
    ckt = _rc_step()
    ckt.op()
    res = run(ckt)
    assert [p["kind"] for p in res.plots] == ["tran", "op"]
    assert "time" not in res.vectors  # `vectors` = last analysis (op)
    assert res.plot("tran")["v(out)"].size > 100


@pytest.mark.spice
def test_spaced_workdir(ngspice_path, spaced_tmp):
    # Review Focus #1: paths with spaces, '~' and non-ASCII characters.
    res = run(_rc_step(), workdir=spaced_tmp)
    assert res.measures["vtau"] == pytest.approx(2.086, rel=0.01)
    assert (spaced_tmp / "circuit.cir").is_file()
    assert (spaced_tmp / "out.raw").is_file()
    assert (spaced_tmp / "ngspice.log").is_file()


@pytest.mark.spice
def test_raw_netlist_string(ngspice_path):
    text = """divider from text
V1 in 0 DC 5
R1 in out 1k
R2 out 0 2k
.op
.end
"""
    res = run(text)
    assert isinstance(res, SimResult)
    assert res.vectors["v(out)"][0] == pytest.approx(10 / 3, rel=1e-6)


@pytest.mark.spice
@pytest.mark.parametrize(
    "netlist, pattern",
    [
        ("bad model\nV1 a 0 1\nR1 a b 1k\nD1 b 0 nosuchmodel\n.op\n.end\n", "(?i)model"),
        ("bad subckt\nV1 a 0 1\nX1 a 0 nosuchsub\n.op\n.end\n", "(?i)subckt"),
        ("floating node\nV1 in 0 3.3\nR1 in out 1k\nC1 out x 1u\nC2 x 0 1u\nI1 x 0 1m\n.op\n.end\n",
         "(?i)singular"),
        ("timestep\nC1 x 0 1n IC=0\nR1 x 0 1k\nB1 0 x I = V(x) < 0.5 ? 1 : -1\n.tran 1n 10u uic\n.end\n",
         "(?i)timestep too small"),
    ],
)
def test_ngspice_failures_raise_spice_error_with_log(ngspice_path, netlist, pattern):
    with pytest.raises(SpiceError, match=pattern) as exc:
        run(netlist)
    assert exc.value.log  # full log attached for diagnosis
    assert exc.value.netlist.startswith(netlist.splitlines()[0])


@pytest.mark.spice
def test_out_of_memory_reported(ngspice_path):
    huge = SpiceCircuit("huge")
    huge.V("1", "in", "0", 1.0)
    huge.R("1", "in", "0", 1e3)
    huge.tran(1e-9, 10.0)  # ngspice pre-allocates 1e10 output points
    with pytest.raises(SpiceError, match="(?i)memory"):
        run(huge, timeout=30)


@pytest.mark.spice
def test_timeout_kills_ngspice(ngspice_path):
    slow = SpiceCircuit("slow")
    slow.V("1", "in", "0", Pulse(0, 1, tr=1e-9, tf=1e-9, pw=5e-7, per=1e-6))
    slow.R("1", "in", "out", 1e3)
    slow.C("1", "out", "0", 1e-9)
    slow.tran(1e-6, 10e-3, tmax=1e-11)  # 1e9 internal steps: would run for many minutes
    t0 = time.monotonic()
    with pytest.raises(SpiceError, match="(?i)timed out|timeout"):
        run(slow, timeout=1.5)
    assert time.monotonic() - t0 < 10


def test_find_ngspice_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "ngspice"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PIFORGE_NGSPICE", str(fake))
    assert find_ngspice() == str(fake)
    monkeypatch.setenv("PIFORGE_NGSPICE", str(tmp_path / "missing"))
    found = find_ngspice()
    assert found is None or found != str(tmp_path / "missing")


@pytest.mark.spice
def test_measure_on_unknown_vector_is_nan_not_fatal(ngspice_path, caplog):
    text = """meas on a missing vector
V1 in 0 1
R1 in out 1k
C1 out 0 1u
.tran 1u 1m
.meas tran bad FIND v(nonexist) AT=0.5m
.meas tran good FIND v(out) AT=0.5m
.end
"""
    with caplog.at_level("WARNING", logger="piforge.spice.runner"):
        res = run(text)
    assert math.isnan(res.measures["bad"])
    assert res.measures["good"] == pytest.approx(1.0, abs=0.01)
    assert any("bad" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.spice
def test_unrelated_error_stays_fatal_next_to_measures(ngspice_path):
    text = "bad model\nV1 a 0 1\nR1 a b 1k\nD1 b 0 nosuchmodel\n.tran 1u 1m\n.meas tran x FIND v(b) AT=0.5m\n.end\n"
    with pytest.raises(SpiceError):
        run(text)
