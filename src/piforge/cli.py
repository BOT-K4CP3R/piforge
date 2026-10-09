"""``piforge`` command line (typer + rich).

Every heavy subsystem is imported inside its command, so e.g. ``piforge info printers`` starts in a
fraction of a second and never loads the CAD kernel. Exit codes: 0 = OK, 1 = the design has errors
(or the project / an external tool failed), 2 = usage error (bad option, unknown name).
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:  # pragma: no cover
    from piforge.core.report import Finding, Report
    from piforge.project import Project

app = typer.Typer(name="piforge", no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False,
                  help="Design, simulate, validate and export Raspberry Pi devices (electronics + 3D-printed "
                       "parts + firmware).")
twin_app = typer.Typer(no_args_is_help=True, help="Digital twin: run the firmware on a virtual Raspberry Pi.")
app.add_typer(twin_app, name="twin")

TOPICS = ("boards", "modules", "parts", "printers", "materials", "benches", "devices")
_SEV_STYLE = {"error": "bold red", "warning": "yellow", "info": "dim"}


# --------------------------------------------------------------------------------- helpers
def _console(stderr: bool = False) -> Console:
    """Rich console; 120 columns when not a terminal so pipes and logs are not squeezed."""
    tty = (sys.stderr if stderr else sys.stdout).isatty()
    return Console(stderr=stderr, highlight=False, emoji=False, width=None if tty else 120)


def _fail(message: object, code: int = 1) -> NoReturn:
    """Print ``error: message`` to stderr and exit with ``code`` (2 = usage error)."""
    _console(stderr=True).print(Text(f"error: {message}", style="bold red"), soft_wrap=True)
    raise typer.Exit(code)


def _usage(message: object) -> NoReturn:
    _fail(message, 2)


def _load(project: Path) -> Project:
    """Load a project; ProjectLoadError (with the traceback) → exit 1."""
    from piforge.core.errors import PiForgeError
    from piforge.project import load_project

    try:
        return load_project(project)
    except PiForgeError as exc:
        _fail(exc)


def _finding(con: Console, f: Finding) -> None:
    sev = f.severity.label
    line = Text(f"{sev.upper():7} ", style=_SEV_STYLE[sev])
    line.append(f.code, style="bold")
    if f.subject:
        line.append(f" [{f.subject}]")
    if f.source:
        line.append(f"  ({f.source})", style="dim")
    line.append(f"\n        {f.message}")
    if f.hint and f.severity.label != "info":
        line.append(f"\n        fix: {f.hint}", style="cyan")
    con.print(line, soft_wrap=True)


def _report(con: Console, rep: Report, *, infos: bool = True) -> None:
    """Findings, errors first (infos as well unless ``infos=False``)."""
    for f in sorted(rep.findings, key=lambda f: (-int(f.severity), f.source, f.code)):
        if infos or f.severity.label != "info":
            _finding(con, f)


def _summary(con: Console, what: str, rep: Report, where: object = "") -> None:
    c = rep.counts()
    text = Text(f"{what} {'OK' if rep.ok else 'FAILED'}: ", style="bold green" if rep.ok else "bold red")
    text.append(f"{c['error']} error(s), {c['warning']} warning(s), {c['info']} info" + (f" — {where}" if where else ""))
    con.print(text, soft_wrap=True)


def _si(value: float | None, unit: str = "") -> str:
    """``159.2 Hz``, ``3.9 mA``; plain numbers for dB, degrees and dimensionless values."""
    if value is None or not math.isfinite(value):
        return "—"
    if value == 0 or unit in ("", "dB", "deg", "°", "%", "°C") or 1 <= abs(value) < 1000:
        return f"{value:.4g} {unit}".strip()
    for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= factor:
            return f"{value / factor:.4g} {prefix}{unit}"
    return f"{value:.4g} {unit}"


def _hours(h: float) -> str:
    return f"{int(h)} h {round(h % 1 * 60):02d} min" if h >= 1 else f"{max(1, round(h * 60))} min"


# ------------------------------------------------------------------------------------- new
def _template_dir() -> Path:
    env = os.environ.get("PIFORGE_TEMPLATE")
    path = Path(env).expanduser() if env else Path(__file__).resolve().parents[2] / "projects" / "_template"
    if not (path / "project.py").is_file():
        _fail(f"Project template not found at {path} (set PIFORGE_TEMPLATE to a template folder).")
    return path


@app.command()
def new(name: str = typer.Argument(..., help="Project name, also the folder name."),
        dir_: Path = typer.Option(Path("projects"), "--dir", help="Parent folder."),
        board: str = typer.Option("rpi4b", "--board", help="rpi5, rpi4b, rpi3bp or rpizero2w.")) -> None:
    """Create a project from the template: a Pi on a printed plate, button + LED, firmware, scenario."""
    from piforge.core.errors import NotFoundError
    from piforge.mech.boards import get_board

    if not name.strip() or name != name.strip() or re.search(r"[/\\:]", name) or name[0] in "._":
        _usage(f"Invalid project name {name!r}: use letters, digits, spaces, '-' or '_' (no '/', and not "
               "starting with '.' or '_').")
    try:
        key = get_board(board).key
    except NotFoundError as exc:
        _usage(exc)
    dest = dir_.expanduser() / name
    if dest.exists():
        _usage(f"{dest} already exists; choose another name or --dir.")
    template = _template_dir()
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns("build", "__pycache__", "*.pyc", ".DS_Store"))
    for rel in ("project.py", "README.md"):
        f = dest / rel
        text = f.read_text(encoding="utf-8").replace("_template", name)
        if rel == "project.py":
            text = re.sub(r'^BOARD = "[^"]*"', f'BOARD = "{key}"', text, count=1, flags=re.MULTILINE)
        f.write_text(text, encoding="utf-8")
    con = _console()
    con.print(Text(f"Created {dest} (board {key}).", style="bold green"), soft_wrap=True)
    con.print(f"Next: edit project.py and firmware/main.py, then\n  piforge check '{dest}'\n  piforge build '{dest}'"
              f"\n  piforge serve '{dest}'", soft_wrap=True, markup=False)


# ------------------------------------------------------------------------------ build/check
@app.command()
def build(project: Path = typer.Argument(..., help="Project folder (contains project.py)."),
          no_render: bool = typer.Option(False, "--no-render", help="Skip the PNG renders."),
          no_spice: bool = typer.Option(False, "--no-spice", help="Do not simulate the SPICE benches."),
          scenarios: bool = typer.Option(False, "--scenarios", help="Also run the twin scenarios."),
          formats: str = typer.Option("stl,3mf,step", "--formats", help="Part files: comma list of stl, 3mf, step, glb."),
          out: Optional[Path] = typer.Option(None, "--out", help="Output folder (default: <project>/build).")) -> None:
    """Build everything: parts, renders, electronics docs, SPICE, twin config and the report."""
    from piforge.build import FORMATS, build_project
    from piforge.core.errors import PiForgeError

    fmts = [f.strip().lower().lstrip(".") for f in formats.split(",") if f.strip()]
    bad = [f for f in fmts if f not in FORMATS]
    if bad:
        _usage(f"--formats: unknown format(s) {', '.join(bad)}; choose from {', '.join(FORMATS)}.")
    con = _console()
    try:
        res = build_project(project, out_dir=out, render=not no_render, spice=not no_spice, scenarios=scenarios,
                            formats=fmts, progress=lambda m: con.print(Text(f"  · {m}", style="dim"), soft_wrap=True))
    except PiForgeError as exc:
        _fail(exc)
    _report(con, res.report, infos=False)
    table = Table(title=f"{res.project.name}: outputs and timings", title_justify="left")
    table.add_column("files", no_wrap=True)
    table.add_column("seconds per stage")
    table.add_row(", ".join(f"{g} {len(v)}" for g, v in res.manifest["files"].items()),
                  ", ".join(f"{k} {v:.1f}" for k, v in res.durations_s.items()))
    con.print(table)
    _summary(con, "build", res.report, res.out_dir / "report.md")
    raise typer.Exit(0 if res.report.ok else 1)


@app.command()
def check(project: Path = typer.Argument(..., help="Project folder (contains project.py).")) -> None:
    """Fast checks without writing files: ERC, power, printability, assembly, user checks, twin wiring."""
    from piforge.build import check_project
    from piforge.core.errors import PiForgeError

    try:
        rep = check_project(project)
    except PiForgeError as exc:
        _fail(exc)
    con = _console()
    _report(con, rep)
    _summary(con, "check", rep)
    raise typer.Exit(0 if rep.ok else 1)


# ------------------------------------------------------------------------------------ render
def _read_3mf(path: Path) -> Any:
    """Meshes of a 3MF file (core spec: every object mesh, unit-scaled to mm; build transforms ignored)."""
    import xml.etree.ElementTree as ET
    import zipfile

    import numpy as np
    import trimesh

    scale = {"micron": 1e-3, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
    meshes = []
    with zipfile.ZipFile(path) as z:
        for member in (n for n in z.namelist() if n.lower().endswith(".model")):
            root = ET.fromstring(z.read(member))
            s = scale.get(root.get("unit", "millimeter"), 1.0)
            for mesh in (e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "mesh"):
                tags = [(e.tag.rsplit("}", 1)[-1], e) for e in mesh.iter()]
                v = [[float(e.get(k, 0)) for k in "xyz"] for t, e in tags if t == "vertex"]
                f = [[int(e.get(k, 0)) for k in ("v1", "v2", "v3")] for t, e in tags if t == "triangle"]
                if v and f:
                    meshes.append(trimesh.Trimesh(np.asarray(v) * s, np.asarray(f), process=False))
    if not meshes:
        raise ValueError(f"{path.name} contains no mesh")
    return trimesh.util.concatenate(meshes)


def _file_items(path: Path) -> list:
    import trimesh

    from piforge.build import glb_meshes
    from piforge.render import RenderItem

    ext = path.suffix.lower()
    if ext in (".glb", ".gltf"):
        return [RenderItem(m, color=c) for m, c in glb_meshes(path)]
    mesh = _read_3mf(path) if ext == ".3mf" else trimesh.load(str(path), force="mesh")
    return [RenderItem(mesh)]


def _project_items(root: Path) -> tuple[list, str]:
    """The project's assembly (as the build renders it), else its printed parts on the bed in a row."""
    import tempfile

    from piforge.build import scene_items
    from piforge.fab.meshutil import place_on_bed
    from piforge.render import RenderItem

    p = _load(root)
    if p.has_assembly:
        with tempfile.TemporaryDirectory(prefix="piforge-render-") as tmp:
            p.assembly.to_scene(tmp)
            return scene_items(tmp), p.name
    items, x = [], 0.0
    for part in p.printed:
        design, _analysis, _angles, rot = p.print_analysis(part)
        mesh = place_on_bed(design, rot)
        lo, hi = mesh.bounds
        mesh.apply_translation((x - lo[0], 0.0, 0.0))
        x += hi[0] - lo[0] + 10.0
        items.append(RenderItem(mesh, color=part.color))
    if not items:
        _fail(f"{p.name}: nothing to render (no assembly nodes and no printed parts).")
    return items, p.name


