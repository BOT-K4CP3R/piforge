"""Optional bridge to an installed slicer CLI: PrusaSlicer, SuperSlicer, OrcaSlicer, Bambu Studio.

:func:`find_slicer` looks at ``$PIFORGE_SLICER`` (an executable), then ``PATH``, then the macOS
application folders in :data:`APP_DIRS`; it returns ``None`` when nothing is installed and
:func:`slice_stl` then raises :class:`SlicerNotFoundError`.

PrusaSlicer/SuperSlicer get the printer geometry, layer/nozzle/extrusion settings, infill,
supports and nominal material temperatures on the command line — good for time/filament numbers;
for production G-code load your vendor profile (``extra_args=["--load", "printer.ini"]``).
OrcaSlicer/Bambu Studio CLIs take settings only from profile files: pass
``extra_args=["--load-settings", "machine.json;process.json", "--load-filaments", "pla.json"]``.
PiForge's printer/material/infill/supports are then *not* applied (``SliceResult.settings_applied``
is False, with a warning), and asking for a non-default ``infill`` or ``supports=True`` without a
process profile (``--load-settings``; infill and supports live there, not in filament files) raises
:class:`PiForgeError` rather than silently slicing something else.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from piforge.core.errors import PiForgeError, ToolNotFoundError, ValidationError
from piforge.fab.profiles import Material, PrinterProfile, get_material, get_printer

logger = logging.getLogger(__name__)

ENV_VAR = "PIFORGE_SLICER"
DEFAULT_INFILL = 0.15
# extra_args that hand OrcaSlicer/Bambu Studio their printer/process/filament profiles
_PROFILE_ARGS = ("--load-settings", "--load-filaments")
# …of which only the machine/process files ("a.json;b.json") carry infill and supports
_PROCESS_ARG = "--load-settings"
#: Folders searched for ``<Slicer>.app`` bundles (monkeypatch in tests).
APP_DIRS: tuple[Path, ...] = (Path("/Applications"), Path.home() / "Applications")

# src: 1.75 mm filament is the standard of every printer in profiles.PRINTERS (vendor spec sheets).
FILAMENT_DIAMETER_MM = 1.75
# src: nominal nozzle / bed °C from manufacturer TDS — Prusament PLA, PETG, ASA, PC Blend, PA11-CF;
#      Polymaker PolyLite ABS; generic TPU 95A. Mid-range values, tune per filament brand.
MATERIAL_TEMPS_C: dict[str, tuple[int, int]] = {
    "PLA": (215, 60),
    "PETG": (240, 85),
    "ABS": (255, 100),
    "ASA": (260, 105),
    "TPU95A": (230, 50),
    "PC": (275, 110),
    "PA-CF": (285, 100),
}


class SlicerNotFoundError(ToolNotFoundError):
    """No supported slicer CLI is installed (or ``$PIFORGE_SLICER`` is not executable)."""


@dataclass
class SlicerInfo:
    """An installed slicer: display name, executable path and version (if it could be read)."""

    name: str
    path: str
    version: str | None


@dataclass
class SliceResult:
    """Output of :func:`slice_stl`. Stats are ``None`` when the G-code does not report them.

    ``settings_applied`` is True when the slicer got PiForge's printer, material, infill and
    supports on its command line (PrusaSlicer/SuperSlicer); False for OrcaSlicer/Bambu Studio,
    whose stats follow their own (default or loaded) profiles.
    """

    gcode_path: Path
    time_s: float | None
    filament_g: float | None
    filament_m: float | None
    log: str
    settings_applied: bool


@dataclass(frozen=True)
class _Kind:
    name: str
    cli: str  # "prusa" (PrusaSlicer-style options) or "orca" (Bambu/Orca --slice CLI)
    commands: tuple[str, ...]
    bundles: tuple[str, ...]  # executables inside an applications folder


_KINDS: tuple[_Kind, ...] = (
    _Kind("PrusaSlicer", "prusa", ("prusa-slicer", "PrusaSlicer", "prusaslicer"),
          ("PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
           "Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer")),
    _Kind("SuperSlicer", "prusa", ("superslicer", "SuperSlicer"),
          ("SuperSlicer.app/Contents/MacOS/SuperSlicer",)),
    _Kind("OrcaSlicer", "orca", ("orca-slicer", "OrcaSlicer", "orcaslicer"),
          ("OrcaSlicer.app/Contents/MacOS/OrcaSlicer",)),
    _Kind("Bambu Studio", "orca", ("bambu-studio", "BambuStudio", "bambustudio"),
          ("BambuStudio.app/Contents/MacOS/BambuStudio",
           "Bambu Studio.app/Contents/MacOS/BambuStudio")),
)


def _kind_for(name: str) -> _Kind:
    """Match a slicer display name or executable file name to its CLI family."""
    low = name.lower().replace(" ", "").replace("-", "")
    for key, kind in (("orca", _KINDS[2]), ("bambu", _KINDS[3]), ("super", _KINDS[1])):
        if key in low:
            return kind
    return _KINDS[0]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _version_from_cli(exe: Path) -> str | None:
    """First dotted version number in the first line of ``<exe> --help``."""
    try:
        res = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                             errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("cannot read version of %s: %s", exe, exc)
        return None
    first = next((ln for ln in (res.stdout + res.stderr).splitlines() if ln.strip()), "")
    match = re.search(r"(\d+(?:\.\d+)+)", first)
    return match.group(1) if match else None


def _version_from_bundle(exe: Path) -> str | None:
    """CFBundleShortVersionString of the ``.app`` that contains ``exe`` (Contents/MacOS/<exe>)."""
    plist = exe.parent.parent / "Info.plist"
    try:
        with plist.open("rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
    return str(version) if version else None


def find_slicer() -> SlicerInfo | None:
    """Locate a slicer CLI: ``$PIFORGE_SLICER``, then PATH, then ``APP_DIRS``; None if absent."""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        exe = Path(override).expanduser()
        if _is_executable(exe):
            return SlicerInfo(_kind_for(exe.name).name, str(exe), _version_from_cli(exe))
        logger.warning("%s=%r is not an executable file; searching PATH and app folders",
                       ENV_VAR, override)
    for kind in _KINDS:
        for command in kind.commands:
            found = shutil.which(command)
            if found:
                return SlicerInfo(kind.name, found, _version_from_cli(Path(found)))
    for app_dir in APP_DIRS:
        for kind in _KINDS:
            for rel in kind.bundles:
                exe = Path(app_dir) / rel
                if _is_executable(exe):
                    version = _version_from_bundle(exe) or _version_from_cli(exe)
                    return SlicerInfo(kind.name, str(exe), version)
    return None


def _prusa_argv(exe: str, stl: Path, gcode: Path, prof: PrinterProfile, mat: Material,
                infill: float, supports: bool, extra_args: Sequence[str]) -> list[str]:
    bx, by, bz = prof.build_volume
    argv = [
        exe, "--export-gcode", "--output", str(gcode),
        "--bed-shape", f"0x0,{bx:g}x0,{bx:g}x{by:g},0x{by:g}",
        "--max-print-height", f"{bz:g}",
        "--center", f"{bx / 2:g},{by / 2:g}",
        "--nozzle-diameter", f"{prof.nozzle_d:g}",
        "--layer-height", f"{prof.layer_h:g}",
        "--extrusion-width", f"{prof.line_w:g}",
        "--fill-density", f"{infill * 100:g}%",
        "--filament-diameter", f"{FILAMENT_DIAMETER_MM:g}",
        "--filament-density", f"{mat.density_g_cm3:g}",
        "--filament-cost", f"{mat.cost_per_kg:g}",
    ]
    temps = MATERIAL_TEMPS_C.get(mat.name)
    if temps:
        nozzle_c, bed_c = temps
        argv += ["--temperature", str(nozzle_c), "--first-layer-temperature", str(nozzle_c),
                 "--bed-temperature", str(bed_c), "--first-layer-bed-temperature", str(bed_c)]
    if supports:
        argv.append("--support-material")
    return [*argv, *extra_args, str(stl)]


def _gcode_stamps(folder: Path) -> dict[Path, int]:
    return {p: p.stat().st_mtime_ns for p in folder.glob("*.gcode")}


def _read_stats_text(path: Path, limit: int = 4 << 20) -> str:
    """Whole file if small; otherwise the head (Orca/Bambu header) and tail (Prusa summary)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size <= limit:
            data = fh.read()
        else:
            head = fh.read(256 << 10)
            fh.seek(size - (1 << 20))
            data = head + b"\n" + fh.read()
    return data.decode("utf-8", errors="replace")


