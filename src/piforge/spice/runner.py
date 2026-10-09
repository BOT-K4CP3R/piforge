"""Run ngspice in batch mode and parse its ASCII raw output and ``.meas`` results.

:func:`run` writes ``circuit.cir`` into a work directory, appends a ``.control`` block that runs each
analysis explicitly and appends its vectors to ``out.raw`` (ASCII), then starts
``ngspice -n -b circuit.cir`` with ``cwd`` = the work directory (argv list, so paths with spaces or
non-ASCII characters are safe), enforces a timeout and turns ngspice's complaints into
:class:`SpiceError` with a log excerpt.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from piforge.core.errors import NotFoundError
from piforge.spice.errors import NgspiceNotFoundError, SpiceError, SpiceValueError
from piforge.spice.netlist import SpiceCircuit

__all__ = ["NgspiceNotFoundError", "SimResult", "SpiceError", "find_ngspice", "parse_ascii_raw", "run"]

log = logging.getLogger(__name__)

# src: Homebrew (Apple silicon / Intel) and Debian package locations of the ngspice binary.
_FALLBACK_PATHS = ("/opt/homebrew/bin/ngspice", "/usr/local/bin/ngspice", "/usr/bin/ngspice")
_INSTALL_HINT = ("ngspice not found. Install it (macOS: `brew install ngspice`, Debian/Ubuntu: "
                 "`sudo apt install ngspice`) or set PIFORGE_NGSPICE=/path/to/ngspice.")
_RAW_NAME = "out.raw"
_DECK_NAME = "circuit.cir"
_ANALYSIS_RE = re.compile(r"^\s*\.(op|dc|tran|ac|noise|tf|sens|pz|disto|sp)\b(.*)$", re.I)
_MEAS_DEF_RE = re.compile(r"^\s*\.meas(?:ure)?\s+\w+\s+([A-Za-z_]\w*)", re.I)
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_MEAS_VAL_RE = re.compile(rf"^\s*([A-Za-z_]\w*)\s*=\s*({_NUM})(?:\s|$)")
_MEAS_FAIL_RES = (re.compile(r"^\s*\.meas\w*\s+\w+\s+([A-Za-z_]\w*)\b.*failed", re.I),
                  re.compile(r"^\s*Error:\s*measure\s+([A-Za-z_]\w*)\b", re.I))
_FATAL = (
    (re.compile(r"error on line", re.I), "netlist syntax error"),
    (re.compile(r"could not find a valid modelname", re.I), "unknown device model"),
    (re.compile(r"unknown subckt", re.I), "unknown subcircuit"),
    (re.compile(r"singular matrix", re.I),
     "singular matrix: a node has no DC path to ground (add e.g. a 1e9 Ω resistor to 0)"),
    (re.compile(r"timestep too small", re.I),
     "timestep too small: the circuit did not converge (check ideal switches/discontinuities)"),
    (re.compile(r"iteration limit reached", re.I), "operating point did not converge"),
    (re.compile(r"simulation\(s\) aborted", re.I), "simulation aborted"),
    (re.compile(r"simulation interrupted due to error", re.I), "simulation interrupted"),
    (re.compile(r"no simulations run", re.I), "no simulation was run"),
    (re.compile(r"is not a valid .* line", re.I), "an element line was ignored as invalid"),
    (re.compile(r"not enough memory|can't allocate", re.I),
     "ngspice ran out of memory (too many points: use a larger tran step)"),
    (re.compile(r"\bfatal\b", re.I), "fatal ngspice error"),
    (re.compile(r"^\s*error\b(?!:\s*measure)", re.I), "ngspice reported an error"),
)
_PLOT_KINDS = {"operating point": "op", "dc transfer characteristic": "dc",
               "transient analysis": "tran", "ac analysis": "ac"}


@dataclass
class SimResult:
    """Parsed simulation output.

    ``vectors`` holds the LAST analysis (lower-case names such as ``"time"``, ``"v(out)"``,
    ``"i(v1)"``, ``"frequency"``; AC vectors are complex). ``plots`` lists every analysis in run
    order as ``{"name", "kind", "title", "flags", "vectors", "types"}``. ``measures`` maps
    lower-case ``.meas`` names to floats (NaN when the measurement failed).
    """

    vectors: dict[str, np.ndarray]
    measures: dict[str, float]
    plots: list[dict] = field(default_factory=list)
    netlist: str = ""
    log: str = ""

    def __getitem__(self, name: str) -> np.ndarray:
        key = str(name).lower()
        if key not in self.vectors:
            raise NotFoundError("vector", name, self.vectors)
        return self.vectors[key]

    def plot(self, kind: str) -> dict[str, np.ndarray]:
        """Vectors of the first analysis of ``kind`` (``"op"``, ``"dc"``, ``"tran"``, ``"ac"``)."""
        k = str(kind).lower()
        for p in self.plots:
            if p["kind"] == k or p["name"].lower() == k:
                return p["vectors"]
        raise NotFoundError("analysis", kind, [p["kind"] for p in self.plots])


def find_ngspice() -> str | None:
    """Path of the ngspice executable: ``$PIFORGE_NGSPICE``, then ``PATH``, then usual locations."""
    env = os.environ.get("PIFORGE_NGSPICE")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return str(Path(env))
    found = shutil.which("ngspice")
    if found:
        return found
    for cand in _FALLBACK_PATHS:
        if Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def _number(tok: str) -> float | complex:
    if "," in tok:
        re_, im = tok.split(",", 1)
        return complex(float(re_), float(im))
    return float(tok)


def parse_ascii_raw(text: str) -> list[dict]:
    """Parse an ngspice ASCII raw file (one or more analyses) into a list of plot dicts.

    Each dict: ``name`` (Plotname), ``kind`` (op/dc/tran/ac or the lower-case name), ``title``,
    ``flags``, ``vectors`` (lower-case name → ndarray; complex for ``Flags: complex`` except the
    frequency axis) and ``types`` (name → ``voltage``/``current``/``time``…).
    """
    lines = text.splitlines()
    plots: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        header: dict[str, str] = {}
        while i < n and not re.match(r"^(variables|values|binary):", lines[i], re.I):
            if ":" in lines[i]:
                k, v = lines[i].split(":", 1)
                header[k.strip().lower()] = v.strip()
            i += 1
        if i >= n:
            raise SpiceError("raw file ended inside a header (no Variables/Values section)")
        if lines[i].lower().startswith("binary"):
            raise SpiceError("binary raw files are not supported; use `set filetype=ascii`")
        try:
            nvars = int(header.get("no. variables", "0"))
        except ValueError:
            raise SpiceError(f"bad 'No. Variables' in raw file: {header.get('no. variables')!r}") from None
        names: list[str] = []
        types: dict[str, str] = {}
        if lines[i].lower().startswith("variables"):
            i += 1
            for _ in range(nvars):
                if i >= n:
                    raise SpiceError("raw file ended inside the Variables list")
                parts = lines[i].split()
                if len(parts) < 2:
                    raise SpiceError(f"bad variable line in raw file: {lines[i]!r}")
                name = parts[1].lower()
                names.append(name)
                types[name] = parts[2].lower() if len(parts) > 2 else ""
                i += 1
        while i < n and not lines[i].lower().startswith(("values", "binary")):
            i += 1
        if i < n and lines[i].lower().startswith("binary"):
            raise SpiceError("binary raw files are not supported; use `set filetype=ascii`")
        i += 1  # skip "Values:"
        tokens: list[str] = []
        while i < n and not lines[i].startswith("Title:"):
            tokens.extend(lines[i].split())
            i += 1
        stride = nvars + 1
        npts = len(tokens) // stride if stride > 1 else 0
        cols: list[list[float | complex]] = [[] for _ in names]
        try:
            for p in range(npts):
                row = tokens[p * stride:(p + 1) * stride]
                for j in range(nvars):
                    cols[j].append(_number(row[j + 1]))
        except ValueError as exc:
            raise SpiceError(f"bad number in raw file ({exc})") from None
        complex_flag = "complex" in header.get("flags", "").lower()
        vectors: dict[str, np.ndarray] = {}
        for name, col in zip(names, cols):
            arr = np.asarray(col, dtype=complex if complex_flag else float)
            if complex_flag and types.get(name) in ("frequency", "time"):
                arr = arr.real.copy()
            vectors[name] = arr
        pname = header.get("plotname", "")
        plots.append({"name": pname, "kind": _PLOT_KINDS.get(pname.lower(), pname.lower()),
                      "title": header.get("title", ""), "flags": header.get("flags", ""),
                      "vectors": vectors, "types": types})
    return plots


def _measures_from_log(text: str, names: list[str]) -> dict[str, float]:
    wanted = {n.lower() for n in names}
    out: dict[str, float] = {n: math.nan for n in names}
    for line in text.splitlines():
        for rx in _MEAS_FAIL_RES:
            m = rx.match(line)
            if m and m.group(1).lower() in wanted:
                out[m.group(1).lower()] = math.nan
        m = _MEAS_VAL_RE.match(line)
        if m and m.group(1).lower() in wanted:
            out[m.group(1).lower()] = float(m.group(2))  # last print wins (dedupes repeats)
    return out


def _measure_error_lines(lines: list[str]) -> set[int]:
    """Indices of ``Error:`` lines that belong to a failed ``.meas`` (printed just before it)."""
    out: set[int] = set()
    for idx, line in enumerate(lines):
        if any(rx.match(line) for rx in _MEAS_FAIL_RES):
            for k in (idx - 1, idx - 2):
                if k >= 0 and re.match(r"^\s*error\b", lines[k], re.I):
                    out.add(k)
    return out


def _fatal_reason(text: str) -> tuple[str, list[int]] | None:
    lines = text.splitlines()
    exempt = _measure_error_lines(lines)
    hits: list[int] = []
    reason = ""
    for idx, line in enumerate(lines):
        if idx in exempt:
            continue
        for rx, why in _FATAL:
            if rx.search(line):
                if not hits:
                    reason = why
                hits.append(idx)
                break
    return (reason, hits) if hits else None


def _excerpt(text: str, hits: list[int], max_lines: int = 18) -> str:
    lines = text.splitlines()
    keep: list[int] = []
    for h in hits:
        for k in range(max(0, h - 1), min(len(lines), h + 3)):
            if k not in keep:
                keep.append(k)
        if len(keep) >= max_lines:
            break
    return "\n".join(lines[k] for k in sorted(keep)[:max_lines])


def _prepare(circuit_or_netlist: SpiceCircuit | str) -> tuple[str, list[str], list[str]]:
    if isinstance(circuit_or_netlist, SpiceCircuit):
        c = circuit_or_netlist
        return c.to_netlist(), c.analyses, c.measure_names
    if not isinstance(circuit_or_netlist, str):
        raise SpiceValueError(f"run() takes a SpiceCircuit or netlist text, got {type(circuit_or_netlist).__name__}")
    text = circuit_or_netlist
    if re.search(r"^\s*\.control\b", text, re.I | re.M):
        raise SpiceValueError("netlist already contains a .control block; pass it without one — "
                              "run() adds its own control block")
    analyses, measures = [], []
    for line in text.splitlines()[1:]:
        m = _ANALYSIS_RE.match(line)
        if m:
            analyses.append((m.group(1) + m.group(2)).strip())
        mm = _MEAS_DEF_RE.match(line)
        if mm:
            measures.append(mm.group(1).lower())
    if not analyses:
        raise SpiceValueError("netlist has no analysis line (.op, .dc, .tran or .ac)")
    return text, analyses, measures


def _deck(netlist: str, analyses: list[str]) -> str:
    body = re.split(r"^\s*\.end\s*$", netlist, maxsplit=1, flags=re.I | re.M)[0].rstrip("\n")
    ctl = [".control", "set filetype=ascii", "set appendwrite"]
    for cmd in analyses:
        ctl += [cmd, f"write {_RAW_NAME} all"]
    ctl += ["quit", ".endc", ".end"]
    return body + "\n" + "\n".join(ctl) + "\n"


def run(circuit_or_netlist: SpiceCircuit | str, *, timeout: float = 60.0,
        workdir: str | os.PathLike | None = None) -> SimResult:
    """Simulate a :class:`SpiceCircuit` (or netlist text with dot-analyses) with ngspice.

    ``timeout`` in seconds; ``workdir`` keeps ``circuit.cir``, ``out.raw`` and ``ngspice.log``
    (a temporary directory is used and removed otherwise). Raises :class:`SpiceValueError` for an
    invalid circuit (before starting ngspice), :class:`NgspiceNotFoundError` when ngspice is
    missing and :class:`SpiceError` (with ``.log``) when ngspice fails or times out.
    """
    netlist, analyses, measure_names = _prepare(circuit_or_netlist)
    exe = find_ngspice()
    if exe is None:
        raise NgspiceNotFoundError(_INSTALL_HINT)
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="piforge-spice-") as tmp:
            return _run_in(Path(tmp), exe, netlist, analyses, measure_names, timeout, keep_log=False)
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    return _run_in(wd, exe, netlist, analyses, measure_names, timeout, keep_log=True)


def _run_in(wd: Path, exe: str, netlist: str, analyses: list[str], measure_names: list[str],
            timeout: float, *, keep_log: bool) -> SimResult:
    deck = _deck(netlist, analyses)
    (wd / _DECK_NAME).write_text(deck, encoding="utf-8")
    raw_path = wd / _RAW_NAME
    if raw_path.exists():
        raw_path.unlink()  # appendwrite would otherwise extend a previous run's file
    env = dict(os.environ, LC_ALL="C", LANG="C")  # '.' as decimal separator in all output
    log.debug("ngspice %s in %s (%d analyses)", exe, wd, len(analyses))
    try:
        proc = subprocess.run([exe, "-n", "-b", _DECK_NAME], cwd=str(wd), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", "replace") + (exc.stderr or b"").decode("utf-8", "replace")
        raise SpiceError(f"ngspice timed out after {timeout:g} s (simulation too long or stuck); "
                         f"last output:\n{partial[-1500:]}", log=partial, netlist=deck) from None
    except OSError as exc:
        raise SpiceError(f"could not start ngspice at {exe}: {exc}", netlist=deck) from None
    text = proc.stdout.decode("utf-8", "replace") + "\n" + proc.stderr.decode("utf-8", "replace")
    if keep_log:
        (wd / "ngspice.log").write_text(text, encoding="utf-8")
    fatal = _fatal_reason(text)
    if fatal is not None or proc.returncode != 0:
        reason, hits = fatal if fatal else (f"ngspice exited with code {proc.returncode}", [])
        excerpt = _excerpt(text, hits) if hits else text.strip()[-1500:]
        raise SpiceError(f"ngspice failed: {reason}\n--- log excerpt ---\n{excerpt}", log=text, netlist=deck)
    if not raw_path.is_file():
        raise SpiceError("ngspice produced no output file\n--- log tail ---\n" + text.strip()[-1500:],
                         log=text, netlist=deck)
    plots = parse_ascii_raw(raw_path.read_text(encoding="utf-8", errors="replace"))
    if len(plots) != len(analyses):
        raise SpiceError(f"expected {len(analyses)} analyses in the output, got {len(plots)}",
                         log=text, netlist=deck)
    measures = _measures_from_log(text, measure_names)
    for name, val in measures.items():
        if math.isnan(val):
            log.warning("measure %s failed or was not evaluated (value set to NaN)", name)
    return SimResult(vectors=plots[-1]["vectors"], measures=measures, plots=plots, netlist=deck, log=text)
