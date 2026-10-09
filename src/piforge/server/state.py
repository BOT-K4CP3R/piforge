"""Per-app server state: build-directory readers, rebuild status and event broadcasting.

The build directory layout is the binding contract with :mod:`piforge.build` (``manifest.json``,
``report.json``, ``scene.json`` + ``meshes/``, ``parts/index.json``, ``elec/``, ``sim/``, ``twin/``).
Every reader tolerates missing files so the GUI also works before the first build.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import WebSocket

from piforge.core.errors import NotFoundError
from piforge.server.twin_bridge import TwinBridge

log = logging.getLogger(__name__)

EMPTY_COUNTS = {"error": 0, "warning": 0, "info": 0}
ELEC_FILES = {
    "wiring_svg": "elec/wiring.svg", "wiring_png": "elec/wiring.png", "wiring_md": "elec/wiring.md",
    "wiring_json": "elec/wiring.json", "bom_csv": "elec/bom.csv", "bom_md": "elec/bom.md",
    "netlist": "elec/netlist.net", "config_txt": "elec/config.txt", "pinout_md": "elec/pinout.md",
    "circuit_json": "elec/circuit.json", "power_json": "elec/power.json",
    "cut_list_csv": "elec/cut_list.csv", "cut_list_md": "elec/cut_list.md",
}
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_SEVERITY_ALIASES = {"0": "info", "1": "warning", "2": "error", "warn": "warning", "err": "error"}


def severity_label(value: Any) -> str:
    """``"error"``, ``"warning"`` or ``"info"`` from a label, enum name or IntEnum value."""
    s = str(getattr(value, "name", value)).strip().lower()
    s = _SEVERITY_ALIASES.get(s, s)
    return s if s in _SEVERITY_RANK else "info"


def normalize_finding(f: dict) -> dict:
    """A finding dict with a lower-case ``severity`` and string ``code/message/subject/hint/source``."""
    out = {**f, "severity": severity_label(f.get("severity", "info"))}
    for k in ("code", "message", "subject", "hint", "source"):
        out[k] = "" if out.get(k) is None else str(out[k])
    if not isinstance(out.get("data"), dict):
        out["data"] = {}
    return out


@dataclass
class BuildStatus:
    """State of the (single-flight) background rebuild."""

    state: str = "idle"  # idle | running | done | failed
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        dur = None
        if self.started_at is not None:
            dur = (self.finished_at or time.time()) - self.started_at
        return {"state": self.state, "started_at": self.started_at, "finished_at": self.finished_at,
                "duration_s": dur, "error": self.error, "log": list(self.log[-100:])}


def _currency() -> str:
    try:
        from piforge.fab.profiles import CURRENCY

        return str(CURRENCY)
    except ImportError:
        return ""


def worst_severity(findings: list[dict]) -> str:
    """``"error"``, ``"warning"`` or ``"ok"`` for a list of finding dicts."""
    rank = max((_SEVERITY_RANK[severity_label(f.get("severity", "info"))] for f in findings), default=0)
    return {2: "error", 1: "warning"}.get(rank, "ok")


def parse_csv(text: str, *, lower: bool = False) -> tuple[list[dict], list[str]]:
    """CSV text with a header row → (rows as dicts, column names); ``lower`` normalises the names."""
    if not text.strip():
        return [], []
    reader = csv.DictReader(io.StringIO(text))
    key = (lambda k: k.strip().lower()) if lower else (lambda k: k)
    rows = [{key(k): (v if v is not None else "") for k, v in row.items() if k is not None} for row in reader]
    return rows, [key(c) for c in reader.fieldnames or []]


def _snake(name: str) -> str:
    """CSV header → key: ``"Wire ID"`` → ``"wire_id"``, ``"length (mm)"`` → ``"length_mm"``."""
    out = "".join(c if c.isalnum() else "_" for c in name.strip().lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def wire_nodes(scene: dict) -> list[dict]:
    """``kind: "wire"`` nodes of a scene as flat wire dicts (``id``, ``node``, ``color``, ends…).

    Tolerates partial nodes: a wire without a ``wire`` dict gets its node id as wire id.
    """
    out = []
    for n in scene.get("nodes") or []:
        if not isinstance(n, dict) or n.get("kind") != "wire" or not n.get("id"):
            continue
        w = n.get("wire") if isinstance(n.get("wire"), dict) else {}
        ends = {k: (w.get(k) if isinstance(w.get(k), dict) else {}) for k in ("from", "to")}
        out.append({**w, **ends, "id": str(w.get("id") or n["id"]), "node": n["id"],
                    "color": n.get("color")})
    return out


class AppState:
    """Everything one server instance knows: paths, rebuild status, websocket clients, twin bridge."""

    def __init__(self, project_dir: Path, build_dir: Path) -> None:
        self.project_dir = project_dir
        self.build_dir = build_dir
        self.build = BuildStatus()
        #: replaces ``piforge.build.build_project`` (tests); ``None`` → lazy import at rebuild time
        self.build_fn: Callable[..., Any] | None = None
        self.event_clients: set[WebSocket] = set()
        self.spice_sem = asyncio.Semaphore(2)
        self.scenario_lock = asyncio.Lock()
        self.twin = TwinBridge(self)
        self._tasks: set[asyncio.Task] = set()
        self._overhang_cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------------------- file access
    def file(self, rel: str) -> Path:
        """Absolute path of ``rel`` inside the build directory."""
        return self.build_dir / rel

    def read_json(self, rel: str, default: Any = None) -> Any:
        """Parse a JSON file of the build dir; ``default`` when missing or unreadable."""
        p = self.file(rel)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as exc:
            log.warning("cannot read %s: %s", p, exc)
            return default

    def read_text(self, rel: str, default: str = "") -> str:
        """Text content of a build-dir file; ``default`` when missing."""
        try:
            return self.file(rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return default

    def url(self, rel: str | None) -> str | None:
        """``/build/<rel>`` when the file exists, else ``None`` (no dead links in the GUI)."""
        if not rel or not isinstance(rel, str) or not self.file(rel).is_file():
            return None
        return "/build/" + quote(rel.replace("\\", "/"), safe="/")

    def manifest(self) -> dict:
        m = self.read_json("manifest.json", {})
        return m if isinstance(m, dict) else {}

    def version(self) -> str:
        """Token that changes on every rebuild (used by the GUI to bust mesh caches)."""
        stamps = [p.stat().st_mtime_ns for p in (self.file("manifest.json"), self.file("scene.json"))
                  if p.exists()]
        return str(max(stamps, default=0))

    def firmware_path(self) -> Path | None:
        """Absolute firmware path from the manifest (relative to the project dir), if any."""
        rel = self.manifest().get("firmware")
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else self.project_dir / p

    # ------------------------------------------------------------------------- views
    def report(self) -> dict:
        """``report.json`` (merged build report) with ``counts`` guaranteed."""
        rep = self.read_json("report.json", None)
        if not isinstance(rep, dict):
            rep = {"title": "build", "ok": True, "findings": []}
        findings = [normalize_finding(f) for f in rep.get("findings") or [] if isinstance(f, dict)]
        counts = dict(EMPTY_COUNTS)
        for f in findings:
            counts[f["severity"]] += 1
        return {**rep, "findings": findings, "counts": counts, "ok": counts["error"] == 0}

    def printer_profile(self, name: str | None) -> dict | None:
        if not name:
            return None
        try:
            from piforge.fab.profiles import get_printer

            return asdict(get_printer(name))
        except Exception:  # unknown printer name → the GUI falls back to a generic bed
            return None

    def project_info(self) -> dict:
        m = self.manifest()
        renders = [u for u in (self.url(r) for r in (m.get("files") or {}).get("renders", [])) if u]
        return {
            "name": m.get("name") or self.project_dir.name,
            "description": m.get("description") or "",
            "board": m.get("board"), "printer": m.get("printer"), "material": m.get("material"),
            "firmware": m.get("firmware"), "project_dir": str(self.project_dir),
            "build_dir": str(self.build_dir), "built_at": m.get("built_at"),
            "piforge_version": m.get("piforge_version"), "files": m.get("files") or {},
            "durations_s": m.get("durations_s") or {}, "has_build": bool(m),
            "counts": self.report()["counts"], "printer_profile": self.printer_profile(m.get("printer")),
            "version": self.version(), "build": self.build.to_dict(), "renders": renders,
            "currency": _currency(),
        }

    def scene(self) -> dict:
        s = self.read_json("scene.json", None)
        if not isinstance(s, dict):
            s = {"name": self.project_info()["name"], "units": "mm", "up": "Z", "nodes": [],
                 "bounds": None}
        s.setdefault("nodes", [])
        if not isinstance(s.get("connectors"), list):
            s["connectors"] = []
        return {**s, "version": self.version()}

    def parts(self) -> list[dict]:
        """``parts/index.json`` + download URLs of existing files + printability status."""
        parts = self.read_json("parts/index.json", [])
        if not isinstance(parts, list):
            return []
        findings = self.report()["findings"]
        out = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            files = p.get("files") or {}
            urls = {fmt: u for fmt, rel in files.items() if (u := self.url(rel))}
            own = [normalize_finding(f) for f in
                   ((p.get("analysis") or {}).get("report") or {}).get("findings", []) if isinstance(f, dict)]
            own += [f for f in findings if f.get("source") == f"print:{name}"]
            out.append({**p, "urls": urls, "status": worst_severity(own)})
        return out

    def elec(self) -> dict:
        """Electronics outputs: BOM rows (lower-case column names), wiring rows, pinout, config.txt,
        power budget, ERC and power findings, harness wires (scene.json) + cut list, file URLs."""
        files = {k: self.url(rel) for k, rel in ELEC_FILES.items()}
        rows, cols = parse_csv(self.read_text("elec/bom.csv"), lower=True)
        wiring = self.read_json("elec/wiring.json", [])
        findings = self.report()["findings"]
        cut_rows, cut_cols = parse_csv(self.read_text("elec/cut_list.csv"))
        cut_keys = [_snake(c) for c in cut_cols]
        cut_list = [{_snake(k): v for k, v in r.items()} for r in cut_rows]
        scene = self.read_json("scene.json", {})
        return {
            "available": any(files.values()),
            "bom": rows, "bom_columns": cols,
            "wiring": wiring if isinstance(wiring, list) else [],
            "pinout_md": self.read_text("elec/pinout.md"),
            "config_txt": self.read_text("elec/config.txt"),
            "power": self.read_json("elec/power.json", None),
            "circuit": self.read_json("elec/circuit.json", None),
            "erc": [f for f in findings if f["source"].lower() == "erc"],
            "power_findings": [f for f in findings if f["source"].lower() == "power"],
            "wires": wire_nodes(scene if isinstance(scene, dict) else {}),
            "cut_list": cut_list, "cut_list_columns": cut_keys,
            "files": files,
        }

    def spice_project(self) -> list[dict]:
        """``sim/index.json`` rows with URLs of the stored result JSON and plot PNG."""
        rows = self.read_json("sim/index.json", [])
        if not isinstance(rows, list):
            return []
        return [{**r, "result_url": self.url(f"sim/{r.get('label')}.json"),
                 "plot_url": self.url(f"sim/{r.get('label')}.png")}
                for r in rows if isinstance(r, dict)]

    # ------------------------------------------------------------------------- overhang
    def overhang(self, name: str) -> dict:
        """Per-face overhang mask of a printed part's scene GLB, in its print orientation."""
        from piforge.server.overhang import compute_overhang, rotation_from_part

        parts = self.read_json("parts/index.json", []) or []
        entry = next((p for p in parts if isinstance(p, dict) and p.get("name") == name), None)
        if entry is None:
            raise NotFoundError("part", name, [p.get("name", "") for p in parts if isinstance(p, dict)])
        nodes = (self.read_json("scene.json", {}) or {}).get("nodes") or []
        ids = entry.get("node_ids") or []
        node = (next((n for i in ids for n in nodes if n.get("id") == i and n.get("mesh")), None)
                or next((n for n in nodes if name in (n.get("id"), n.get("name")) and n.get("mesh")),
                        None))
        if node is None:
            raise NotFoundError("scene mesh for part", name, [n.get("id", "") for n in nodes])
        path = self.file(node["mesh"])
        if not path.is_file():
            raise NotFoundError("mesh file", node["mesh"], [])
        rot_matrix, rot_angles = rotation_from_part(entry)
        prof = self.printer_profile(self.manifest().get("printer")) or {}
        max_deg = float(prof.get("max_overhang_deg", 45.0))
        max_bridge = prof.get("max_bridge_mm")
        layer_h = float(prof.get("layer_h", 0.2))
        key = (str(path), path.stat().st_mtime_ns, json.dumps(rot_angles), json.dumps(
            rot_matrix.tolist() if rot_matrix is not None else None), max_deg, max_bridge, layer_h)
        if key not in self._overhang_cache:
            res = compute_overhang(path, rot_matrix, max_deg, max_bridge, layer_h)
            self._overhang_cache.clear()  # keep memory bounded: one cached part at a time is enough
            self._overhang_cache[key] = res
        res = self._overhang_cache[key]
        return {"part": name, "node_id": node.get("id"), "node_ids": ids, "mesh": node["mesh"],
                "rotation": rot_angles, "max_overhang_deg": max_deg, **res}

    # ------------------------------------------------------------------------- events + rebuild
    def spawn(self, coro: Any) -> asyncio.Task:
        """Start a background task and keep a reference until it finishes."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def broadcast_event(self, msg: dict) -> None:
        """Send ``msg`` to every ``/ws/events`` client; drop clients that went away."""
        for ws in list(self.event_clients):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 — a closed socket must not break the others
                self.event_clients.discard(ws)

    def _resolve_build_fn(self) -> Callable[..., Any]:
        if self.build_fn is not None:
            return self.build_fn
        from piforge.build import build_project  # lazy: pulls in the CAD kernel

        return build_project

    async def start_build(self) -> bool:
        """Start a background rebuild unless one is running (single flight). True if started."""
        if self.build.state == "running":
            return False
        status = BuildStatus(state="running", started_at=time.time())
        self.build = status
        self.spawn(self._run_build(status))
        return True

    async def _run_build(self, status: BuildStatus) -> None:
        loop = asyncio.get_running_loop()
        await self.broadcast_event({"op": "build", "state": "started"})

        def progress(message: str) -> None:  # called from the build thread
            status.log.append(str(message))
            loop.call_soon_threadsafe(
                lambda: self.spawn(self.broadcast_event({"op": "build_progress", "message": str(message)})))

        try:
            fn = self._resolve_build_fn()
            await asyncio.to_thread(fn, self.project_dir, out_dir=self.build_dir, progress=progress)
        except Exception as exc:  # noqa: BLE001 — every build failure is reported to the GUI
            log.exception("rebuild failed")
            status.state, status.error = "failed", f"{type(exc).__name__}: {exc}"
        else:
            status.state = "done"
        status.finished_at = time.time()
        self._overhang_cache.clear()
        msg: dict[str, Any] = {"op": "build", "state": status.state}
        if status.error:
            msg["error"] = status.error
        await self.broadcast_event(msg)