@app.command("render")
def render_cmd(path: Path = typer.Argument(..., help="STL, 3MF or GLB file, or a project folder."),
               view: str = typer.Option("iso", "--view", help="iso, front, back, left, right, top or bottom."),
               sheet: bool = typer.Option(False, "--sheet", help="Four views with scale bars and overall size."),
               out: Optional[Path] = typer.Option(None, "--out", help="PNG to write (default: next to the input).")) -> None:
    """Render a mesh file or a project's assembly to PNG (headless, orthographic, Z up)."""
    from piforge.core.errors import NotFoundError
    from piforge.render import VIEWS, render, render_views, save_png

    if view.strip().lower() not in VIEWS:
        _usage(NotFoundError("view", view, VIEWS))
    if not path.exists():
        _usage(f"{path} does not exist.")
    if path.is_dir():
        items, title = _project_items(path)
        default = path / f"render_{'sheet' if sheet else view.lower()}.png"
    elif path.suffix.lower() in (".stl", ".3mf", ".glb", ".gltf", ".obj", ".ply", ".off"):
        try:
            items, title = _file_items(path), path.stem
        except Exception as exc:  # noqa: BLE001 - unreadable/corrupt mesh files
            _fail(f"cannot read {path}: {exc}")
        default = path.with_name(f"{path.stem}_{'sheet' if sheet else view.lower()}.png")
    else:
        _usage(f"{path.name}: unsupported file type; use STL, 3MF, GLB/glTF, OBJ, PLY or a project folder.")
    img = render_views(items, title=title, size=(1600, 1200)) if sheet else render(items, view=view.lower(),
                                                                                    size=(1200, 900))
    target = save_png(img, out or default)
    _console().print(Text(f"wrote {target}", style="green"), soft_wrap=True)


