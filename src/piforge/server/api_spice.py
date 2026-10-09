"""SPICE endpoints: list the parametrised benches and run one with user inputs.

``POST /api/spice/run {"key","params"}`` runs :func:`piforge.spice.benches.run_bench` in a worker
thread with a timeout; invalid inputs (``BenchParamError``) and simulation failures (``SpiceError``)
become HTTP 400 with the message, an unknown bench 404, a timeout 504.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from piforge.core.errors import NotFoundError, ValidationError, suggest
from piforge.core.report import jsonable

log = logging.getLogger(__name__)
router = APIRouter()

SPICE_TIMEOUT_S = 120.0


class RunRequest(BaseModel):
    """Body of ``POST /api/spice/run``."""

    key: str
    params: dict[str, Any] = Field(default_factory=dict)


def _load_benches() -> Any:
    from piforge.spice import benches  # lazy: the GUI must work without the SPICE package

    return benches


def _find_ngspice() -> str | None:
    try:
        from piforge.spice.runner import find_ngspice
    except ImportError:
        return None
    try:
        return find_ngspice()
    except Exception:  # noqa: BLE001
        return None


def param_dict(name: str, spec: Any) -> dict:
    """A ``ParamSpec`` as JSON: type (number|choice|bool|text), default, min, max, unit, choices, label, help."""
    if hasattr(spec, "to_dict"):
        d = dict(spec.to_dict())
    else:
        d = {"default": getattr(spec, "default", None), "min": getattr(spec, "min", None),
             "max": getattr(spec, "max", None), "unit": getattr(spec, "unit", "") or "",
             "choices": list(getattr(spec, "choices", ()) or ()), "help": getattr(spec, "help", "")}
    d["label"] = d.get("label") or name
    d.setdefault("type", "bool" if isinstance(d.get("default"), bool) else "choice" if d.get("choices")
                 else "number" if isinstance(d.get("default"), (int, float)) else "text")
    return d


def bench_dict(bench: Any) -> dict:
    """A ``Bench`` as JSON (without its build/evaluate callables)."""
    base = dict(bench.to_dict()) if hasattr(bench, "to_dict") else {}
    params = getattr(bench, "params", {}) or {}
    return {**base, "key": bench.key, "title": getattr(bench, "title", bench.key),
            "description": getattr(bench, "description", ""),
            "params": {k: param_dict(k, s) for k, s in params.items()},
            "units": dict(getattr(bench, "units", {}) or {})}


def result_dict(res: Any, bench: Any, elapsed_s: float) -> dict:
    """A ``BenchResult`` as JSON (traces, units, report, table, netlist text; no raw vectors)."""
    if hasattr(res, "to_dict"):
        d = dict(res.to_dict())
    else:
        report = getattr(res, "report", None)
        d = {"key": res.key, "params": res.params, "measures": res.measures, "analytic": res.analytic,
             "report": report.to_dict() if hasattr(report, "to_dict") else (report or {}),
             "traces": {str(k): list(v) for k, v in (getattr(res, "traces", {}) or {}).items()},
             "x_label": getattr(res, "x_label", ""), "units": dict(getattr(res, "units", {}) or {})}
    d["title"] = d.get("title") or getattr(bench, "title", res.key)
    d["netlist"] = getattr(getattr(res, "sim", None), "netlist", None)
    d["elapsed_s"] = elapsed_s
    return d


@router.get("/api/spice/benches")
def list_benches() -> JSONResponse:
    """Available benches with their parameter specs (empty + error when SPICE is unavailable)."""
    try:
        mod = _load_benches()
    except ImportError as exc:
        return JSONResponse({"available": False, "error": f"SPICE benches unavailable: {exc}",
                             "ngspice": None, "benches": []})
    benches = [bench_dict(b) for b in mod.BENCHES.values()]
    return JSONResponse(jsonable({"available": True, "error": None, "ngspice": _find_ngspice(),
                                  "benches": benches}))


@router.get("/api/spice/project")
def project_results(request: Request) -> JSONResponse:
    """The project's own bench runs from the last build (``sim/index.json``)."""
    return JSONResponse(jsonable(request.app.state.piforge.spice_project()))


@router.post("/api/spice/run")
async def run(req: RunRequest, request: Request) -> JSONResponse:
    """Run one bench with ``params`` (missing ones take their defaults)."""
    st = request.app.state.piforge
    try:
        mod = await asyncio.to_thread(_load_benches)
    except ImportError as exc:
        raise HTTPException(503, f"SPICE benches unavailable: {exc}") from exc
    benches = mod.BENCHES
    if req.key not in benches:
        raise HTTPException(404, str(NotFoundError("SPICE bench", req.key, benches)))
    known = getattr(benches[req.key], "params", None)
    unknown = [k for k in req.params if isinstance(known, dict) and k not in known]
    if unknown:  # checked here: a stray "timeout" would otherwise reach run_bench()'s own keyword
        hint = suggest(unknown[0], list(known))
        raise HTTPException(400, f"unknown parameter {unknown[0]!r} for bench {req.key!r}; "
                                 + (f"did you mean {', '.join(hint)}?" if hint else
                                    f"parameters: {', '.join(known)}"))
    # Only parameter-validation failures are the caller's fault; anything else (TypeError from a
    # bench bug, SpiceError from a simulator crash) is a server error.
    user_errors = tuple(c for c in (ValidationError, getattr(mod, "BenchParamError", None))
                        if isinstance(c, type))
    async with st.spice_sem:
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(asyncio.to_thread(mod.run_bench, req.key, **req.params),
                                         SPICE_TIMEOUT_S)
        except TimeoutError as exc:
            raise HTTPException(504, f"SPICE bench {req.key!r} timed out after "
                                     f"{SPICE_TIMEOUT_S:.0f} s") from exc
        except user_errors as exc:
            log.info("SPICE run %s rejected: %s", req.key, exc)
            raise HTTPException(400, str(exc) or type(exc).__name__) from exc
        except Exception as exc:  # noqa: BLE001 — report as 500, keep the traceback in the log
            log.exception("SPICE run %s failed", req.key)
            raise HTTPException(500, f"SPICE bench {req.key!r} failed: {type(exc).__name__}: {exc}") from exc
        elapsed = time.perf_counter() - t0
    return JSONResponse(jsonable(result_dict(res, benches[req.key], elapsed)))
