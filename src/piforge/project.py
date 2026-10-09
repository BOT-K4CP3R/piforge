"""Project definition: one ``projects/<name>/project.py`` describes a whole device.

``project.py`` defines ``build(p: Project) -> None``; :func:`load_project` imports it and calls it::

    from piforge.project import Project

    def build(p: Project) -> None:
        p.meta(description="Desk gauge", board="rpi4b", printer="prusa_mk4", material="PETG")
        pi = p.circuit.add("rpi4b", "U1")             # electronics (piforge.elec.model.Circuit)
        ...
        p.add_printed(base, lid)                      # printed PartSpecs → parts/ + printability
        p.assembly.add(base); p.assembly.add(lid, (0, 0, 30))   # placed parts → scene.json
        p.firmware("firmware/main.py")                # runs unmodified in the digital twin
        p.scenario(Scenario(...)); p.spice("led_driver", r_series=330)

:func:`piforge.build.build_project` turns the project into a build directory. The CAD kernel is only
imported when the project creates geometry or touches :attr:`Project.assembly`, so projects (and
checks) that are electronics-only stay fast.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import math
import re
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piforge.analysis.thermal import ThermalInputs
from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.core.report import Report
from piforge.elec.model import Circuit
from piforge.fab.profiles import get_material, get_printer
from piforge.mech.part import PartSpec
from piforge.twin.config import BOARDS as TWIN_BOARDS
from piforge.twin.config import DeviceConfig, TwinConfig
from piforge.twin.scenario import Scenario

if TYPE_CHECKING:  # pragma: no cover
    from piforge.mech.assembly import Assembly

log = logging.getLogger(__name__)

__all__ = ["Project", "ProjectLoadError", "euler_xyz", "load_project", "project_root", "safe_name"]

_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)
_INTERFERENCE_KINDS = ("printed", "reference", "pcb")  # fasteners sit in holes on purpose


class ProjectLoadError(PiForgeError):
    """``project.py`` is missing, does not import, or its ``build(p)`` raised.

    The message starts with a one-line summary followed by the traceback text; ``path`` is the
    ``project.py`` path and ``traceback_text`` the traceback alone.
    """

    def __init__(self, message: str, *, path: Path | None = None, traceback_text: str = "") -> None:
        super().__init__(message)
        self.path = path
        self.traceback_text = traceback_text


def safe_name(name: str, default: str = "item") -> str:
    """File-system friendly form of ``name`` (letters incl. non-ASCII, digits, ``._-``).

    Same rule as :func:`piforge.mech.export.export_part` uses for part file names.
    """
    s = _UNSAFE.sub("_", str(name).strip()).strip("._")
    return s or default


def euler_xyz(rotation: Any) -> tuple[float, float, float]:
    """Angles (rx, ry, rz) in degrees with ``rotation_matrix(rx, ry, rz) == rotation`` (fixed X→Y→Z)."""
    try:
        r = [[float(v) for v in row] for row in rotation]
    except (TypeError, ValueError):
        r = []
    if len(r) != 3 or any(len(row) != 3 for row in r):
        raise ValidationError(f"euler_xyz needs a 3x3 rotation matrix, got {rotation!r}")
    cy = math.hypot(r[0][0], r[1][0])
    if cy > 1e-9:
        rx, ry, rz = math.atan2(r[2][1], r[2][2]), math.atan2(-r[2][0], cy), math.atan2(r[1][0], r[0][0])
    else:  # gimbal lock (ry = ±90°): rz folds into rx
        ry, rz = math.copysign(math.pi / 2, -r[2][0]), 0.0
        rx = math.atan2(r[0][1], r[1][1]) if -r[2][0] > 0 else math.atan2(-r[0][1], r[1][1])
    return tuple(_clean_angle(math.degrees(a)) for a in (rx, ry, rz))  # type: ignore[return-value]


def _clean_angle(angle: float) -> float:
    """Round away float noise (9 decimals), no -0.0, and -180° written as 180°."""
    a = round(float(angle), 9) + 0.0
    return 180.0 if a == -180.0 else a


def _check_name(fn: Callable[..., Any]) -> str:
    name = getattr(fn, "__name__", "") or ""
    return name if name.isidentifier() else ""


class Project:
    """Everything that describes one device: metadata, circuit, parts, assembly, firmware, tests.

    Lengths in mm. ``printer``/``material`` name a :mod:`piforge.fab.profiles` printer and the
    project's default filament (used for new parts in templates and listed in the manifest);
    each printed :class:`~piforge.mech.part.PartSpec` is analysed in its own material.
    """

    def __init__(self, name: str, *, root: Path, printer: str = "generic", material: str = "PETG") -> None:
        if not str(name).strip():
            raise ValidationError("Project name must not be empty")
        self.name: str = str(name)
        self.root: Path = Path(root).expanduser().resolve()
        self.description: str = ""
        self.board: str | None = None
        self.printer: str = get_printer(printer).name
        self.material: str = get_material(material).name
        self.circuit: Circuit = Circuit(self.name)
        self.printed: list[PartSpec] = []
        self.checks: list[Callable[[], Report]] = []
        self.benches: list[tuple[str, str, dict]] = []
        self.scenarios: list[Scenario] = []
        self.thermal_cases: list[tuple[str | None, ThermalInputs]] = []
        self.firmware_path: Path | None = None
        self.twin_overrides: dict[str, dict] = {}
        self.twin_devices: list[DeviceConfig] = []
        self.assembly_options: dict[str, Any] = {"enabled": True, "ignore": [], "kinds": _INTERFERENCE_KINDS,
                                                 "min_volume": 0.5, "sweep_joints": True}
        self._check_names: list[str] = []
        self._assembly: Assembly | None = None
        self.harnesses: list[Any] = []  # piforge.mech.harness.Harness objects (p.harness)

    def __repr__(self) -> str:
        return (f"Project({self.name!r}, root={str(self.root)!r}, parts={len(self.circuit.parts)}, "
                f"printed={len(self.printed)}, benches={len(self.benches)}, scenarios={len(self.scenarios)})")

    # -- metadata -----------------------------------------------------------------------------
    def meta(self, *, name: str | None = None, description: str | None = None, board: str | None = None,
             printer: str | None = None, material: str | None = None) -> None:
        """Set project metadata; board/printer/material names are validated (close matches suggested)."""
        if name is not None:
            if not str(name).strip():
                raise ValidationError("Project name must not be empty")
            self.name = str(name)
            self.circuit.name = self.name
            if self._assembly is not None:
                self._assembly.name = self.name
        if description is not None:
            self.description = str(description)
        if board is not None:
            from piforge.mech.boards import get_board  # board table only, no CAD kernel

            self.board = get_board(board).key
        if printer is not None:
            self.printer = get_printer(printer).name
        if material is not None:
            self.material = get_material(material).name

    # -- mechanics ------------------------------------------------------------------------------
    @property
    def assembly(self) -> Assembly:
        """The placed parts (``piforge.mech.assembly.Assembly``); created — with the CAD kernel — on first use."""
        if self._assembly is None:
            from piforge.mech.assembly import Assembly

            self._assembly = Assembly(self.name)
        return self._assembly

    @property
    def has_assembly(self) -> bool:
        """True when the assembly holds at least one node (never imports the CAD kernel)."""
        return self._assembly is not None and len(self._assembly) > 0

    def add_printed(self, *parts: PartSpec) -> None:
        """Register printed parts (exported to ``parts/`` and analysed for printability).

        Names must be unique, also as file names on a case-insensitive file system
        (:class:`~piforge.mech.assembly.DuplicateIdError`); place the parts in :attr:`assembly`
        separately to show them in the 3D scene.
        """
        for part in parts:
            if not isinstance(part, PartSpec):
                raise ValidationError(f"add_printed expects PartSpec objects, got {type(part).__name__}")
            if part.kind != "printed":
                raise ValidationError(f"add_printed expects printed parts; {part.name!r} is a {part.kind!r} "
                                      "part — add it to p.assembly instead")
        taken = {safe_name(p.name).lower(): p.name for p in self.printed}
        for part in parts:
            key = safe_name(part.name).lower()
            if key in taken:
                from piforge.mech.assembly import DuplicateIdError

                raise DuplicateIdError(f"printed part name {part.name!r} clashes with {taken[key]!r} "
                                       "(part names must be unique, also as file names ignoring case)")
            taken[key] = part.name
        self.printed.extend(parts)

    def print_analysis(self, part: PartSpec) -> tuple[Any, Any, tuple[float, float, float], Any]:
        """Tessellate a printed part, choose its print orientation (``print_rotation``, else automatic on
        :attr:`printer`) and analyse it in its own material with :func:`piforge.fab.analyze_mesh`.

        Returns (design-frame mesh, PrintAnalysis, (rx, ry, rz) degrees, 3×3 rotation); the angles
        reproduce the rotation exactly, so the exported files and the GUI agree with the analysis.
        """
        import numpy as np

        from piforge.fab.analyze import analyze_mesh
        from piforge.fab.meshutil import rotation_matrix
        from piforge.mech.export import to_trimesh

        mesh = to_trimesh(part.shape)
        if part.print_rotation is not None:
            angles, rot = tuple(_clean_angle(a) for a in part.print_rotation), rotation_matrix(*part.print_rotation)
        else:
            from piforge.fab.orient import best_orientation

            rot = np.asarray(best_orientation(mesh, self.printer)[0].rotation, dtype=float)
            angles = euler_xyz(rot)
        analysis = analyze_mesh(mesh, self.printer, part.material, name=part.name, rotation=rot)
        return mesh, analysis, angles, rot  # type: ignore[return-value]

    def assembly_check(self, *, enabled: bool = True, ignore: list | tuple = (), kinds: tuple | list | None = None,
                       min_volume: float = 0.5, sweep_joints: bool = True) -> None:
        """Configure the build's assembly checks (interference between parts, joint sweeps).

        ``ignore``: node-id pairs allowed to overlap; ``kinds``: part kinds to check (default
        printed, reference and pcb — fasteners deliberately sit in holes); ``min_volume`` in mm³.
        """
        if min_volume < 0:
            raise ValidationError("min_volume must be >= 0 mm³")
        self.assembly_options = {"enabled": bool(enabled), "ignore": [tuple(p) for p in ignore],
                                 "kinds": tuple(kinds) if kinds is not None else _INTERFERENCE_KINDS,
                                 "min_volume": float(min_volume), "sweep_joints": bool(sweep_joints)}

    def harness(self, harness: Any, *, add: bool = True) -> Any:
        """Register a routed :class:`~piforge.mech.harness.Harness` (from ``route_harness``).

        ``add`` puts its wires into :attr:`assembly` (``kind="wire"`` nodes + ``connectors`` in
        scene.json); the build writes ``elec/cut_list.csv|md`` and adds ``harness.checks()`` with
        source ``harness`` (``WIRE.*``). Returns the harness.
        """
        from piforge.mech.harness import Harness

        if not isinstance(harness, Harness):
            raise ValidationError(f"p.harness expects a Harness (route_harness(...)), got {type(harness).__name__}")
        if add:
            harness.add_to(self.assembly)
        if harness not in self.harnesses:
            self.harnesses.append(harness)
        return harness

    # -- checks, firmware, simulation -----------------------------------------------------------
    def add_check(self, fn: Callable[[], Report], *, name: str | None = None) -> None:
        """Register a design check returning a :class:`~piforge.core.report.Report` (source ``check:<name>``).

        ``name`` defaults to the function name (e.g. ``p.add_check(enclosure.checks)`` → ``checks``),
        or the check's position for lambdas.
        """
        if not callable(fn):
            raise ValidationError(f"add_check expects a callable returning a Report, got {type(fn).__name__}")
        base = safe_name(name) if name else (_check_name(fn) or str(len(self.checks) + 1))
        label, n = base, 2
        while label in self._check_names:
            label, n = f"{base}_{n}", n + 1
        self.checks.append(fn)
        self._check_names.append(label)

    def named_checks(self) -> list[tuple[str, Callable[[], Report]]]:
        """``[(name, check function)]`` in registration order."""
        return list(zip(self._check_names, self.checks, strict=True))

    def firmware(self, path: str) -> None:
        """Set the firmware entry point, relative to the project root; it must exist (NotFoundError)."""
        rel = Path(path).expanduser()
        full = (rel if rel.is_absolute() else self.root / rel).resolve()
        if not full.is_file():
            candidates = sorted({q.relative_to(self.root).as_posix()
                                 for pattern in ("*.py", "*/*.py", "*/*/*.py") for q in self.root.glob(pattern)
                                 if not {"build", "__pycache__"} & set(q.relative_to(self.root).parts)})
            raise NotFoundError("firmware file", str(path), candidates)
        self.firmware_path = full

    def scenario(self, scenario: Scenario) -> None:
        """Add a twin scenario (:class:`~piforge.twin.scenario.Scenario`); names must be unique."""
        if not isinstance(scenario, Scenario):
            raise ValidationError(f"scenario() expects a piforge.twin.scenario.Scenario, got {type(scenario).__name__}")
        if any(s.name == scenario.name for s in self.scenarios):
            raise ValidationError(f"scenario name {scenario.name!r} is already used in this project")
        self.scenarios.append(scenario)

    def spice(self, bench_key: str, *, label: str | None = None, **params: Any) -> None:
        """Add a SPICE bench run; ``params`` are validated now (BenchParamError, NotFoundError).

        ``label`` names the outputs ``sim/<label>.json|png`` (default: the bench key, suffixed
        ``_2``, ``_3``… when repeated); an explicit duplicate label raises ValidationError.
        """
        from piforge.spice.benches import get_bench

        bench = get_bench(bench_key)
        bench.validate(params)
        taken = {b[0].lower() for b in self.benches}
        if label is None:
            base = safe_name(bench.key)
            lab, n = base, 2
            while lab.lower() in taken:
                lab, n = f"{base}_{n}", n + 1
        else:
            lab = safe_name(label, default="bench")
            if lab.lower() in taken:
                raise ValidationError(f"SPICE label {lab!r} is already used; pass another label=")
        self.benches.append((lab, bench.key, dict(params)))

    def thermal(self, inputs: ThermalInputs, *, label: str | None = None) -> None:
        """Add an enclosure thermal case (may be called several times, e.g. ``label="fan on"``).

        One unlabelled case (the original API): ``thermal.json`` holds its result and findings have
        source ``thermal``. Otherwise ``thermal.json`` is a dict keyed by label and findings have
        source ``thermal:<label>`` (unlabelled cases are called ``case<n>``).
        """
        if not isinstance(inputs, ThermalInputs):
            raise ValidationError(f"thermal() expects piforge.analysis.thermal.ThermalInputs, got {type(inputs).__name__}")
        if label is not None:
            label = str(label).strip()
            if not label:
                raise ValidationError("thermal(label=...) must not be empty")
            if any(lab == label for lab, _ in self.thermal_cases):
                raise ValidationError(f"thermal case {label!r} already exists; pass another label=")
        self.thermal_cases.append((label, inputs))

    @property
    def thermal_inputs(self) -> ThermalInputs | None:
        """Inputs of the first thermal case (``None`` without :meth:`thermal`)."""
        return self.thermal_cases[0][1] if self.thermal_cases else None

    def thermal_labelled(self) -> list[tuple[str, ThermalInputs]]:
        """Thermal cases with labels filled in; ``[]`` or a single ``("", inputs)`` for the legacy
        single unlabelled case."""
        if len(self.thermal_cases) == 1 and self.thermal_cases[0][0] is None:
            return [("", self.thermal_cases[0][1])]
        used = {lab for lab, _ in self.thermal_cases if lab}
        out = []
        for i, (lab, inp) in enumerate(self.thermal_cases, 1):
            if not lab:
                lab, k = f"case{i}", i
                while lab in used:
                    k += 1
                    lab = f"case{k}"
                used.add(lab)
            out.append((lab, inp))
        return out

    # -- digital twin ---------------------------------------------------------------------------
    def twin_override(self, device_id: str, **params: Any) -> None:
        """Adjust a device derived from the circuit: ``pins=``, ``bus=``, ``type=`` replace those
        fields, ``params=`` and any other keyword update the device params (e.g. ``color="green"``)."""
        if not str(device_id).strip():
            raise ValidationError("twin_override needs a device id (the part reference, e.g. 'D1')")
        self.twin_overrides.setdefault(str(device_id), {}).update(params)

    def twin_device(self, cfg: DeviceConfig) -> None:
        """Add a twin device that cannot be derived from the circuit."""
        if isinstance(cfg, dict):
            cfg = DeviceConfig.from_dict(cfg)
        if not isinstance(cfg, DeviceConfig):
            raise ValidationError(f"twin_device expects a DeviceConfig, got {type(cfg).__name__}")
        self.twin_devices.append(cfg)

    def twin_config(self) -> TwinConfig:
        """Twin wiring: auto-derived from the circuit, then overrides, then extra devices.

        Without a Raspberry Pi in the circuit only the extra devices are used (board = project board).
        """
        if self.circuit.board is not None:
            from piforge.twin.from_circuit import twin_config_from_circuit

            base = twin_config_from_circuit(self.circuit)
        else:
            base = TwinConfig(board=self.board if self.board in TWIN_BOARDS else "rpi4b")
        devices = list(base.devices)
        for dev_id, override in self.twin_overrides.items():
            dev = base.device(dev_id)  # NotFoundError lists the derived device ids
            ov = dict(override)
            new = DeviceConfig(id=dev.id, type=ov.pop("type", dev.type), pins=ov.pop("pins", dev.pins),
                               bus=ov.pop("bus", dev.bus), params={**dev.params, **ov.pop("params", {}), **ov})
            devices[devices.index(dev)] = new
        return TwinConfig(board=base.board, devices=devices + list(self.twin_devices), pulls=base.pulls)

    # -- validation -----------------------------------------------------------------------------
    def lint(self, config: TwinConfig | None = None) -> Report:
        """Cheap consistency checks (report title ``project``, nothing is run).

        ``PROJECT.FIRMWARE_SYNTAX`` (ERROR): the firmware does not compile. With a twin ``config``
        (see :meth:`twin_config`) also ``PROJECT.SCENARIO_INVALID``: a scenario step names a device
        that is not wired (ERROR), an input it does not have (ERROR) or an unknown property (WARNING).
        """
        rep = Report("project")
        fw, rel = self.firmware_path, self.firmware_relpath()
        if fw is not None:
            try:
                compile(fw.read_bytes(), str(fw), "exec", dont_inherit=True)
            except SyntaxError as exc:
                rep.add("PROJECT.FIRMWARE_SYNTAX", "error", f"Firmware {rel} has a syntax error at line "
                        f"{exc.lineno}: {exc.msg}.", f"file:{rel}", "Fix it: the firmware would not start on "
                        "the Pi or in the digital twin.", line=exc.lineno, offset=exc.offset)
            except (OSError, ValueError) as exc:
                rep.add("PROJECT.FIRMWARE_SYNTAX", "error", f"Firmware {rel} cannot be read: {exc}.", f"file:{rel}")
        if config is None or not self.scenarios:
            return rep
        from piforge.twin.devices import DEVICE_TYPES

        devices = {d.id: d for d in config.devices}
        for sc in self.scenarios:
            for st in sc.steps:
                if st.action == "expect_log":
                    continue
                where, dev = f"Scenario {sc.name!r}, {st.action} at {st.at:g} s", devices.get(st.device)
                if dev is None:
                    rep.add("PROJECT.SCENARIO_INVALID", "error", f"{where}: unknown twin device {st.device!r} "
                            f"(devices: {', '.join(devices) or 'none'}).", f"scenario:{sc.name}",
                            "Twin device ids are the circuit's part references (e.g. SW1, D1).")
                    continue
                cls = DEVICE_TYPES.get(dev.type)
                props = set(getattr(cls, "inputs", {})) | (set() if st.action == "input" else
                                                            set(getattr(cls, "outputs", {})))
                if cls is not None and st.prop not in props:
                    what = "input" if st.action == "input" else "property"
                    rep.add("PROJECT.SCENARIO_INVALID", "error" if st.action == "input" else "warning",
                            f"{where}: {st.device} ({dev.type}) has no {what} {st.prop!r} "
                            f"(known: {', '.join(sorted(props)) or 'none'}).", f"scenario:{sc.name}")
        return rep

    # -- convenience ----------------------------------------------------------------------------
    @property
    def board_key(self) -> str | None:
        """``board`` if set, else the circuit's Raspberry Pi definition key (``None`` without one)."""
        if self.board:
            return self.board
        pi = self.circuit.board
        return pi.key if pi is not None else None

    def firmware_relpath(self) -> str | None:
        """Firmware path relative to the project root (POSIX), or None."""
        if self.firmware_path is None:
            return None
        try:
            return self.firmware_path.relative_to(self.root).as_posix()
        except ValueError:
            import os

            return Path(os.path.relpath(self.firmware_path, self.root)).as_posix()