# ------------------------------------------------------------------------------------- print
@app.command("print")
def print_(project: Path = typer.Argument(..., help="Project folder (contains project.py)."),
           part: Optional[str] = typer.Option(None, "--part", help="Only this printed part."),
           slice_: bool = typer.Option(False, "--slice", help="Also slice with PrusaSlicer/OrcaSlicer if installed;"
                                       " G-code goes to <project>/gcode/.")) -> None:
    """Printability, filament, time and cost per printed part (in its print orientation)."""
    from piforge.core.errors import NotFoundError
    from piforge.core.report import Report
    from piforge.fab.profiles import CURRENCY

    p = _load(project)
    parts = p.printed
    if part is not None:
        parts = [q for q in parts if q.name == part]
        if not parts:
            _usage(NotFoundError("printed part", part, [q.name for q in p.printed]))
    con = _console()
    if not parts:
        con.print(f"{p.name}: no printed parts (add them with p.add_printed(...)).")
        raise typer.Exit(0)
    table = Table(title=f"{p.name} — printer {p.printer}", title_justify="left")
    for col in ("part", "material", "qty", "size mm", "mass g", "filament m", "time", f"cost {CURRENCY}",
                "rotation °", "overhang mm²", "status"):
        table.add_column(col, justify="left" if col in ("part", "material", "rotation °", "status") else "right",
                         no_wrap=col in ("part", "size mm"))
    merged = Report("print")
    analyses = []
    for q in parts:
        mesh, a, angles, rot = p.print_analysis(q)
        analyses.append((q, mesh, rot))
        merged.extend(a.report)
        e, sz = a.estimate, a.size_mm
        status = "error" if not a.report.ok else ("supports" if a.needs_supports else "ok")
        table.add_row(q.name, q.material, str(q.quantity), "×".join(f"{v:.0f}" for v in sz), f"{e.mass_g:.1f}",
                      f"{e.filament_m:.2f}", _hours(e.time_h), f"{e.cost:.2f}", "/".join(f"{v:g}" for v in angles),
                      f"{a.overhang_area_mm2:.0f}", status, style="red" if status == "error" else None)
    con.print(table)
    _report(con, merged, infos=False)
    if slice_:
        _slice(con, p, analyses)
    _summary(con, "print check", merged)
    raise typer.Exit(0 if merged.ok else 1)


