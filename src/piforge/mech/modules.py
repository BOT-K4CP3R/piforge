"""Mechanical models of common modules (displays, sensors, buttons, LEDs, motors, fans, regulators…).

Frame convention and panel-mount semantics are documented in :mod:`piforge.mech._module_data`
(binding): PCB centred on the origin, PCB bottom at z = 0, panel/front side +Z. Data access is
kernel-free; :meth:`ModuleModel.shape` and :meth:`ModuleModel.panel_cutout` import build123d lazily.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from piforge.core.errors import NotFoundError, ValidationError
from piforge.mech._module_data import MODULE_DATA
from piforge.mech.anchors import PinAnchor, anchor_from_record
from piforge.mech.part import PartSpec

if TYPE_CHECKING:  # pragma: no cover
    from build123d import Compound, Part

PANEL_MOUNTS = ("behind", "through", "surface")
EPS = 0.01  # mm, same as piforge.mech.primitives.EPS

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Window = tuple[str, Vec2, tuple[float, ...]]

_PCB_COLOR = "#1d6b3a"
_COLOR_RULES = (  # (substring of the component name, colour)
    ("glass", "#1b2a3a"), ("bezel", "#2b2b2b"), ("lens", "#202020"), ("dome", "#f2f2f2"),
    ("fresnel", "#f2f2f2"), ("header", "#202020"), ("pins", "#c9a227"), ("leads", "#c9ccd1"),
    ("transducer", "#c9ccd1"), ("relay", "#2a63c8"), ("heatsink", "#2b2b2b"), ("term", "#1f7a3a"),
    ("cap", "#3a3a3a"), ("led", "#f5f5f5"), ("frame", "#2b2b2b"), ("shaft", "#c9ccd1"),
    ("bushing", "#c9ccd1"), ("thread", "#c9ccd1"), ("boss", "#c9ccd1"), ("body", "#3a3a3a"),
    ("tabs", "#3a3a3a"), ("gear_top", "#3a3a3a"), ("spline", "#f2f2f2"), ("receptacle", "#c9ccd1"),
)


def _component_color(name: str) -> str:
    for key, col in _COLOR_RULES:
        if key in name:
            return col
    return "#3a3a3a"


@dataclass(frozen=True)
class ModuleModel:
    """Mechanical model of a bought module; see :mod:`piforge.mech._module_data` for the frame.

    ``components`` are ``(name, "box"|"cyl", center, size)`` with ``size`` = (sx, sy, sz) for boxes
    and (d, h) for vertical cylinders. ``window`` (+ ``extra_windows``) are panel openings without
    clearance. ``front_height`` is the plane (z) that seats against the panel for ``behind``
    mounts, the head height above the outer panel face for ``through`` mounts and the overall
    height for ``surface`` modules.
    """

    key: str
    name: str
    pcb: Vec3 | None
    holes: tuple[Vec2, ...]
    hole_d: float
    components: tuple[tuple[str, str, tuple, tuple], ...]
    window: Window | None
    panel_mount: str
    front_height: float
    elec_key: str | None
    source: str
    extra_windows: tuple[Window, ...] = ()
    pcb_round: bool = False
    pcb_hole_d: float = 0.0
    pcb_color: str = _PCB_COLOR
    anchors: tuple[PinAnchor, ...] = field(default=(), repr=False)  # wire contacts, module frame
    aliases: tuple[str, ...] = field(default=(), repr=False)

    # -- data (kernel-free) ---------------------------------------------------------------------
    @property
    def windows(self) -> tuple[Window, ...]:
        """All panel openings (``window`` first)."""
        return ((self.window,) if self.window is not None else ()) + tuple(self.extra_windows)

    def component(self, name: str) -> tuple[str, str, tuple, tuple]:
        """The component record called ``name``; NotFoundError lists the others."""
        for c in self.components:
            if c[0] == name:
                return c
        raise NotFoundError(f"component of {self.key}", name, [c[0] for c in self.components])

    def anchor(self, pin: str) -> PinAnchor:
        """The wire anchor of electrical pin ``pin`` (name or number); NotFoundError lists the others."""
        for a in self.anchors:
            if a.pin == pin or (a.number and a.number == str(pin)):
                return a
        raise NotFoundError(f"anchor of {self.key}", pin, [a.pin for a in self.anchors])

    def bounds(self) -> tuple[Vec3, Vec3]:
        """Axis-aligned bounds ((xmin, ymin, zmin), (xmax, ymax, zmax)) of the model, mm."""
        lo = [math.inf] * 3
        hi = [-math.inf] * 3

        def grow(c: Vec3, half: Vec3) -> None:
            for i in range(3):
                lo[i] = min(lo[i], c[i] - half[i])
                hi[i] = max(hi[i], c[i] + half[i])

        if self.pcb is not None:
            pl, pw, pt = self.pcb
            grow((0.0, 0.0, pt / 2), (pl / 2, pw / 2, pt / 2))
        for _name, kind, center, size in self.components:
            if kind == "box":
                grow(center, (size[0] / 2, size[1] / 2, size[2] / 2))
            else:
                grow(center, (size[0] / 2, size[0] / 2, size[1] / 2))
        return tuple(lo), tuple(hi)  # type: ignore[return-value]

    def features_above(self, z: float) -> list[tuple[str, str, tuple, tuple]]:
        """Components reaching above height ``z`` (e.g. the PCB top) — for boss clearance."""
        out = []
        for c in self.components:
            _n, kind, center, size = c
            top = center[2] + (size[2] if kind == "box" else size[1]) / 2
            if top > z + 1e-6:
                out.append(c)
        return out

    # -- geometry ---------------------------------------------------------------------------------
    def shape(self) -> "Compound":
        """Reference 3D model (PCB with holes + component boxes/cylinders), labelled and coloured."""
        cached = _SHAPE_CACHE.get(self)
        if cached is None:
            cached = _build_shape(self)
            _SHAPE_CACHE[self] = cached
        return copy.copy(cached)

    def part(self, name: str | None = None) -> PartSpec:
        """The module as a reference :class:`PartSpec` for an Assembly."""
        return PartSpec(name or self.key, self.shape(), kind="reference", material="module",
                        color="#3a3a3a", meta={"module": self.key, "elec_key": self.elec_key})

    def panel_cutout(self, thickness: float, clearance: float = 0.3) -> "Part":
        """Cutter for the panel opening(s), in the module frame, along +Z.

        Each window grows by ``clearance`` per side. The cutter spans the panel (``thickness``)
        where the mounting rule puts it — ``behind``: z ∈ [front_height, front_height + thickness];
        ``through`` / ``surface``: z ∈ [−thickness, 0] — and pokes EPS through both faces.
        """
        if not self.windows:
            raise ValidationError(f"module {self.key!r} has no panel window")
        if not (thickness > 0 and math.isfinite(thickness)):
            raise ValidationError(f"panel thickness must be > 0 mm, got {thickness!r}")
        if clearance < 0:
            raise ValidationError("clearance must be >= 0 mm")
        import build123d as bd

        from piforge.mech.primitives import _as_part
        z0 = self.front_height if self.panel_mount == "behind" else -thickness
        depth = thickness + 2 * EPS
        cutters = []
        for shape, (x, y), size in self.windows:
            if shape == "rect":
                face = bd.Rectangle(size[0] + 2 * clearance, size[1] + 2 * clearance)
            else:
                face = bd.Circle(size[0] / 2 + clearance)
            solid = bd.extrude(face, depth)
            cutters.append(solid.moved(bd.Location((x, y, z0 - EPS))))
        out = cutters[0]
        for c in cutters[1:]:
            out = out + c
        return _as_part(out)


_SHAPE_CACHE: dict[ModuleModel, Any] = {}


def _build_shape(m: ModuleModel) -> "Compound":
    import build123d as bd

    children = []
    if m.pcb is not None:
        pl, pw, pt = m.pcb
        if m.pcb_round:
            pcb = bd.extrude(bd.Circle(pl / 2), pt)
            if m.pcb_hole_d > 0:
                pcb = pcb - bd.extrude(bd.Circle(m.pcb_hole_d / 2), pt + 2 * EPS).moved(bd.Location((0, 0, -EPS)))
        else:
            pcb = bd.extrude(bd.Rectangle(pl, pw), pt)
        for x, y in m.holes:
            pcb = pcb - bd.Cylinder(m.hole_d / 2, pt + 2 * EPS).moved(bd.Location((x, y, pt / 2)))
        pcb = bd.Part(pcb.wrapped)
        pcb.label, pcb.color = "pcb", bd.Color(m.pcb_color)
        children.append(pcb)
    for name, kind, center, size in m.components:
        if kind == "box":
            solid = bd.Box(*size)
        else:
            solid = bd.Cylinder(size[0] / 2, size[1])
        solid = solid.moved(bd.Location(tuple(center)))
        solid.label = name
        solid.color = bd.Color(_component_color(name))
        children.append(solid)
    return bd.Compound(children=children, label=m.name)


def _module_from_record(rec: dict[str, Any]) -> ModuleModel:
    def win(w):
        if w is None:
            return None
        shape, center, size = w
        return (shape, (float(center[0]), float(center[1])), tuple(float(v) for v in size))

    comps = tuple((n, k, tuple(float(v) for v in c), tuple(float(v) for v in s))
                  for n, k, c, s in rec["components"])
    return ModuleModel(
        key=rec["key"], name=rec["name"],
        pcb=tuple(float(v) for v in rec["pcb"]) if rec["pcb"] is not None else None,  # type: ignore[arg-type]
        holes=tuple((float(x), float(y)) for x, y in rec["holes"]), hole_d=float(rec["hole_d"]),
        components=comps, window=win(rec["window"]), panel_mount=rec["panel_mount"],
        front_height=float(rec["front_height"]), elec_key=rec["elec_key"], source=rec["source"],
        extra_windows=tuple(win(w) for w in rec.get("extra_windows", ())),  # type: ignore[misc]
        pcb_round=bool(rec.get("pcb_round", False)), pcb_hole_d=float(rec.get("pcb_hole_d", 0.0)),
        pcb_color=rec.get("pcb_color", _PCB_COLOR),
        anchors=tuple(anchor_from_record(a) for a in rec.get("anchors", ())),
        aliases=tuple(rec.get("aliases", ())))


MODULES: dict[str, ModuleModel] = {k: _module_from_record(v) for k, v in MODULE_DATA.items()}


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-.]+", "", str(name).strip().lower())


_ALIASES: dict[str, str] = {}
for _m in MODULES.values():
    for _a in (*_m.aliases, _m.key):  # keys win over aliases
        _ALIASES[_norm(_a)] = _m.key


def get_module(key: "str | ModuleModel") -> ModuleModel:
    """Look up a module model by key (``ssd1306_096_i2c``) or alias (``oled``); models pass through."""
    if isinstance(key, ModuleModel):
        return key
    k = _ALIASES.get(_norm(key))
    if k is None:
        raise NotFoundError("module", key, MODULES)
    return MODULES[k]


def _check_tables() -> None:
    for m in MODULES.values():
        if m.panel_mount not in PANEL_MOUNTS:
            raise ValidationError(f"module {m.key}: panel_mount {m.panel_mount!r} not in {PANEL_MOUNTS}")
        if m.holes and m.hole_d <= 0:
            raise ValidationError(f"module {m.key}: holes need hole_d > 0")


_check_tables()