def _tail(text: str, n: int = 1200) -> str:
    text = text.strip()
    return text if len(text) <= n else "…" + text[-n:]


def slice_stl(
    stl_path: str | Path,
    printer: str | PrinterProfile,
    material: str | Material,
    out_dir: str | Path,
    *,
    infill: float = DEFAULT_INFILL,
    supports: bool = False,
    extra_args: Sequence[str] = (),
    timeout_s: float = 900.0,
) -> SliceResult:
    """Slice ``stl_path`` (already in print orientation) to G-code in ``out_dir``.

    Raises :class:`SlicerNotFoundError` when no slicer is installed; ``ValidationError`` for a
    missing STL, bad infill or an ``out_dir`` that is a file; ``PiForgeError`` when the slicer
    fails, writes no new G-code, or is an OrcaSlicer/Bambu Studio CLI asked for a non-default
    ``infill``/``supports`` without a process profile (``--load-settings``, see the module
    docstring).
    """
    stl = Path(stl_path)
    if not stl.is_file():
        raise ValidationError(f"STL file not found: {stl}")
    try:
        fill = float(infill)
    except (TypeError, ValueError):
        fill = float("nan")
    if not 0.0 <= fill <= 1.0:  # also rejects NaN
        raise ValidationError(f"infill must be a fraction in [0, 1], got {infill!r}")
    out = Path(out_dir)
    if out.exists() and not out.is_dir():
        raise ValidationError(f"Output path exists and is not a directory: {out}")
    prof = get_printer(printer)
    mat = get_material(material)
    info = find_slicer()
    if info is None:
        raise SlicerNotFoundError(
            "No slicer CLI found (looked for PrusaSlicer, SuperSlicer, OrcaSlicer and Bambu Studio "
            "on PATH and in /Applications). Install one, e.g. `brew install --cask prusaslicer`, "
            f"or set {ENV_VAR}=/path/to/slicer."
        )
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PiForgeError(f"Cannot create output directory {out}: {exc}") from exc

    target: Path | None
    if _kind_for(info.name).cli == "prusa":
        target = out / f"{stl.stem}.gcode"
        stale = [target]
        settings_applied = True
        argv = _prusa_argv(info.path, stl, target, prof, mat, fill, supports, extra_args)
    else:
        has_profiles = any(str(arg).startswith(_PROFILE_ARGS) for arg in extra_args)
        has_process = any(str(arg).startswith(_PROCESS_ARG) for arg in extra_args)
        if (supports or abs(fill - DEFAULT_INFILL) > 1e-9) and not has_process:
            raise PiForgeError(
                f"The {info.name} CLI cannot apply infill={fill:g} / supports={supports} from the "
                "command line. Pass a process profile that sets them, e.g. extra_args=["
                "'--load-settings', 'machine.json;process.json', '--load-filaments', "
                "'filament.json'], or use PrusaSlicer."
            )
        target = None  # the Orca/Bambu CLI names its output plate_<n>.gcode
        stale = sorted(out.glob("plate_*.gcode"))
        settings_applied = False
        argv = [info.path, "--slice", "0", "--arrange", "1", "--outputdir", str(out),
                *extra_args, str(stl)]
        logger.warning(
            "%s: PiForge printer/material/infill/supports are not applied; G-code and stats follow "
            "%s.", info.name, "the loaded profiles" if has_profiles else "the slicer's defaults")
    for path in stale:  # earlier outputs must never be mistaken for this run's
        path.unlink(missing_ok=True)
    before = _gcode_stamps(out)
    logger.debug("slicing: %s", argv)
    try:
        res = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                             timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise PiForgeError(f"{info.name} did not finish within {timeout_s:g} s") from exc
    except OSError as exc:
        raise PiForgeError(f"Could not run {info.name} at {info.path}: {exc}") from exc
    log = res.stdout + res.stderr
    if res.returncode != 0:
        raise PiForgeError(f"{info.name} failed (exit {res.returncode}): {_tail(log)}")
    written = {p: t for p, t in _gcode_stamps(out).items() if before.get(p) != t}
    if target is not None:
        gcode = target if target in written else None
    else:
        gcode = max(written, key=written.__getitem__) if written else None
    if gcode is None:
        raise PiForgeError(f"{info.name} wrote no G-code to {out}: {_tail(log)}")
    stats = parse_gcode_stats(_read_stats_text(gcode))
    return SliceResult(gcode_path=gcode, time_s=stats["time_s"], filament_g=stats["filament_g"],
                       filament_m=stats["filament_m"], log=log, settings_applied=settings_applied)