def _slice(con: Console, p: Project, analyses: list) -> None:
    from piforge.core.errors import PiForgeError
    from piforge.fab.meshutil import place_on_bed
    from piforge.fab.slicer import find_slicer, slice_stl

    info = find_slicer()
    if info is None:
        con.print("No slicer CLI found (PrusaSlicer / OrcaSlicer / Bambu Studio, or $PIFORGE_SLICER): "
                  "the estimates above are PiForge's own.", markup=False, soft_wrap=True)
        return
    out = p.root / "gcode"
    out.mkdir(exist_ok=True)
    for q, mesh, rot in analyses:
        stl = out / f"{q.name}.stl"
        place_on_bed(mesh, rot).export(str(stl))
        try:
            res = slice_stl(stl, p.printer, q.material, out)
        except PiForgeError as exc:
            con.print(Text(f"{q.name}: slicing failed: {exc}", style="red"), soft_wrap=True)
            continue
        time_txt = _hours(res.time_s / 3600) if res.time_s else "?"
        con.print(f"{q.name}: {info.name} → {res.gcode_path} ({time_txt}, "
                  f"{res.filament_g or 0:.1f} g)", markup=False, soft_wrap=True)


# ------------------------------------------------------------------------------------- spice
@app.command()
def spice(bench: str = typer.Argument(..., help="Bench key (see `piforge info benches`)."),
          param: list[str] = typer.Option([], "-p", "--param", help="key=value, repeatable (e.g. -p r=4.7k)."),
          plot: Optional[Path] = typer.Option(None, "--plot", help="Also write a PNG plot.")) -> None:
    """Simulate a SPICE bench with ngspice and compare it with the textbook values."""
    from piforge.core.errors import NotFoundError, PiForgeError
    from piforge.spice import BENCHES, BenchParamError, plot_result, run_bench

    if bench not in BENCHES:
        _usage(NotFoundError("SPICE bench", bench, BENCHES))
    params: dict[str, str] = {}
    for item in param:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            _usage(f"-p {item!r}: expected key=value, e.g. -p r=4.7k")
        params[key.strip()] = value.strip()
    try:
        BENCHES[bench].validate(params)
    except BenchParamError as exc:
        _usage(exc)
    try:
        res = run_bench(bench, **params)
    except PiForgeError as exc:  # ngspice missing (with install hint) or simulator failure
        _fail(exc)
    con = _console()
    con.print(Text(f"{res.title} ({bench}): ", style="bold") + Text(", ".join(
        f"{k}={_si(v, BENCHES[bench].params[k].unit) if isinstance(v, float) else v}" for k, v in res.params.items())),
              soft_wrap=True)
    table = Table(show_header=True)
    for col in ("measure", "simulated", "analytic", "error"):
        table.add_column(col, justify="left" if col == "measure" else "right")
    errors = res.errors_pct()
    for k, v in res.measures.items():
        unit = res.units.get(k, "")
        table.add_row(k, _si(v, unit), _si(res.analytic.get(k), unit) if k in res.analytic else "",
                      f"{errors[k]:+.1f} %" if k in errors else "")
    con.print(table)
    _report(con, res.report)
    if plot is not None:
        con.print(Text(f"wrote {plot_result(res, plot)}", style="green"), soft_wrap=True)
    raise typer.Exit(0 if res.report.ok else 1)