# ------------------------------------------------------------------------------------- loading
def project_root(project_dir: Path | str) -> Path:
    """Resolved project directory for a directory or a ``project.py`` path."""
    path = Path(project_dir).expanduser()
    if path.name == "project.py" and path.is_file():
        path = path.parent
    return path.resolve()


def _inside(file: object, root: Path) -> bool:
    if not isinstance(file, str):
        return False
    try:
        return Path(file).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _purge_modules(root: Path, keep: str = "") -> None:
    """Forget modules imported from ``root`` (sibling helpers) so the next load sees fresh code.

    PiForge itself and installed packages are never purged, even when a project folder happens
    to contain them (re-importing piforge would break ``isinstance`` checks).
    """
    for name, mod in list(sys.modules.items()):
        if name == keep or mod is None or name == "piforge" or name.startswith("piforge."):
            continue
        file = getattr(mod, "__file__", None)
        if _inside(file, root) and "site-packages" not in Path(str(file)).parts:
            del sys.modules[name]


def _load_error(file: Path, exc: BaseException) -> ProjectLoadError:
    parts = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tb = "".join(p for p in parts if "<frozen importlib" not in p)
    summary = f"Cannot load {file}: {type(exc).__name__}: {exc}"
    return ProjectLoadError(f"{summary}\n{tb}", path=file, traceback_text=tb)