# -- G-code statistics ---------------------------------------------------------------------------
_DURATION = r"((?:\d+(?:\.\d+)?\s*[dhms]\s*)+)"
_NUMBERS = r"(\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*)"
_FLAGS = re.MULTILINE | re.IGNORECASE
_RE_NORMAL_TIME = re.compile(r"^;\s*estimated printing time \(normal mode\)\s*[=:]\s*" + _DURATION, _FLAGS)
_RE_TOTAL_TIME = re.compile(r"total estimated time\s*[=:]\s*" + _DURATION, _FLAGS)
_RE_MODEL_TIME = re.compile(r"model printing time\s*[=:]\s*" + _DURATION, _FLAGS)
_RE_TOTAL_G = re.compile(r"^;\s*total filament (?:used|weight) \[g\]\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_G = re.compile(r"^;\s*filament used \[g\]\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_TOTAL_MM = re.compile(r"^;\s*total filament length \[mm\]\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_MM = re.compile(r"^;\s*filament used \[mm\]\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_CM3 = re.compile(r"^;\s*filament used \[cm3\]\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_TOTAL_COST = re.compile(r"^;\s*total filament cost\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_COST = re.compile(r"^;\s*filament cost\s*[=:]\s*" + _NUMBERS, _FLAGS)
_RE_LAYERS = re.compile(r"(?:total layers count|total layer number)\s*[=:]\s*(\d+)", _FLAGS)
_RE_GENERATED = re.compile(r"^;\s*generated by\s+(.+?)(?:\s+on\s+\d{4}-\d{2}-\d{2}.*)?$", _FLAGS)
_RE_BAMBU_GEN = re.compile(r"^;\s*((?:BambuStudio|OrcaSlicer)\s+[\d.]+)\s*$", _FLAGS)


def _duration_s(text: str) -> float:
    scale = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
    return sum(float(v) * scale[u.lower()] for v, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])",
                                                                  text, re.IGNORECASE))