# -------------------------------------------------------------------------------------- twin
def _twin_setup(project: Path) -> tuple[Project, Any]:
    from piforge.core.errors import PiForgeError

    p = _load(project)
    if p.firmware_path is None:
        _fail(f"{p.name} has no firmware: call p.firmware('firmware/main.py') in project.py.")
    try:
        return p, p.twin_config()
    except PiForgeError as exc:
        _fail(f"twin wiring: {exc}")


@twin_app.command("run")
def twin_run(project: Path = typer.Argument(..., help="Project folder (contains project.py)."),
             duration: Optional[float] = typer.Option(None, "--duration", help="Stop after S seconds (default: "
                                                      "until Ctrl+C).")) -> None:
    """Run the firmware on the virtual Pi and stream its log (no inputs; use `piforge serve` for that)."""
    from piforge.twin.session import TwinSession

    p, cfg = _twin_setup(project)
    con = _console()
    con.print(Text(f"{p.name}: {p.firmware_relpath()} on a virtual {cfg.board} with "
                   f"{', '.join(f'{d.id} ({d.type})' for d in cfg.devices) or 'no devices'}", style="bold"),
              soft_wrap=True)
    session = TwinSession(cfg, p.firmware_path, duration=duration)
    session.start()
    deadline = None if duration is None else time.monotonic() + duration + session.start_timeout
    def show_logs() -> None:
        for msg in session.poll():
            if msg.get("op") == "log":
                style = {"stderr": "red", "twin": "yellow"}.get(msg.get("stream", "stdout"))
                con.print(Text(str(msg.get("text", "")).rstrip("\n"), style=style), soft_wrap=True)

    try:
        while session.exit_message is None and (deadline is None or time.monotonic() < deadline):
            show_logs()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
    show_logs()  # lines that arrived while stopping
    state = session.latest_state().get("devices", {})
    if state:
        con.print(Text("final state: " + "; ".join(f"{d}: {s}" for d, s in state.items()), style="dim"),
                  soft_wrap=True)
    ex = session.exit_message or {}
    if (ex.get("error") or ex.get("code") not in (0, None)) and ex.get("reason") not in ("stopped", "duration"):
        _fail(f"firmware stopped ({ex.get('reason')}, code {ex.get('code')}):\n{ex.get('error') or ''}")


