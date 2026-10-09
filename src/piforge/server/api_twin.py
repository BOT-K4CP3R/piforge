"""Digital-twin endpoints: device catalogue, scenario runner and the ``/ws/twin`` websocket."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from piforge.core.errors import NotFoundError, PiForgeError
from piforge.core.report import Report, jsonable

log = logging.getLogger(__name__)
router = APIRouter()

SCENARIO_MARGIN_S = 45.0  # runner start-up (≤ 30 s on a loaded host) + settle + stop


def prop_dict(spec: Any) -> dict:
    """A twin ``PropSpec`` as JSON."""
    if dataclasses.is_dataclass(spec) and not isinstance(spec, type):
        d = dataclasses.asdict(spec)
    elif isinstance(spec, dict):
        d = dict(spec)
    else:
        d = {k: getattr(spec, k, None) for k in ("type", "min", "max", "unit", "default", "choices",
                                                  "label")}
    d["choices"] = list(d.get("choices") or ())
    return d


def describe_type(cls: Any) -> dict:
    """Label, display flag and inputs/outputs of a device class (its class-level ``PropSpec`` tables)."""
    kind = getattr(cls, "type", cls.__name__)
    return {"type": kind, "label": getattr(cls, "label", "") or kind,
            "display": bool(getattr(cls, "is_display", False)),
            "inputs": {k: prop_dict(v) for k, v in (getattr(cls, "inputs", {}) or {}).items()},
            "outputs": {k: prop_dict(v) for k, v in (getattr(cls, "outputs", {}) or {}).items()}}


def _device_types() -> tuple[dict[str, dict], str | None]:
    try:
        from piforge.twin.devices import DEVICE_TYPES
    except ImportError as exc:
        return {}, f"The digital twin package is not available: {exc}"
    return {k: describe_type(cls) for k, cls in DEVICE_TYPES.items()}, None


@router.get("/api/twin/devices")
def twin_devices(request: Request) -> JSONResponse:
    """Configured twin devices (with their input/output specs), device types, scenarios."""
    st = request.app.state.piforge
    types, error = _device_types()
    config = st.read_json("twin/config.json", None)
    devices = []
    for d in (config or {}).get("devices") or []:
        if not isinstance(d, dict):
            continue
        t = types.get(str(d.get("type")), {})
        devices.append({**d, "label": d.get("label") or t.get("label", ""), "display": t.get("display", False),
                        "inputs": t.get("inputs", {}), "outputs": t.get("outputs", {})})
    scenarios = st.read_json("twin/scenarios.json", []) or []
    fw = st.firmware_path()
    return JSONResponse(jsonable({
        "available": error is None, "error": error, "config": config, "devices": devices,
        "types": types, "firmware": st.manifest().get("firmware"),
        "firmware_exists": bool(fw and fw.is_file()),
        "scenarios": [{"name": s.get("name"), "duration": s.get("duration"),
                       "steps": len(s.get("steps") or [])} for s in scenarios if isinstance(s, dict)],
        "results": st.read_json("twin/results.json", None),
        "running": st.twin.running,
    }))


def _scenario_from_dict(mod: Any, d: dict) -> Any:
    scenario_cls = mod.Scenario
    if hasattr(scenario_cls, "from_dict"):
        return scenario_cls.from_dict(d)
    step_cls = mod.Step
    fields = {f.name for f in dataclasses.fields(step_cls)}
    steps = [step_cls(**{k: v for k, v in s.items() if k in fields}) for s in d.get("steps") or []]
    return scenario_cls(name=d["name"], steps=steps, duration=float(d.get("duration", 5.0)))


@router.post("/api/twin/scenarios/run")
async def run_scenarios(request: Request, body: dict | None = Body(default=None)) -> JSONResponse:
    """Run the project's scenarios (all, or ``{"names": [...]}``) → merged Report JSON.

    ``{"watch": true}`` streams each run to the ``/ws/twin`` clients (the live twin is stopped first):
    ``scenario`` progress messages plus the scenario session's ``hello``/``state``/``log``/… messages
    tagged with ``"scenario": name`` — the GUI shows the scenario while it runs.
    """
    st = request.app.state.piforge
    try:
        from piforge.twin import scenario as sc
        from piforge.twin.config import TwinConfig
    except ImportError as exc:
        raise HTTPException(503, f"The digital twin package is not available: {exc}") from exc
    scenarios = [s for s in st.read_json("twin/scenarios.json", []) or [] if isinstance(s, dict)]
    names = (body or {}).get("names") or None
    if names:
        known = [s.get("name") for s in scenarios]
        for n in names:
            if n not in known:
                raise HTTPException(404, str(NotFoundError("scenario", n, known)))
        scenarios = [s for s in scenarios if s.get("name") in names]
    if not scenarios:
        raise HTTPException(400, "No scenarios defined (build/twin/scenarios.json is empty).")
    cfg_path, fw = st.file("twin/config.json"), st.firmware_path()
    if not cfg_path.is_file():
        raise HTTPException(400, "No twin configuration (build/twin/config.json); build first.")
    if fw is None or not fw.is_file():
        raise HTTPException(400, f"Firmware not found: {fw}")
    config = TwinConfig.from_json(cfg_path.read_text(encoding="utf-8"))
    watch = bool((body or {}).get("watch"))
    bridge = st.twin
    loop = asyncio.get_running_loop()
    total = len(scenarios)

    def work() -> list[tuple[str, Report]]:
        out = []
        for i, d in enumerate(scenarios):
            name = str(d.get("name"))
            if watch:
                bridge.emit_threadsafe(loop, {"op": "scenario", "phase": "start", "name": name, "index": i,
                                              "total": total, "duration": float(d.get("duration") or 5.0)})
            ctx = bridge.watch(loop, name) if watch else contextlib.nullcontext()
            with ctx:
                rep = sc.run_scenario(config, fw, _scenario_from_dict(sc, d))
            for f in rep.findings:
                f.source = f"twin:{name}"
            out.append((name, rep))
            if watch:
                bridge.emit_threadsafe(loop, {"op": "scenario", "phase": "end", "name": name, "index": i,
                                              "total": total, "ok": rep.ok, "counts": rep.counts()})
        return out

    timeout = sum(float(s.get("duration") or 5.0) + float(s.get("settle") or 0.5) + SCENARIO_MARGIN_S
                  for s in scenarios)
    async with st.scenario_lock:
        if watch:
            await bridge.begin_scenarios()
        results: list[tuple[str, Report]] = []
        error: str | None = None
        try:
            results = await asyncio.wait_for(asyncio.to_thread(work), timeout)
        except TimeoutError as exc:
            error = f"Scenarios timed out after {timeout:.0f} s"
            raise HTTPException(504, error) from exc
        except (PiForgeError, ValueError) as exc:
            error = str(exc)
            raise HTTPException(400, error) from exc
        finally:
            if watch:
                await bridge.end_scenarios({"total": total, "error": error,
                                            "ok": bool(results) and all(r.ok for _, r in results),
                                            "results": [{"name": n, "ok": r.ok} for n, r in results]})
    merged = Report.merge(*(r for _, r in results), title="twin scenarios")
    out = merged.to_dict()
    out["scenarios"] = [{"name": n, "ok": r.ok, "counts": r.counts()} for n, r in results]
    return JSONResponse(jsonable(out))


@router.websocket("/ws/twin")
async def ws_twin(ws: WebSocket) -> None:
    """Live twin session: start/stop/input from the client, runner messages back (spec §5.6)."""
    bridge = ws.app.state.piforge.twin
    await bridge.connect(ws)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is None:
                text = (msg.get("bytes") or b"").decode("utf-8", "replace")
            await bridge.handle_text(ws, text)
    except WebSocketDisconnect:
        pass
    finally:
        await bridge.disconnect(ws)