def _last(pattern: re.Pattern[str], text: str) -> str | None:
    matches = pattern.findall(text)
    return matches[-1] if matches else None


def _sum_numbers(pattern: re.Pattern[str], text: str) -> float | None:
    value = _last(pattern, text)
    return None if value is None else sum(float(v) for v in value.split(","))


def parse_gcode_stats(text: str) -> dict:
    """Print statistics from slicer comments (PrusaSlicer/SuperSlicer, OrcaSlicer, Bambu Studio).

    Returns a dict with ``time_s`` (normal-mode or total estimate), ``model_time_s``,
    ``filament_g``, ``filament_m``, ``filament_cm3``, ``cost``, ``layers`` and ``generator``;
    values the G-code does not state are ``None``. Multi-extruder lists are summed.
    """
    text = text.replace("\r\n", "\n")
    normal = _last(_RE_NORMAL_TIME, text)
    total = _last(_RE_TOTAL_TIME, text)
    model = _last(_RE_MODEL_TIME, text)
    time_text = normal or total or model
    grams = _sum_numbers(_RE_TOTAL_G, text)
    if grams is None:
        grams = _sum_numbers(_RE_G, text)
    length_mm = _sum_numbers(_RE_MM, text)
    if length_mm is None:
        length_mm = _sum_numbers(_RE_TOTAL_MM, text)
    cost = _sum_numbers(_RE_TOTAL_COST, text)
    if cost is None:
        cost = _sum_numbers(_RE_COST, text)
    layers = _last(_RE_LAYERS, text)
    generator = _last(_RE_GENERATED, text) or _last(_RE_BAMBU_GEN, text)
    return {
        "time_s": _duration_s(time_text) if time_text else None,
        "model_time_s": _duration_s(model) if model else None,
        "filament_g": grams,
        "filament_m": None if length_mm is None else length_mm / 1000.0,
        "filament_cm3": _sum_numbers(_RE_CM3, text),
        "cost": cost,
        "layers": int(layers) if layers else None,
        "generator": generator.strip() if generator else None,
    }