@twin_app.command("test")
def twin_test(project: Path = typer.Argument(..., help="Project folder (contains project.py)."),
              scenario: list[str] = typer.Option([], "--scenario", "-s", help="Only these scenarios.")) -> None:
    """Run the project's twin scenarios (timed inputs + expectations): PASS/FAIL per scenario."""
    from piforge.core.errors import NotFoundError
    from piforge.twin.scenario import run_scenario

    p, cfg = _twin_setup(project)
    con = _console()
    chosen = [s for s in p.scenarios if not scenario or s.name in scenario]
    for name in scenario:
        if name not in {s.name for s in p.scenarios}:
            _usage(NotFoundError("scenario", name, [s.name for s in p.scenarios]))
    if not chosen:
        con.print(f"{p.name}: no scenarios (add them with p.scenario(Scenario(...))).")
        raise typer.Exit(0)
    failed = 0
    for sc in chosen:
        t0 = time.perf_counter()
        rep = run_scenario(cfg, p.firmware_path, sc)
        failed += not rep.ok
        label = Text("PASS " if rep.ok else "FAIL ", style="bold green" if rep.ok else "bold red")
        con.print(label + Text(f"{sc.name} ({time.perf_counter() - t0:.1f} s)"), soft_wrap=True)
        _report(con, rep, infos=rep.ok)
    con.print(Text(f"{len(chosen) - failed}/{len(chosen)} scenario(s) passed", style="bold"))
    raise typer.Exit(1 if failed else 0)