def load_project(project_dir: Path | str) -> Project:
    """Import ``<project_dir>/project.py`` (unique module name) and run its ``build(p)``.

    Modules next to ``project.py`` are importable while it loads and are forgotten afterwards, so
    two projects may both have a ``parts.py`` and edits are picked up on the next load. Every
    failure raises :class:`ProjectLoadError` with the traceback text (``__cause__`` is the original
    exception).
    """
    root = project_root(project_dir)
    file = root / "project.py"
    if not file.is_file():
        raise ProjectLoadError(f"No project.py in {root} — create a project with `piforge new NAME` "
                               "(project.py must define build(p)).", path=file)
    module_name = "piforge_project_" + hashlib.sha1(str(file).encode("utf-8")).hexdigest()[:12]
    project = Project(root.name, root=root)
    old_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # no __pycache__ in project folders, no stale bytecode on edits
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    _purge_modules(root)
    try:
        spec = importlib.util.spec_from_file_location(module_name, file)
        if spec is None or spec.loader is None:  # pragma: no cover - a .py file always has a loader
            raise ProjectLoadError(f"Cannot import {file}", path=file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            build = getattr(module, "build", None)
            if not callable(build):
                raise ProjectLoadError(f"{file} must define a function build(p: Project) that describes "
                                       "the device (see projects/_template/project.py).", path=file)
            build(project)
        except ProjectLoadError:
            raise
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - any failure in user code
            raise _load_error(file, exc) from exc
    finally:
        sys.dont_write_bytecode = old_dont_write
        try:
            sys.path.remove(str(root))
        except ValueError:  # pragma: no cover - removed by the project itself
            pass
        _purge_modules(root, keep=module_name)
    log.info("loaded project %s from %s", project.name, file)
    return project
