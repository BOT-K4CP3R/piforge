"""Build pipeline: turn ``projects/<name>/project.py`` into a build directory (spec §6.8).

:func:`build_project` writes ``<project>/build/`` — the layout is the binding contract with the GUI:
``manifest.json report.json report.md scene.json meshes/ parts/ renders/ elec/ sim/ twin/ thermal.json``.

Stages run in the order load → elec → parts → scene → checks → render → spice → twin. Only a broken
``project.py`` aborts (:class:`~piforge.project.ProjectLoadError`); every other failure becomes an
ERROR finding with ``source="project"`` and the build goes on. Outputs are written to a staging
folder next to the build directory and swapped in at the end, so a reader (the GUI) never sees a
half-written build. :func:`check_project` runs the same validation without writing anything.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

import piforge
from piforge.core.errors import PiForgeError, ValidationError
from piforge.core.report import Finding, Report, Severity, jsonable
from piforge.elec.pinout import pinout_markdown
from piforge.project import Project, euler_xyz, load_project, project_root, safe_name
from piforge.render.scene import glb_meshes, scene_items  # re-exported for compatibility

log = logging.getLogger(__name__)

__all__ = ["FORMATS", "STAGES", "BuildResult", "build_project", "check_project", "euler_xyz", "glb_meshes",
           "pinout_markdown", "scene_items"]

STAGES = ("load", "elec", "parts", "scene", "checks", "render", "spice", "twin")
FORMATS = ("stl", "3mf", "step", "glb")
GROUPS = ("parts", "renders", "elec", "sim", "twin", "scene", "report")
_OVERHANG_RGB = (229, 72, 77)  # faces that need support are painted red in part renders


@dataclass
class BuildResult:
    """Outcome of :func:`build_project`: the loaded project, where it was written, the merged report."""

    project: Project
    out_dir: Path
    report: Report
    manifest: dict
    durations_s: dict[str, float]


# ------------------------------------------------------------------------------- build context
class _Ctx:
    """State shared by the stages of one build or check: report, written files, timings."""

    def __init__(self, project: Project, out: Path | None, progress: Callable[[str], None] | None, *,
                 title: str = "build", render: bool = False, spice: bool = False, scenarios: bool = False,
                 formats: tuple[str, ...] = ()) -> None:
        self.p = project
        self.out = out  # staging directory; None = check mode, nothing is written
        self.progress = progress
        self.render, self.spice, self.scenarios, self.formats = render, spice, scenarios, formats
        self.report = Report(title)
        self.files: dict[str, list[str]] = {g: [] for g in GROUPS}
        self.durations: dict[str, float] = {s: 0.0 for s in STAGES}
        self.part_meshes: list[tuple[Any, Any, Any]] = []  # (PartSpec, placed mesh, PrintAnalysis)
        self.has_scene = False
        self.built_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def say(self, message: str) -> None:
        """Report progress."""
        log.info("%s: %s", self.p.name, message)
        _notify(self.progress, message)

    def add(self, source: str, findings: Report | Iterable[Finding]) -> None:
        """Merge findings into the build report under ``source`` (``erc``, ``print:lid``, …)."""
        for f in findings.findings if isinstance(findings, Report) else findings:
            f.source = source
            self.report.findings.append(f)

    def note(self, code: str, severity: str, message: str, *, subject: str = "", hint: str = "", **data: Any) -> None:
        """Add a project-level finding (``source="project"``)."""
        self.report.findings.append(Finding(code, Severity.parse(severity), message, subject=subject,
                                            data=jsonable(data), hint=hint, source="project"))

    def failed(self, what: str, exc: BaseException, code: str = "PROJECT.STAGE_FAILED") -> None:
        """Turn an exception of a subsystem into an ERROR finding (with the traceback in ``data``)."""
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-12:])
        log.warning("%s failed: %s", what, exc, exc_info=log.isEnabledFor(logging.DEBUG))
        self.note(code, "error", f"{what} failed: {type(exc).__name__}: {exc}",
                  hint="See data.traceback. Fix project.py (or report a PiForge bug); the rest of the "
                       "build went on.", exception=type(exc).__name__, traceback=tb)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a stage; an escaping exception becomes a PROJECT.STAGE_FAILED finding."""
        t0 = time.perf_counter()
        try:
            with self.attempt(f"{name} stage"):
                yield
        finally:
            self.durations[name] = round(self.durations.get(name, 0.0) + time.perf_counter() - t0, 3)

    @contextmanager
    def attempt(self, what: str, code: str = "PROJECT.STAGE_FAILED") -> Iterator[None]:
        """Run one output/check; an exception becomes an ERROR finding and the stage goes on."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001
            self.failed(what, exc, code)

    # -- files (``group`` = manifest ``files`` key) ------------------------------------------
    def path(self, rel: str) -> Path:
        """Absolute path of ``rel`` in the staging directory (parent folders created)."""
        (p := self.out / rel).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[operator]
        return p

    def wrote(self, group: str, rel: str) -> None:
        if rel not in self.files[group]:
            self.files[group].append(rel)

    def write_text(self, group: str, rel: str, text: str) -> None:
        self.path(rel).write_text(text, encoding="utf-8")
        self.wrote(group, rel)

    def write_json(self, group: str, rel: str, data: Any) -> None:
        self.write_text(group, rel, json.dumps(jsonable(data), indent=1, ensure_ascii=False) + "\n")

    def write_png(self, rel: str, image: Any) -> None:
        from piforge.render import save_png

        save_png(image, self.path(rel))
        self.wrote("renders", rel)


# ----------------------------------------------------------------------------------- public API
def build_project(project_dir: Path | str, *, out_dir: Path | str | None = None, render: bool = True,
                  spice: bool = True, scenarios: bool = False,
                  formats: Iterable[str] | str = ("stl", "3mf", "step"),
                  progress: Callable[[str], None] | None = None) -> BuildResult:
    """Load ``project_dir/project.py`` and write the build directory (default ``<project>/build``).

    ``render``: PNG renders; ``spice``: simulate the project's SPICE benches; ``scenarios``: run the
    twin scenarios (spawns the firmware); ``formats``: part files (stl, 3mf, step, glb). An existing
    ``out_dir`` is replaced only when it is empty or a previous build (has ``manifest.json``).
    Raises ProjectLoadError for a broken project, ValidationError for bad options.
    """
    fmts = _formats(formats)
    t0 = time.perf_counter()
    _notify(progress, f"load: {project_root(project_dir)}")
    project = load_project(project_dir)
    load_s = time.perf_counter() - t0
    out = (Path(out_dir).expanduser() if out_dir is not None else project.root / "build").resolve()
    _check_out_dir(out, project.root)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-staging-", dir=out.parent))
    try:
        os.chmod(staging, 0o755)
        ctx = _Ctx(project, staging, progress, render=render, spice=spice, scenarios=scenarios, formats=fmts)
        ctx.durations["load"] = round(load_s, 3)
        for name, fn in _STAGE_FUNCS:
            with ctx.stage(name):
                fn(ctx)
        ctx.durations["total"] = round(time.perf_counter() - t0, 3)
        manifest = _finish(ctx, {"render": render, "spice": spice, "scenarios": scenarios, "formats": list(fmts)})
        _share(staging)
        _swap(staging, out)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    c = ctx.report.counts()
    ctx.say(f"done in {ctx.durations['total']:.1f} s: {c['error']} error(s), {c['warning']} warning(s) → {out}")
    return BuildResult(project, out, ctx.report, manifest, dict(ctx.durations))


def check_project(project_dir: Path | str) -> Report:
    """Validate a project without writing files: ERC, power, printability (in print orientation),
    assembly interference/joint sweeps, user checks, thermal, twin wiring, firmware syntax and
    scenario references. No exports, renders, SPICE runs or twin runs. Report title ``check``."""
    ctx = _Ctx(load_project(project_dir), None, None, title="check")
    for name, fn in _STAGE_FUNCS:
        if name not in ("render", "spice"):
            with ctx.stage(name):
                fn(ctx)
    return ctx.report


# ------------------------------------------------------------------------------------- stages
def _elec(ctx: _Ctx) -> None:
    from piforge.elec import (
        boot_config, bom, bom_csv, bom_markdown, kicad_netlist, power_budget, run_erc, wiring_diagram,
        wiring_markdown, wiring_table,
    )

    c = ctx.p.circuit
    extra = ", BOM, wiring, pinout, config.txt, netlist" if ctx.out is not None else ""
    ctx.say(f"elec: ERC, power budget{extra} ({len(c.parts)} parts, {len(c.nets)} nets)")
    with ctx.attempt("ERC"):
        erc = run_erc(c)
        if c.parts and not erc.findings:  # say so: an empty ERC section could also mean "not run"
            from piforge.elec import RULES

            erc.add("ERC.OK", "info", f"ERC passed: {len(RULES)} rules on {len(c.parts)} parts and {len(c.nets)} "
                    "nets found no problem.", rules=len(RULES), parts=len(c.parts), nets=len(c.nets))
        ctx.add("erc", erc)
    budget = None
    with ctx.attempt("power budget"):
        budget = power_budget(c)
        ctx.add("power", budget.report)
    if ctx.out is None:
        return
    with ctx.attempt("circuit.json"):
        ctx.write_json("elec", "elec/circuit.json", c.to_dict())
    with ctx.attempt("BOM"):
        lines = bom(c)
        ctx.write_text("elec", "elec/bom.csv", bom_csv(lines))
        ctx.write_text("elec", "elec/bom.md", bom_markdown(lines))
    rows: list = []
    with ctx.attempt("wiring table"):
        rows = wiring_table(c)
        ctx.write_json("elec", "elec/wiring.json", [asdict(r) for r in rows])
        ctx.write_text("elec", "elec/wiring.md", wiring_markdown(rows))
    if c.parts:
        with ctx.attempt("wiring diagram"):
            wiring_diagram(c, ctx.path("elec/wiring.svg"), ctx.path("elec/wiring.png"))
            ctx.wrote("elec", "elec/wiring.svg")
            ctx.wrote("elec", "elec/wiring.png")
    with ctx.attempt("config.txt"):
        ctx.write_text("elec", "elec/config.txt", boot_config(c))
    with ctx.attempt("KiCad netlist"):
        ctx.write_text("elec", "elec/netlist.net", kicad_netlist(c, date=ctx.built_at))
    if budget is not None:
        with ctx.attempt("power budget file"):
            rails = {k: {**asdict(r), "utilization": r.utilization} for k, r in budget.rails.items()}
            ctx.write_json("elec", "elec/power.json", {"psu": budget.psu, "rails": rails,
                                                       "report": budget.report.to_dict()})
    with ctx.attempt("pinout"):
        ctx.write_text("elec", "elec/pinout.md", pinout_markdown(c, rows))


def _parts(ctx: _Ctx) -> None:
    p = ctx.p
    index: list[dict] = []
    if p.printed:
        from piforge.fab.meshutil import place_on_bed
        from piforge.mech.export import export_part

        nodes = p.assembly.nodes if p.has_assembly else []
        for i, part in enumerate(p.printed, 1):
            ctx.say(f"parts: {part.name} ({i}/{len(p.printed)}): "
                    f"{'export ' + '/'.join(ctx.formats) + ', ' if ctx.out is not None and ctx.formats else ''}"
                    f"printability on {p.printer}")
            with ctx.attempt(f"printed part {part.name!r}", code="PROJECT.PART_FAILED"):
                mesh, analysis, angles, rot = p.print_analysis(part)
                ctx.add(f"print:{part.name}", analysis.report)
                if ctx.render and ctx.out is not None:
                    ctx.part_meshes.append((part, place_on_bed(mesh, rot), analysis))
                files: dict[str, str] = {}
                if ctx.out is not None and ctx.formats:
                    written = export_part(replace(part, print_rotation=angles), ctx.out / "parts", ctx.formats)
                    files = {fmt: f"parts/{path.name}" for fmt, path in written.items()}
                    for rel in files.values():
                        ctx.wrote("parts", rel)
                index.append({
                    "name": part.name, "material": part.material, "color": part.color, "quantity": part.quantity,
                    "files": files, "print_rotation": list(angles), "analysis": analysis.to_dict(),
                    "node_ids": [n.id for n in nodes if n.part is part or (
                        n.part.kind == "printed" and n.part.name == part.name)]})
    if ctx.out is not None:
        ctx.write_json("parts", "parts/index.json", index)


def _scene(ctx: _Ctx) -> None:
    p = ctx.p
    nodes = p.assembly.nodes if p.has_assembly else []
    if nodes:
        asm = p.assembly
        ctx.say(f"scene: {len(nodes)} node(s){' → scene.json + meshes' if ctx.out is not None else ''}, "
                "interference check")
        if ctx.out is not None:
            with ctx.attempt("3D scene export (scene.json + meshes)"):
                asm.to_scene(ctx.out)
                ctx.wrote("scene", "scene.json")
                for glb in sorted((ctx.out / "meshes").glob("*.glb")):
                    ctx.wrote("scene", f"meshes/{glb.name}")
                ctx.has_scene = True
        opts = p.assembly_options
        if opts["enabled"]:
            with ctx.attempt("assembly interference check"):
                ctx.add("assembly", asm.check_interference(ignore=opts["ignore"], min_volume=opts["min_volume"],
                                                           kinds=opts["kinds"]))
            for n in nodes if opts["sweep_joints"] else ():
                if n.joint is not None:
                    with ctx.attempt(f"joint sweep of {n.id!r}"):
                        ctx.add("assembly", asm.sweep_joint(n.id, ignore=opts["ignore"],
                                                            min_volume=opts["min_volume"]))
        registered = {id(q) for q in p.printed} | {q.name for q in p.printed}
        for n in nodes:
            if n.part.kind == "printed" and id(n.part) not in registered and n.part.name not in registered:
                ctx.note("PROJECT.PART_NOT_EXPORTED", "warning",
                         f"Assembly node {n.id!r} is a printed part ({n.part.name!r}) that is not registered "
                         "with p.add_printed(): it is shown in the scene but not exported or checked for "
                         "printability.", subject=f"node:{n.id}",
                         hint="Pass the PartSpec to p.add_printed(...) (or give it kind='reference').")
    placed = {id(n.part) for n in nodes} | {n.part.name for n in nodes}
    for q in p.printed:
        if id(q) not in placed and q.name not in placed:
            ctx.note("PROJECT.PART_NOT_PLACED", "info",
                     f"Printed part {q.name!r} is not placed in p.assembly: it is exported and checked but not "
                     "shown in the 3D scene or the assembly renders.", subject=f"part:{q.name}",
                     hint="p.assembly.add(part, (x, y, z)) places it.")
    if ctx.out is not None and not ctx.has_scene:
        ctx.write_json("scene", "scene.json", {"name": p.name, "units": "mm", "up": "Z", "nodes": [],
                                               "bounds": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]})


def _checks(ctx: _Ctx) -> None:
    p = ctx.p
    for name, fn in p.named_checks():
        ctx.say(f"checks: {name}")
        with ctx.attempt(f"check {name!r}", code="PROJECT.CHECK_FAILED"):
            rep = fn()
            if isinstance(rep, Report):
                ctx.add(f"check:{name}", rep)
            else:
                ctx.note("PROJECT.CHECK_INVALID", "error",
                         f"Check {name!r} returned {type(rep).__name__}, not a piforge Report.",
                         subject=f"check:{name}", hint="A check returns piforge.core.report.Report, e.g. "
                         "r = Report('mine'); r.add('MY.CODE', 'error', 'message'); return r.")
    for k, h in enumerate(getattr(p, "harnesses", ())):
        ctx.say(f"checks: harness {h.name} ({len(h.wires)} wires, collision test)")
        with ctx.attempt(f"harness {h.name!r}"):
            ctx.add("harness", h.checks())
            if ctx.out is not None:
                stem = "elec/cut_list" if k == 0 else f"elec/cut_list_{safe_name(h.name)}"
                ctx.write_text("elec", f"{stem}.csv", h.cut_list_csv())
                ctx.write_text("elec", f"{stem}.md", h.cut_list_markdown())
    cases = p.thermal_labelled()
    if cases:
        ctx.say("checks: enclosure thermal model" + (f" ({len(cases)} cases)" if len(cases) > 1 else ""))
        # one unlabelled case: thermal.json = its result (source "thermal"); else {label: result}
        results: dict[str, Any] = {}
        for label, inputs in cases:
            with ctx.attempt(f"thermal model {label!r}" if label else "thermal model"):
                from piforge.analysis.thermal import PI_THERMAL, enclosure_temperature

                board = p.board_key if p.board_key in PI_THERMAL else None
                res = enclosure_temperature(inputs, board=board)
                ctx.add(f"thermal:{label}" if label else "thermal", res.report)
                results[label] = res.to_dict()
        if ctx.out is not None and results:
            ctx.write_json("report", "thermal.json", results[""] if "" in results else results)


def _render(ctx: _Ctx) -> None:
    if ctx.out is None or not ctx.render:
        return
    from piforge.render import render, render_views

    if ctx.has_scene:
        ctx.say("render: assembly iso view + 4-view sheet")
        with ctx.attempt("assembly render"):
            items = scene_items(ctx.out)
            if items:
                ctx.write_png("renders/assembly_iso.png", render(items, view="iso", size=(1200, 900)))
                ctx.write_png("renders/assembly_sheet.png", render_views(items, title=ctx.p.name, size=(1600, 1200)))
    for part, mesh, analysis in ctx.part_meshes:
        ctx.say(f"render: part {part.name} (print orientation)")
        with ctx.attempt(f"render of part {part.name!r}"):
            ctx.write_png(f"renders/part_{safe_name(part.name, 'part')}.png",
                          render(_part_item(part, mesh, analysis), view="iso", size=(800, 600)))
    ctx.part_meshes.clear()


def _spice(ctx: _Ctx) -> None:
    p = ctx.p
    if ctx.out is None:
        return
    rows: list[dict] = []
    try:
        if not p.benches:
            return
        if not ctx.spice:
            ctx.note("PROJECT.SPICE_SKIPPED", "info", f"{len(p.benches)} SPICE bench(es) not simulated "
                     "(SPICE disabled for this build).", hint="Build without --no-spice to simulate them.")
            return
        from piforge.spice import plot_result, run_bench, runner

        if runner.find_ngspice() is None:
            ctx.note("PROJECT.NGSPICE_MISSING", "info", f"ngspice is not installed: {len(p.benches)} SPICE "
                     "bench(es) skipped.", hint="Install it (macOS: brew install ngspice) and rebuild.")
            return
        for label, key, params in p.benches:
            ctx.say(f"spice: {label} ({key})")
            row: dict[str, Any] = {"label": label, "bench": key, "params": params, "measures": {},
                                   "analytic": {}, "ok": False}
            try:
                res = run_bench(key, **params)
                ctx.add(f"spice:{label}", res.report)
                row.update(params=res.params, measures=res.measures, analytic=res.analytic, ok=res.report.ok,
                           title=res.title, units=res.units)
                ctx.write_json("sim", f"sim/{label}.json", res.to_dict())
                plot_result(res, ctx.path(f"sim/{label}.png"))
                ctx.wrote("sim", f"sim/{label}.png")
            except Exception as exc:  # noqa: BLE001 - SpiceError, missing ngspice, plotting
                ctx.failed(f"SPICE bench {label!r} ({key})", exc, code="PROJECT.SPICE_FAILED")
                row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
    finally:
        ctx.write_json("sim", "sim/index.json", rows)


def _twin(ctx: _Ctx) -> None:
    p = ctx.p
    run = bool(ctx.scenarios and ctx.out is not None and p.scenarios)
    ctx.say(f"twin: wiring from the circuit{f', {len(p.scenarios)} scenario run(s)' if run else ''}")
    cfg = None
    with ctx.attempt("digital twin configuration"):
        cfg = p.twin_config()
        if ctx.out is not None:
            ctx.write_text("twin", "twin/config.json", cfg.to_json())
    if ctx.out is not None:
        ctx.write_json("twin", "twin/scenarios.json", [s.to_dict() for s in p.scenarios])
    ctx.add("project", p.lint(None if run else cfg))  # scenario runs validate their steps themselves
    if not p.scenarios or cfg is None:
        return
    if not run:
        if ctx.out is not None:
            ctx.note("PROJECT.SCENARIOS_NOT_RUN", "info", f"{len(p.scenarios)} twin scenario(s) defined but not "
                     "run in this build.", hint="Run them with `piforge build --scenarios` or `piforge twin test`.")
        return
    if p.firmware_path is None:
        ctx.note("PROJECT.NO_FIRMWARE", "error", "Twin scenarios need firmware, but project.py sets none.",
                 hint="Call p.firmware('firmware/main.py') in build(p).")
        return
    from piforge.twin.scenario import run_scenario

    results: list[tuple[str, Report, float]] = []
    for sc in p.scenarios:
        ctx.say(f"twin: scenario {sc.name} ({sc.duration:g} s)")
        t0 = time.perf_counter()
        with ctx.attempt(f"twin scenario {sc.name!r}"):
            rep = run_scenario(cfg, p.firmware_path, sc)
            ctx.add(f"twin:{sc.name}", rep)
            results.append((sc.name, rep, time.perf_counter() - t0))
    data = Report.merge(*(r for _, r, _ in results), title="twin scenarios").to_dict()
    data["scenarios"] = [{"name": n, "ok": r.ok, "counts": r.counts(), "duration_s": round(d, 2)}
                         for n, r, d in results]
    ctx.write_json("twin", "twin/results.json", data)


_STAGE_FUNCS: tuple[tuple[str, Callable[[_Ctx], None]], ...] = (
    ("elec", _elec), ("parts", _parts), ("scene", _scene), ("checks", _checks), ("render", _render),
    ("spice", _spice), ("twin", _twin))


# ------------------------------------------------------------------------------------ helpers
def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    """Call the progress callback; a failing callback never breaks the build."""
    if progress is not None:
        try:
            progress(message)
        except Exception:  # noqa: BLE001
            log.warning("progress callback failed", exc_info=True)


def _part_item(part: Any, mesh: Any, analysis: Any) -> Any:
    """Render item of a part on the bed; faces that need support are painted red."""
    from piforge.render import RenderItem

    mask = np.asarray(analysis.overhang_face_mask, dtype=bool)
    if mask.shape == (len(mesh.faces),) and mask.any():
        colors = np.tile(np.array([int(part.color[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.uint8),
                         (len(mesh.faces), 1))
        colors[mask] = _OVERHANG_RGB
        return RenderItem(mesh, face_colors=colors)
    return RenderItem(mesh, color=part.color)


def _formats(formats: Iterable[str] | str) -> tuple[str, ...]:
    items = formats.split(",") if isinstance(formats, str) else list(formats)
    fmts = tuple(dict.fromkeys(str(f).strip().lower().lstrip(".") for f in items if str(f).strip()))
    bad = [f for f in fmts if f not in FORMATS]
    if bad:
        raise ValidationError(f"Unknown part format(s) {', '.join(bad)}; choose from {', '.join(FORMATS)}.")
    return fmts


def _check_out_dir(out: Path, root: Path) -> None:
    """Refuse to replace anything that is not an empty folder or a previous PiForge build."""
    if out == root or root.is_relative_to(out):
        raise PiForgeError(f"Refusing to build into {out}: it contains the project itself. Use a separate "
                           f"folder such as {root / 'build'}.")
    if out.exists() and not out.is_dir():
        raise PiForgeError(f"Build output {out} exists and is not a directory.")
    if out.is_dir() and not (out / "manifest.json").is_file() and any(
            q.name != ".DS_Store" for q in out.iterdir()):  # Finder litter does not count
        raise PiForgeError(f"Refusing to replace {out}: it is not empty and has no manifest.json, so it is not a "
                           "PiForge build directory. Choose another output folder or empty it first.")


def _swap(staging: Path, out: Path) -> None:
    """Replace ``out`` with ``staging`` (rename the old build away first, delete it afterwards)."""
    old = None
    if out.exists():
        old = out.with_name(f".{out.name}-old-{os.getpid()}-{time.time_ns()}")
        os.replace(out, old)
    try:
        os.replace(staging, out)
    except OSError:
        if old is not None:
            os.replace(old, out)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def _share(folder: Path) -> None:
    """mkstemp-based writers leave files 0600; build outputs are ordinary readable files."""
    for f in folder.rglob("*"):
        if f.is_file():
            f.chmod(0o644)


def _finish(ctx: _Ctx, options: dict) -> dict:
    """Write report.json, report.md and manifest.json; return the manifest (as written)."""
    p, rep = ctx.p, ctx.report
    ctx.write_json("report", "report.json", rep.to_dict())
    ctx.write_text("report", "report.md", rep.to_markdown())
    assert ctx.out is not None
    files = {g: [rel for rel in rels if (ctx.out / rel).is_file()] for g, rels in ctx.files.items()}
    manifest = jsonable({
        "name": p.name, "description": p.description, "board": p.board_key, "printer": p.printer,
        "material": p.material, "firmware": p.firmware_relpath(), "project_dir": str(p.root),
        "built_at": ctx.built_at, "piforge_version": piforge.__version__, "ok": rep.ok, "counts": rep.counts(),
        "options": options, "files": files, "durations_s": dict(ctx.durations)})
    (ctx.out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    return manifest