# ------------------------------------------------------------------------------------- serve
@app.command()
def serve(project: Path = typer.Argument(..., help="Project folder (contains project.py)."),
          port: int = typer.Option(8765, "--port", help="TCP port on 127.0.0.1."),
          no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab.")) -> None:
    """Open the web GUI: 3D viewer, checks, electronics, SPICE with inputs, interactive digital twin."""
    from piforge.core.errors import PiForgeError

    root = project.expanduser().resolve()
    if not (root / "project.py").is_file():
        _fail(f"No project.py in {root}.")
    try:
        from piforge.server.app import serve as run_server
    except ImportError as exc:
        _fail(f"The web server (piforge.server) is not available: {exc}")
    _console().print(Text(f"PiForge GUI for {root} at http://127.0.0.1:{port}/ — Ctrl+C stops it.", style="bold"),
                     soft_wrap=True)
    try:
        run_server(root, port=port, open_browser=not no_browser)
    except PiForgeError as exc:
        _fail(exc)
    except KeyboardInterrupt:
        pass


# -------------------------------------------------------------------------------------- info
@app.command()
def info(topic: str = typer.Argument(..., help="boards, modules, parts, printers, materials, benches or devices.")) -> None:
    """List what PiForge knows about: boards, parts, printers, materials, SPICE benches, twin devices."""
    from piforge.core.errors import NotFoundError

    key = topic.strip().lower()
    if key not in TOPICS:
        _usage(NotFoundError("info topic", topic, TOPICS))
    columns, rows, note = _info_rows(key)
    table = Table(title=key, title_justify="left")
    for i, name in enumerate(columns):
        table.add_column(name, no_wrap=i == 0)
    for row in rows:
        table.add_row(*(str(v) for v in row))
    con = _console()
    con.print(table)
    if note:
        con.print(note, markup=False, soft_wrap=True)


def _info_rows(key: str) -> tuple[tuple[str, ...], list[tuple], str]:
    """(columns, rows, note) of an ``info`` topic; each topic imports only what it lists."""
    if key == "boards":
        from piforge.mech.boards import BOARDS

        return ("key", "name", "PCB mm", "holes", "ports"), [
            (b.key, b.name, f"{b.length:g} × {b.width:g} × {b.thickness:g}", f"{len(b.holes)} × Ø{b.hole_d:g}",
             ", ".join(b.port_names)) for b in BOARDS.values()], ""
    if key in ("parts", "modules"):
        from piforge.elec import list_defs

        defs = list_defs()
        if key == "parts":
            return ("key", "category", "name", "twin"), [
                (d.key, d.category, d.name, (d.sim or {}).get("twin", "")) for d in defs], ""
        try:  # the mechanical module library (Task 3) may not exist yet
            registry = dict(getattr(__import__("piforge.mech.modules", fromlist=["MODULES"]), "MODULES", {}))
        except ImportError as exc:
            logging.getLogger(__name__).warning("piforge.mech.modules could not be imported: %s", exc)
            registry = {}
        keys = sorted(set(registry) | {d.mech for d in defs if d.mech})
        return ("module", "name", "electronics parts"), [
            (k, getattr(registry.get(k), "name", "") if k in registry else "(no 3D model yet)",
             ", ".join(d.key for d in defs if d.mech == k)) for k in keys], (
            "" if registry else "piforge.mech.modules is not available yet: these are the module keys the "
                                "electronics parts refer to.")
    if key in ("printers", "materials"):
        from piforge.fab.profiles import CURRENCY, MATERIALS, PRINTERS

        if key == "printers":
            return ("key", "vendor", "bed mm", "nozzle", "layer", "overhang ≤", "bridge ≤", "speed mm/s"), [
                (p.name, p.vendor, f"{p.build_x:g} × {p.build_y:g} × {p.build_z:g}", f"{p.nozzle_d:g}",
                 f"{p.layer_h:g}", f"{p.max_overhang_deg:g}°", f"{p.max_bridge_mm:g} mm", f"{p.speed_mm_s:g}")
                for p in PRINTERS.values()], ""
        return ("name", "density g/cm³", "Tg °C", "max service °C", f"{CURRENCY}/kg"), [
            (m.name, f"{m.density_g_cm3:g}", f"{m.glass_transition_c:g}", f"{m.max_service_c:g}",
             f"{m.cost_per_kg:g}") for m in MATERIALS.values()], ""
    if key == "benches":
        from piforge.spice import BENCHES

        return ("key", "title", "parameters (default)"), [
            (b.key, b.title, ", ".join(f"{k}={v.default}{(' ' + v.unit) if v.unit else ''}" for k, v in b.params.items()))
            for b in BENCHES.values()], ""
    from piforge.twin.devices import DEVICE_TYPES

    return ("type", "pins / bus", "inputs", "outputs", "description"), [
        (k, ", ".join(c.pin_roles) or "/".join(c.bus_kinds) or "—", ", ".join(c.inputs) or "—",
         ", ".join(c.outputs) or "—", (c.__doc__ or "").strip().split("\n")[0].replace("``", "")) for k, c in DEVICE_TYPES.items()], ""


# -------------------------------------------------------------------------------------- main
@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Show PiForge log messages.")) -> None:
    """PiForge — design, simulate, validate and export Raspberry Pi devices."""
    if verbose:
        logger = logging.getLogger("piforge")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler())


def main() -> None:
    """Console-script entry point (``piforge``; also ``python -m piforge``)."""
    logging.getLogger("piforge").setLevel(logging.ERROR)  # findings carry the details; -v shows the log
    app()
