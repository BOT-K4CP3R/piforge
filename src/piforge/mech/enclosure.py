"""Enclosure generator: a printable base + lid around a Raspberry Pi, with standoffs, automatic
port cutouts, panel modules (displays, buttons, LEDs…), vents, an engraved label and screw or
snap lid fastening.

Enclosure frame (binding): origin at the centre of the base's outer bottom face, Z up, X along
the board's long edge (GPIO header towards +Y). The base is printed as modelled (floor on the
bed); the lid is printed outside face down (``print_rotation = (180, 0, 0)``).

Faces (``VentSpec.face``, ``PanelItem.face``, ``label_face``): ``top`` (lid), ``bottom`` (floor),
``-x``, ``+x``, ``-y``, ``+y`` (aliases: front = −y, back = +y, left = −x, right = +x). Each face
has a local frame: origin at the centre of its INNER surface, local Z = outward normal, local X
to the right and local Y up as seen from outside (for walls: Y = world +Z; for the top: X = +x,
Y = +y; for the bottom: X = +x, Y = −y). Offsets ``(u, v)`` are in that local XY.

Printability rules applied automatically: every cutter overlaps the faces it opens by EPS;
openings in side walls get flat bridged roofs up to the printer's bridge limit and 45° gables
above it; round wall openings become teardrops; horizontal bosses get a 45° underside.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import build123d as bd
import numpy as np
from build123d import Align, Location, Part

from piforge.core.errors import NotFoundError, ValidationError
from piforge.core.report import Report, Severity
from piforge.fab.profiles import get_material, get_printer
from piforge.mech.assembly import Assembly, location_to_matrix, matrix_to_location
from piforge.mech.boards import BoardModel, Port, get_board
from piforge.mech.fasteners import get_size, hole_compensation, screw, standoff
from piforge.mech.modules import ModuleModel, get_module
from piforge.mech.part import PartSpec
from piforge.mech.primitives import EPS, _as_part, hex_vents, rounded_box, text_solid, vent_slots

log = logging.getLogger(__name__)

FACES = ("top", "bottom", "-x", "+x", "-y", "+y")
_FACE_ALIASES = {"front": "-y", "back": "+y", "left": "-x", "right": "+x", "lid": "top", "floor": "bottom"}
WALL_FACES = ("-x", "+x", "-y", "+y")
# src: ISO 4762 / ISO 7380 preferred screw lengths (mm) commonly stocked for M2–M5
SCREW_LENGTHS = (3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 25, 30)
LIP_GAP = 0.3  # src: est — sliding fit between the lid lip and the base walls (≈ clearance_sliding)
LIP_W = 1.2  # src: est — three 0.4 mm perimeters
PILLAR_WALL = 1.2  # src: est — material around a lid-screw pilot hole (3 × 0.4 mm perimeters; fused to the walls)
PILLAR_SKIN = 1.2  # src: est — material between a lid-screw pilot hole and the outer surface (3 perimeters)
LID_ENGAGEMENT = 6.0  # src: est — target thread engagement of a self-tapping M3 in PETG (2 × d)
MIN_ENGAGEMENT = 4.0  # src: task brief — lid screws must engage ≥ 4 mm
_KEEP = 0.3  # mm — plan clearance between lid-screw pillars and the board / its connectors (board drops in)
# Fingertip that pushes / pulls a microSD card or presses an edge button: (width, height, length).
# src: est — adult index fingertip, distal breadth ≈ 15–18 mm (ANSUR II hand data); the nail-side tip
#      that touches a card edge / button cap is narrower and flattens to ≈ 14 × 8 mm.
FINGERTIP: dict[str, tuple[float, float, float]] = {"microsd": (14.0, 8.0, 20.0), "button": (14.0, 8.0, 20.0)}
FINGER_REACH = 3.0  # src: est — a fingertip/nail still operates a card or button whose mouth is ≤ 3 mm away
PORT_RECESS_MAX = 4.0  # src: est — plug mouths deeper than this behind the outer face make short plugs hard to seat
# Boards that need ventilation in a closed box (sustained load).
# src: Raspberry Pi product briefs — Pi 4 B up to ≈ 6.4 W, Pi 5 up to ≈ 12 W (27 W PSU); both throttle at 80–85 °C
HOT_BOARDS = ("rpi4b", "rpi5")
_BASE_COLOR, _LID_COLOR = "#3d7dd8", "#f2b134"


# ==============================================================================================
# specs
# ==============================================================================================
@dataclass
class VentSpec:
    """Ventilation area on a face: ``size`` (w, h) in face-local XY, centred at ``offset``.

    ``style``: ``slots`` (2 mm slots, vertical on walls so their roofs bridge) or ``hex``.
    """

    face: str
    size: tuple[float, float]
    offset: tuple[float, float] = (0, 0)
    style: str = "slots"


@dataclass
class PanelItem:
    """A module mounted on a face (see :mod:`piforge.mech._module_data` for the mount types).

    ``offset`` (u, v) in face-local XY; ``rotation`` (degrees) about the face normal.
    ``mount=False`` skips screw bosses / holes (window only).
    """

    module: "str | ModuleModel"
    face: str
    offset: tuple[float, float] = (0, 0)
    rotation: float = 0.0
    mount: bool = True


@dataclass
class EnclosureSpec:
    """Parameters of an :class:`Enclosure` (mm unless stated)."""

    board: "str | BoardModel" = "rpi4b"
    wall: float = 2.0
    floor: float = 2.0
    lid_thickness: float = 2.0
    side_clearance: float = 1.5
    top_clearance: float = 5.0
    inner_height: float | None = None
    corner_radius: float = 3.0
    standoff_height: float = 5.0
    board_screw: str = "M2.5"
    board_fastening: str = "tap"  # "tap" | "insert"
    lid_fastening: str = "screws"  # "screws" | "snap"
    lid_screw: str = "M3"
    ports: tuple[str, ...] | None = None  # None = every horizontal edge port of the board
    port_clearance: float = 0.6  # per side, around max(opening, plug envelope)
    vents: tuple[VentSpec, ...] = ()
    panel_items: tuple[PanelItem, ...] = ()
    label: str | None = None
    label_face: str = "top"
    label_size: float = 6.0
    label_offset: tuple[float, float] = (0, 0)
    extra_space: tuple[float, float, float, float] = (0, 0, 0, 0)  # extra inner room on -x, +x, -y, +y
    printer: str = "generic"
    material: str = "PETG"
    finger_notches: bool = True  # fingertip-sized notches at microSD slots / edge buttons (see FINGERTIP)


@dataclass
class _Item:
    """A resolved panel item: module, face, placement and the features it adds."""

    index: int
    spec: PanelItem
    module: ModuleModel
    face: str
    loc: Location  # module frame → enclosure frame
    adds: list = field(default_factory=list)  # solids fused to the base/lid
    cuts: list = field(default_factory=list)  # cutters


# ==============================================================================================
# small geometry helpers
# ==============================================================================================
def _norm_face(face: str) -> str:
    f = str(face).strip().lower()
    f = _FACE_ALIASES.get(f, f)
    if f not in FACES:
        raise ValidationError(f"face {face!r} is not one of {FACES} (or front/back/left/right)")
    return f


def _part(shape: Any) -> Part:
    """Any build123d shape as a compound-backed Part (``.volume`` is wrong on a bare-solid Part)."""
    return _as_part(shape)


def _face_poly(points: list[tuple[float, float]]) -> bd.Face:
    return bd.Face(bd.Wire.make_polygon([bd.Vector(x, y, 0) for x, y in points], close=True))


def _extrude(face: Any, z0: float, z1: float) -> Part:
    """Extrude a planar XY face/sketch to the slab z ∈ [z0, z1] (local coordinates)."""
    return _part(bd.extrude(face, z1 - z0, dir=(0, 0, 1)).moved(Location((0, 0, z0))))


def _rot2(x: float, y: float, deg: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c * x - s * y, s * x + c * y)


def _teardrop_cap(r: float, top: float | None) -> tuple[float, float]:
    """Height of a wall teardrop's roof over a circle of radius ``r`` and the width of its flat top.

    The 45° roof peaks at r·√2; when ``top`` (the highest local y the opening may reach) is lower,
    the tip is cut off there — never below the circle (y = r) — leaving a short flat bridge.
    """
    tip = r * math.sqrt(2)
    if top is None or top >= tip:
        return tip, 0.0
    cap = max(top, r)
    return cap, 2.0 * (tip - cap)


def _opening_face(kind: str, size: tuple[float, ...], *, wall: bool, max_bridge: float,
                  rotation: float = 0.0, top: float | None = None) -> Any:
    """Opening outline centred on the origin (local XY, local +Y = up on walls).

    On walls (printed vertically) rectangles wider than the bridge limit get a 45° gable and
    circles become teardrops, so no opening needs support. ``top`` (walls): highest local y the
    teardrop may reach — its tip is flattened into a short bridge there (see :func:`_teardrop_cap`).
    """
    if kind == "circle":
        r = size[0] / 2
        face = bd.Circle(r)
        if wall:
            k = r / math.sqrt(2)
            cap, flat = _teardrop_cap(r, top)
            pts = [(-k, k), (k, k), (0.0, cap)] if flat <= 0.0 else \
                [(-k, k), (k, k), (flat / 2, cap), (-flat / 2, cap)]
            face = face + _face_poly(pts)
        return face
    w, h = size
    if not wall or abs(math.remainder(rotation, 90.0)) > 1e-6:
        return bd.Rectangle(w, h, rotation=rotation)
    if abs(math.remainder(rotation, 180.0)) > 1e-6:  # quarter turn: swap
        w, h = h, w
    if w <= max_bridge - 1.0:
        return bd.Rectangle(w, h)
    flat = max(max_bridge - 2.0, 1.0)  # bridged top, 45° flanks up to it
    rise = (w - flat) / 2
    return _face_poly([(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (flat / 2, h / 2 + rise),
                       (-flat / 2, h / 2 + rise), (-w / 2, h / 2)])


def _boss_face(od: float, *, wall: bool) -> Any:
    """Boss outline: a circle; on walls a down-pointing teardrop (45° underside)."""
    r = od / 2
    face = bd.Circle(r)
    if wall:
        k = r / math.sqrt(2)
        face = face + _face_poly([(-k, -k), (0.0, -r * math.sqrt(2)), (k, -k)])
    return face


def _diamond(length: float, half_diag: float, axis: str) -> Part:
    """Prism with a 45°-rotated square section (V groove / snap bump), centred on the origin."""
    s = half_diag * math.sqrt(2)
    if axis == "x":
        box = bd.Box(length, s, s).rotate(bd.Axis.X, 45)
    else:
        box = bd.Box(s, length, s).rotate(bd.Axis.Y, 45)
    return _part(box)


def _screw_for_hole(d: float) -> str:
    """Metric screw that fits a module mounting hole of Ø ``d``."""
    for name, limit in (("M2", 2.4), ("M2.5", 2.9), ("M3", 3.7), ("M4", 4.7)):
        if d < limit:
            return name
    return "M5"


def _pick_length(min_len: float, max_len: float, prefer: float) -> int:
    """Stock screw length in [min_len, max_len] closest to ``prefer`` (or the longest ≤ max_len)."""
    ok = [L for L in SCREW_LENGTHS if min_len - 1e-9 <= L <= max_len + 1e-9]
    if ok:
        return min(ok, key=lambda L: (abs(L - prefer), -L))
    shorter = [L for L in SCREW_LENGTHS if L <= max_len + 1e-9]
    return max(shorter) if shorter else SCREW_LENGTHS[0]


def _rect_dist(px: float, py: float, x0: float, x1: float, y0: float, y1: float) -> float:
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


# ==============================================================================================
# the generator
# ==============================================================================================
class Enclosure:
    """Base + lid around ``spec.board``. Build once; inspect ``base``, ``lid``, ``assembly()``,
    ``checks()``. Sizes: ``inner_size`` (cavity L × W × H), ``outer_size`` (closed box)."""

    def __init__(self, spec: EnclosureSpec | None = None):
        self.spec = spec or EnclosureSpec()
        self._validate()
        self.board: BoardModel = get_board(self.spec.board)
        self.printer = get_printer(self.spec.printer)
        get_material(self.spec.material)
        self._comp = hole_compensation(self.printer)
        self._select_ports()
        self._layout()
        self._build()

    # -- validation ---------------------------------------------------------------------------
    def _validate(self) -> None:
        s = self.spec
        for name in ("wall", "floor", "lid_thickness", "standoff_height", "label_size"):
            v = getattr(s, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                raise ValidationError(f"EnclosureSpec.{name} must be > 0 mm, got {v!r}")
        for name in ("side_clearance", "top_clearance", "corner_radius", "port_clearance"):
            v = getattr(s, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
                raise ValidationError(f"EnclosureSpec.{name} must be >= 0 mm, got {v!r}")
        if s.inner_height is not None and not s.inner_height > 0:
            raise ValidationError("EnclosureSpec.inner_height must be > 0 mm (or None for automatic)")
        if len(s.extra_space) != 4 or any(v < 0 for v in s.extra_space):
            raise ValidationError("extra_space must be four values >= 0 (-x, +x, -y, +y)")
        if s.board_fastening not in ("tap", "insert"):
            raise ValidationError(f"board_fastening must be 'tap' or 'insert', got {s.board_fastening!r}")
        if s.lid_fastening not in ("screws", "snap"):
            raise ValidationError(f"lid_fastening must be 'screws' or 'snap', got {s.lid_fastening!r}")
        get_size(s.board_screw)
        get_size(s.lid_screw)
        _norm_face(s.label_face)
        for v in s.vents:
            _norm_face(v.face)
            if v.style not in ("slots", "hex"):
                raise ValidationError(f"vent style must be 'slots' or 'hex', got {v.style!r}")
            if len(v.size) != 2 or min(v.size) <= 0:
                raise ValidationError(f"vent size must be (w, h) > 0, got {v.size!r}")
        for it in s.panel_items:
            _norm_face(it.face)
            get_module(it.module)

    def _select_ports(self) -> None:
        b = self.board
        if self.spec.ports is None:
            self.ports: tuple[Port, ...] = b.edge_ports()
        else:
            self.ports = tuple(b.port(p) for p in self.spec.ports)
        names = {p.name for p in self.ports}
        # an explicit, non-empty selection deliberately leaves the other edge ports covered
        self.excluded_ports: tuple[Port, ...] = tuple(p for p in b.edge_ports() if p.name not in names) \
            if self.spec.ports else ()
        self._wall_ports = [p for p in self.ports if p.horizontal]
        self._top_ports = [p for p in self.ports if not p.horizontal and p.direction[2] > 0]

    # -- layout ---------------------------------------------------------------------------------
    def _finger_notch(self, p: Port) -> bool:
        """True when ``p`` (a wall port) gets a fingertip-sized notch."""
        return self.spec.finger_notches and p.horizontal and p.kind in FINGERTIP

    def _port_cut_size(self, p: Port) -> tuple[float, float]:
        c = self.spec.port_clearance
        w, h = max(p.opening[0], p.plug[0]), max(p.opening[1], p.plug[1])
        if self._finger_notch(p):
            fw, fh, _ = FINGERTIP[p.kind]
            w, h = max(w, fw), max(h, fh)
        return (w + 2 * c, h + 2 * c)

    @staticmethod
    def _port_min_width(p: Port) -> float:
        """Narrowest acceptable cutout width (plug / card + 0.1 mm per side)."""
        return max(p.opening[0], p.plug[0]) + 0.2

    def _layout(self) -> None:
        s, b = self.spec, self.board
        ex = dict(zip(("-x", "+x", "-y", "+y"), s.extra_space))
        cl = {k: s.side_clearance + ex[k] for k in ex}
        cut = {p.name for p in self._wall_ports}
        for p in b.edge_ports():  # uncut connectors that stick out must still clear the wall
            if p.name in cut:
                continue
            (bx, by, _), (sx, sy, _) = p.body_center, p.body_size
            out = {"-x": -(bx - sx / 2), "+x": bx + sx / 2 - b.length,
                   "-y": -(by - sy / 2), "+y": by + sy / 2 - b.width}[p.edge]
            cl[p.edge] = max(cl[p.edge], out + LIP_GAP)
        self._screws = s.lid_fastening == "screws"
        ls = get_size(s.lid_screw)
        self._pillar_hole = ls.tap_plastic + self._comp
        self._pillar_r = self._pillar_hole / 2 + PILLAR_WALL
        self._hole_skin = max(self.printer.min_wall, 0.8)  # pilot hole ↔ port cutout
        self._pillar_plan: list[tuple[int, int, float, float, float, float]] = []
        if self._screws:
            self._solve_pillars(cl)
        self.clearances = cl
        il = b.length + cl["-x"] + cl["+x"]
        iw = b.width + cl["-y"] + cl["+y"]
        stack = s.standoff_height + b.thickness + b.top_height
        self._stack = stack
        x0, y0 = -il / 2 + cl["-x"], -iw / 2 + cl["-y"]
        z0 = s.floor + s.standoff_height
        self.board_location = Location((x0, y0, z0))
        self._il, self._iw = il, iw
        if s.inner_height is not None:
            ih = float(s.inner_height)
        else:
            ih = stack + s.top_clearance
            ih = max(ih, self._height_for_top_modules())
        self.inner_size = (il, iw, ih)
        self._ih = ih
        self._h_base = s.floor + ih
        self.outer_size = (il + 2 * s.wall, iw + 2 * s.wall, s.floor + ih + s.lid_thickness)
        room = ih - stack
        if self._screws:
            self._lip_h = min(2.5, max(room - 1.0, 0.0))
        else:
            self._lip_h = min(3.0, max(room - 1.0, 2.0))
        if self._lip_h < 1.0:
            self._lip_h = 0.0
        # pillar centres (enclosure frame) and their insets (e_x, e_y) from the inner wall faces
        self._pillars = [(x0 + px, y0 + py) for _sx, _sy, px, py, _ex, _ey in self._pillar_plan]
        self._pillar_insets = [(ex, ey) for *_r, ex, ey in self._pillar_plan]

    def _plan_obstacles(self) -> list[tuple[str, tuple, float]]:
        """Plan-view keep-outs for the lid-screw pillars, board coordinates:
        ``(kind, (x0, x1, y0, y1, corner_r), distance required from the pillar centre)``."""
        b = self.board
        body = self._pillar_r + _KEEP
        hole = self._pillar_hole / 2 + self._hole_skin
        # pillars are trimmed to the board outline + _KEEP, so only the pilot hole's skin limits them there
        obs: list[tuple[str, tuple, float]] = [("board", (0.0, b.length, 0.0, b.width, b.corner_radius), hole + _KEEP)]
        for p in b.ports:
            (cx, cy, _), (sx, sy, _) = p.body_center, p.body_size
            obs.append(("port", (cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2, 0.0), body))
        for _n, (cx, cy, _), (sx, sy, _) in b.components:
            obs.append(("comp", (cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2, 0.0), body))
        far = 1e3
        for p in self._wall_ports:
            cx, cy, _ = p.center
            for kind, w, need, start in (("plug", p.plug[0], body, None), ("cut", self._port_min_width(p), hole, 0)):
                # plugs run outwards from the mouth; cutters through the wall from mid-board outwards
                if p.edge == "+x":
                    x0 = cx if start is None else b.length / 2
                    obs.append((kind, (x0, far, cy - w / 2, cy + w / 2, 0.0), need))
                elif p.edge == "-x":
                    x1 = cx if start is None else b.length / 2
                    obs.append((kind, (-far, x1, cy - w / 2, cy + w / 2, 0.0), need))
                elif p.edge == "+y":
                    y0 = cy if start is None else b.width / 2
                    obs.append((kind, (cx - w / 2, cx + w / 2, y0, far, 0.0), need))
                else:
                    y1 = cy if start is None else b.width / 2
                    obs.append((kind, (cx - w / 2, cx + w / 2, -far, y1, 0.0), need))
        return obs

    def _solve_pillars(self, cl: dict[str, float]) -> None:
        """Place the four lid-screw pillars in the cavity corners, growing the side clearances as
        little as possible (minimax over x and y together).

        A pillar may sink into the wall as long as the pilot hole keeps ``PILLAR_SKIN`` (and the
        screw head stays inside the outline, incl. the rounded corner). In plan it must clear the
        board's rounded outline, connector bodies, components and plug paths by ``_KEEP`` (the
        board drops in vertically), and keep ``hole_skin`` between its pilot hole and the narrowest
        allowed port cutout. Sets ``self._pillar_plan`` = [(sx, sy, px, py, ex, ey)] in board
        coordinates, (ex, ey) = inset of the centre from the inner wall faces.
        """
        s, b = self.spec, self.board
        rh, rp = self._pillar_hole / 2, self._pillar_r
        ls = get_size(s.lid_screw)
        r_out = max(rh + PILLAR_SKIN, ls.head_socket_d / 2,  # head inside the outline
                    (ls.clearance_normal + self._comp) / 2 + self.printer.min_wall + self._lid_chamfer())
        R, w = s.corner_radius, s.wall
        st = 0.05
        es = np.round(np.arange(-w, rp + 1e-9, st) / st) * st
        EX, EY = (a.ravel() for a in np.meshgrid(es, es))
        c = R - w  # outer corner arc centre, in inset coordinates
        in_arc = (EX < c) & (EY < c)
        d_out = np.where(in_arc, R - np.hypot(EX - c, EY - c), np.minimum(EX, EY) + w)
        ok = d_out >= r_out - 1e-9
        EX, EY = EX[ok], EY[ok]
        if EX.size == 0:
            raise ValidationError("walls too thin for lid-screw pillars; use lid_fastening='snap' or a thicker wall")
        order = np.lexsort((np.abs(EX - EY), EX + EY))  # closest to the corner first
        EX, EY = EX[order], EY[order]
        obs = self._plan_obstacles()
        G, gst = 8.0, 0.05
        gs = np.round(np.arange(0.0, G + 1e-9, gst) / gst) * gst
        pairs = sorted(((gx, gy) for gx in gs for gy in gs), key=lambda g: (max(g), g[0] + g[1]))

        def centres(sx: int, sy: int, cx: float, cy: float) -> tuple[np.ndarray, np.ndarray]:
            px = EX - cx if sx < 0 else b.length + cx - EX
            py = EY - cy if sy < 0 else b.width + cy - EY
            return px, py

        def feasible(px: np.ndarray, py: np.ndarray, near: list) -> np.ndarray:
            good = np.ones(px.shape, dtype=bool)
            for _kind, (x0, x1, y0, y1, r), need in near:
                dx = np.maximum(np.maximum(x0 + r - px, px - (x1 - r)), 0.0)
                dy = np.maximum(np.maximum(y0 + r - py, py - (y1 - r)), 0.0)
                good &= np.hypot(dx, dy) - r >= need - 1e-9
            return good

        corners = [(sx, sy) for sx in (-1, 1) for sy in (-1, 1)]
        side = {(sx, sy): ("-x" if sx < 0 else "+x", "-y" if sy < 0 else "+y") for sx, sy in corners}
        growth: dict[str, float] = {k: 0.0 for k in ("-x", "+x", "-y", "+y")}
        near_obs: dict[tuple[int, int], list] = {}
        for sx, sy in corners:
            kx, ky = side[(sx, sy)]
            span = G + w + rp + 2 * R + 10.0
            cxr = (-cl[kx] - span, -cl[kx] + span) if sx < 0 else (b.length + cl[kx] - span, b.length + cl[kx] + span)
            cyr = (-cl[ky] - span, -cl[ky] + span) if sy < 0 else (b.width + cl[ky] - span, b.width + cl[ky] + span)
            near = [o for o in obs if o[1][0] < cxr[1] and o[1][1] > cxr[0] and o[1][2] < cyr[1] and o[1][3] > cyr[0]]
            near_obs[(sx, sy)] = near
            for gx, gy in pairs:
                if feasible(*centres(sx, sy, cl[kx] + gx, cl[ky] + gy), near).any():
                    growth[kx] = max(growth[kx], float(gx))
                    growth[ky] = max(growth[ky], float(gy))
                    break
            else:
                raise ValidationError("cannot place the lid-screw pillars; use lid_fastening='snap'")
        for k, g in growth.items():
            cl[k] += g
        plan = []
        for sx, sy in corners:
            kx, ky = side[(sx, sy)]
            px, py = centres(sx, sy, cl[kx], cl[ky])
            hit = np.flatnonzero(feasible(px, py, near_obs[(sx, sy)]))
            if hit.size == 0:  # pragma: no cover - growth only frees space
                raise ValidationError("cannot place the lid-screw pillars; use lid_fastening='snap'")
            i = int(hit[0])
            plan.append((sx, sy, float(px[i]), float(py[i]), float(EX[i]), float(EY[i])))
        self._pillar_plan = plan

    def _face_matrix(self, face: str) -> np.ndarray:
        """4×4 transform face-local → enclosure frame (origin at the inner face centre)."""
        il, iw, ih, f = self._il, self._iw, self._ih, self.spec.floor
        zc = f + ih / 2
        cols = {  # local X, Y, Z axes and origin
            "top": ((1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, f + ih)),
            "bottom": ((1, 0, 0), (0, -1, 0), (0, 0, -1), (0, 0, f)),
            "+x": ((0, 1, 0), (0, 0, 1), (1, 0, 0), (il / 2, 0, zc)),
            "-x": ((0, -1, 0), (0, 0, 1), (-1, 0, 0), (-il / 2, 0, zc)),
            "-y": ((1, 0, 0), (0, 0, 1), (0, -1, 0), (0, -iw / 2, zc)),
            "+y": ((-1, 0, 0), (0, 0, 1), (0, 1, 0), (0, iw / 2, zc)),
        }[face]
        m = np.eye(4)
        m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = cols
        return m

    def _face_loc(self, face: str) -> Location:
        return matrix_to_location(self._face_matrix(face))

    def _thickness(self, face: str) -> float:
        s = self.spec
        return {"top": s.lid_thickness, "bottom": s.floor}.get(face, s.wall)

    def _inward(self, face: str) -> float:
        """How far wall cutters reach into the cavity (to notch the lid lip as well)."""
        return LIP_GAP + LIP_W + 0.2 if face in WALL_FACES else EPS

    def _height_for_top_modules(self) -> float:
        """Cavity height needed below top-mounted modules (module depth + 1 mm over the board)."""
        s, b = self.spec, self.board
        need = 0.0
        x0, y0 = -self._il / 2 + self.clearances["-x"], -self._iw / 2 + self.clearances["-y"]
        feats = [((0.0, b.length, 0.0, b.width), s.standoff_height + b.thickness)]
        for p in b.ports:
            (cx, cy, cz), (sx, sy, sz) = p.body_center, p.body_size
            feats.append(((cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2), s.standoff_height + cz + sz / 2))
        for _n, (cx, cy, cz), (sx, sy, sz) in b.components:
            feats.append(((cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2), s.standoff_height + cz + sz / 2))
        for it in s.panel_items:
            if _norm_face(it.face) != "top":
                continue
            m = get_module(it.module)
            (lx, ly, lz), (hx, hy, hz) = m.bounds()
            if m.panel_mount == "behind":
                depth = m.front_height - lz
            elif m.panel_mount == "through":
                depth = -lz - s.lid_thickness
            else:
                depth = hz + (s.standoff_height if m.pcb is not None else 0.0)
            corners = [_rot2(x, y, it.rotation) for x in (lx, hx) for y in (ly, hy)]
            ux = [c[0] + it.offset[0] - x0 for c in corners]
            uy = [c[1] + it.offset[1] - y0 for c in corners]
            fx0, fx1, fy0, fy1 = min(ux), max(ux), min(uy), max(uy)
            under = 0.0
            for (bx0, bx1, by0, by1), top in feats:
                if bx0 < fx1 and fx0 < bx1 and by0 < fy1 and fy0 < by1:
                    under = max(under, top)
            need = max(need, under + depth + 1.0)
        return need

    # -- building -------------------------------------------------------------------------------
    def _build(self) -> None:
        s = self.spec
        self._feature_findings: list[tuple[str, Severity, str, str, str, dict]] = []
        self._items = [self._resolve_item(i, it) for i, it in enumerate(s.panel_items)]
        base = self._build_base()
        lid = self._build_lid()
        meta = {"printer": self.printer.name, "board": self.board.key}
        self.base = PartSpec("enclosure_base", base, kind="printed", material=s.material, color=_BASE_COLOR,
                             print_rotation=(0, 0, 0), meta=dict(meta, role="base"))
        self.lid = PartSpec("enclosure_lid", lid, kind="printed", material=s.material, color=_LID_COLOR,
                            print_rotation=(180, 0, 0), meta=dict(meta, role="lid"))

    def _local(self, face: str, solid: Part) -> Part:
        return _part(solid.moved(self._face_loc(face)))

    def _resolve_item(self, index: int, it: PanelItem) -> _Item:
        s = self.spec
        face = _norm_face(it.face)
        m = get_module(it.module)
        t = self._thickness(face)
        wall = face in WALL_FACES
        u, v = (float(it.offset[0]), float(it.offset[1]))
        rot = float(it.rotation)
        place = Location((u, v, 0)) * Location((0, 0, 0), (0, 0, 1), rot)
        if m.panel_mount == "behind":
            mount = Location((0, 0, -m.front_height))
        elif m.panel_mount == "through":
            mount = Location((0, 0, t))
        else:
            lift = s.standoff_height if m.pcb is not None else 0.0
            mount = Location((0, 0, -lift)) * Location((0, 0, 0), (1, 0, 0), 180)
        item = _Item(index, it, m, face, self._face_loc(face) * place * mount)

        def at(x: float, y: float) -> tuple[float, float]:
            rx, ry = _rot2(x, y, rot)
            return (u + rx, v + ry)

        lo_cut, hi_cut = -self._inward(face), t + EPS
        # windows
        if m.panel_mount != "surface":
            for kind, (wx, wy), size in m.windows:
                c = 0.3
                grown = tuple(d + 2 * c for d in size)
                cu, cv = at(wx, wy)
                top = self._round_opening_top(index, m, kind, grown, cv) if wall else None
                face2d = _opening_face(kind, grown, wall=wall, max_bridge=self.printer.max_bridge_mm, rotation=rot,
                                       top=top)
                item.cuts.append(self._local(face, _extrude(face2d, lo_cut, hi_cut).moved(Location((cu, cv, 0)))))
        if not it.mount or not m.holes:
            return item
        size = _screw_for_hole(m.hole_d)
        ms = get_size(size)
        tap = ms.tap_plastic + self._comp
        if m.panel_mount == "behind" and m.pcb is not None:
            length = m.front_height - m.pcb[2]
            self._add_bosses(item, face, t, [at(x, y) for x, y in m.holes],
                             [self._boss_od(m, x, y, ms.d, tap) for x, y in m.holes], length, tap)
        elif m.panel_mount == "behind":  # screws from outside through clearance holes
            hole = ms.clearance_normal + self._comp
            for x, y in m.holes:
                cu, cv = at(x, y)
                top = self._round_opening_top(index, m, "circle", (hole,), cv) if wall else None
                f2 = _opening_face("circle", (hole,), wall=wall, max_bridge=self.printer.max_bridge_mm, top=top)
                item.cuts.append(self._local(face, _extrude(f2, lo_cut, hi_cut).moved(Location((cu, cv, 0)))))
        elif m.panel_mount == "surface" and m.pcb is not None:
            od = max(2 * ms.d + 1.0, tap + 1.6)
            self._add_bosses(item, face, t, [at(x, y) for x, y in m.holes], [od] * len(m.holes),
                             s.standoff_height, tap)
        return item

    def _round_opening_top(self, index: int, m: ModuleModel, kind: str, size: tuple[float, ...],
                           cv: float) -> float | None:
        """Highest local y (relative to the opening centre ``cv``) a round wall opening may reach:
        one wall thickness below the base rim. Records ``ENCL.FEATURE_TOO_BIG`` when the circle
        itself does not fit between the floor and that line (ERROR) or the flattened teardrop needs
        a bridge longer than the printer's limit (WARNING)."""
        if kind != "circle":
            return None
        s = self.spec
        r = size[0] / 2
        half = self._ih / 2  # wall-local v of the rim (base top) and of the floor's inner face
        top = half - s.wall - cv
        subject = f"enclosure:panel_item:{index}"
        cap, flat = _teardrop_cap(r, top)
        data = {"module": m.key, "diameter": 2 * r, "center_v": cv, "rim_margin": s.wall}
        if r > top + 1e-6 or cv - r < -half - 1e-6:
            self._feature_findings.append((
                "ENCL.FEATURE_TOO_BIG", Severity.ERROR,
                f"The Ø{2 * r:.1f} mm opening of {m.key} (panel item {index}) does not fit in the "
                f"{self._ih:g} mm wall between the floor and {s.wall:g} mm below the rim.", subject,
                "Move it (offset v), make the box taller (inner_height) or use a smaller module.", data))
        elif flat > self.printer.max_bridge_mm + 1e-6:
            self._feature_findings.append((
                "ENCL.FEATURE_TOO_BIG", Severity.WARNING,
                f"The Ø{2 * r:.1f} mm opening of {m.key} (panel item {index}) is close to the rim: its "
                f"teardrop roof is flattened into a {flat:.1f} mm bridge (> {self.printer.max_bridge_mm:g} mm "
                f"for {self.printer.name}).", subject, "Move it down or make the box taller.",
                dict(data, bridge_mm=flat)))
        return top

    def _boss_od(self, m: ModuleModel, x: float, y: float, d: float, tap: float) -> float:
        """Largest boss Ø (≤ 2d + 1) that stays clear of the module's components on the PCB."""
        r = d + 0.5
        top = m.pcb[2] if m.pcb is not None else 0.0
        for _n, kind, (cx, cy, _cz), size in m.features_above(top):
            if kind == "box":
                dist = _rect_dist(x, y, cx - size[0] / 2, cx + size[0] / 2, cy - size[1] / 2, cy + size[1] / 2)
            else:
                dist = max(math.hypot(x - cx, y - cy) - size[0] / 2, 0.0)
            r = min(r, dist - 0.2)
        return max(2 * r, tap + 1.2)

    def _add_bosses(self, item: _Item, face: str, t: float, centres: list[tuple[float, float]],
                    ods: list[float], length: float, tap: float) -> None:
        """Bosses from the inner face inwards (local z ∈ [−length, t/2]) with a pilot hole that
        stops 0.6 mm below the outer surface."""
        if length < 0.4:  # module flat on the panel: pilot holes into the panel only
            length = 0.0
        wall = face in WALL_FACES
        for (cu, cv), od in zip(centres, ods):
            if length > 0:
                boss = _extrude(_boss_face(od, wall=wall), -length, t / 2).moved(Location((cu, cv, 0)))
                item.adds.append(self._local(face, boss))
            depth_top = max(t - max(self.printer.min_wall, 0.6), 0.3)  # keep a min_wall skin outside
            hole_face = _opening_face("circle", (tap,), wall=wall, max_bridge=self.printer.max_bridge_mm)
            hole = _extrude(hole_face, -length - EPS, depth_top).moved(Location((cu, cv, 0)))
            item.cuts.append(self._local(face, hole))

    def _face_items(self, part: str) -> list[_Item]:
        return [it for it in self._items if (it.face == "top") == (part == "lid")]

    def _vent_cutter(self, v: VentSpec) -> tuple[str, Part]:
        face = _norm_face(v.face)
        t = self._thickness(face)
        wall = face in WALL_FACES
        lo, hi = -self._inward(face), t + EPS
        depth = 2 * max(-lo, hi) + 0.1  # vent cutters are centred on z = 0
        w, h = v.size
        if v.style == "hex":
            cut = hex_vents(w, h, depth=depth)
        else:
            axis = ("y" if h >= 2.0 else "x") if wall else ("x" if w >= h else "y")
            cut = vent_slots(w, h, slot_axis=axis, depth=depth, r_ends=not wall)
        slab = bd.Box(w + 2, h + 2, hi - lo, align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((0, 0, lo)))
        cut = _part(cut & slab)
        return face, self._local(face, cut.moved(Location((v.offset[0], v.offset[1], 0))))

    def _wall_port_rects(self) -> dict[str, list[dict]]:
        """Port openings per wall in face-local (u, v): {face: [{u0, u1, v0, v1, min_*, ports}]}.

        Overlapping openings merge into one; webs thinner than 1.5 × min_wall between neighbours
        are widened by trimming clearance (never below plug + 0.1 mm per side), else merged.
        """
        bm = location_to_matrix(self.board_location)
        min_web = 1.5 * self.printer.min_wall
        out: dict[str, list[dict]] = {}
        for p in self._wall_ports:
            fm = self._face_matrix(p.edge)
            u, v = (np.linalg.inv(fm) @ (bm @ np.array([*p.center, 1.0])))[:2]
            w, h = self._port_cut_size(p)
            mw = self._port_min_width(p)
            out.setdefault(p.edge, []).append({
                "u0": u - w / 2, "u1": u + w / 2, "v0": v - h / 2, "v1": v + h / 2,
                "min_u0": u - mw / 2, "min_u1": u + mw / 2, "ports": [p.name]})
        for face, rects in out.items():
            while self._resolve_once(rects, min_web):
                pass
            self._trim_for_pillars(face, rects)
        return out

    def _trim_for_pillars(self, face: str, rects: list[dict]) -> None:
        """Pull cutout edges back so a lid-screw pilot hole keeps ``hole_skin`` of material
        (never below the plug / card width)."""
        if not self._pillars:
            return
        inv = np.linalg.inv(self._face_matrix(face))
        rr = self._pillar_hole / 2 + self._hole_skin
        reach = self._inward(face)
        for x, y in self._pillars:
            pu, _pv, pz = (inv @ np.array([x, y, 0.0, 1.0]))[:3]
            if pz + rr <= -reach:  # hole well inside the cavity: cutters do not get there
                continue
            for r in rects:
                if r["u0"] < pu + rr and pu - rr < r["u1"]:
                    if pu < (r["u0"] + r["u1"]) / 2:
                        r["u0"] = min(max(r["u0"], pu + rr), r["min_u0"])
                    else:
                        r["u1"] = max(min(r["u1"], pu - rr), r["min_u1"])

    @staticmethod
    def _resolve_once(rects: list[dict], min_web: float) -> bool:
        """Fix the first too-thin web / overlap between two openings; False when none is left."""
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                if b["u0"] < a["u0"]:
                    a, b = b, a
                if a["v1"] + min_web <= b["v0"] or b["v1"] + min_web <= a["v0"]:
                    continue  # vertically apart
                web = b["u0"] - a["u1"]
                if web >= min_web - 1e-6:
                    continue
                need = min_web - web
                room_a = max(a["u1"] - a["min_u1"], 0.0)
                room_b = max(b["min_u0"] - b["u0"], 0.0)
                if web >= 0 and room_a + room_b >= need - 1e-9:
                    ta = min(room_a, need / 2)
                    tb = min(room_b, need - ta)
                    ta = need - tb
                    a["u1"] -= ta
                    b["u0"] += tb
                else:  # overlapping or no room: one opening
                    merged = {"u0": min(a["u0"], b["u0"]), "u1": max(a["u1"], b["u1"]),
                              "v0": min(a["v0"], b["v0"]), "v1": max(a["v1"], b["v1"]),
                              "min_u0": min(a["min_u0"], b["min_u0"]), "min_u1": max(a["min_u1"], b["min_u1"]),
                              "ports": a["ports"] + b["ports"]}
                    rects[:] = [r for r in rects if r is not a and r is not b] + [merged]
                return True
        return False

    def _port_cutters(self) -> tuple[list[Part], list[Part]]:
        """(wall cutters for the base, top cutters for the lid)."""
        wall_cuts, top_cuts = [], []
        for face, rects in self._wall_port_rects().items():
            for r in rects:
                w, h = r["u1"] - r["u0"], r["v1"] - r["v0"]
                f2 = _opening_face("rect", (w, h), wall=True, max_bridge=self.printer.max_bridge_mm)
                cut = _extrude(f2, -self._inward(face), self._thickness(face) + EPS)
                cu, cv = (r["u0"] + r["u1"]) / 2, (r["v0"] + r["v1"]) / 2
                wall_cuts.append(self._local(face, cut.moved(Location((cu, cv, 0)))))
        bm = location_to_matrix(self.board_location)
        for p in self._top_ports:
            fm = self._face_matrix("top")
            local = np.linalg.inv(fm) @ (bm @ np.array([*p.center, 1.0]))
            w, h = self._port_cut_size(p)
            cut = _extrude(bd.Rectangle(w, h), -EPS, self.spec.lid_thickness + EPS)
            top_cuts.append(self._local("top", cut.moved(Location((local[0], local[1], 0)))))
        return wall_cuts, top_cuts

    def _label_cutter(self) -> tuple[str, Part] | None:
        loc = self._label_local()
        if loc is None:
            return None
        face, txt = loc
        return face, self._local(face, txt)

    def _label_local(self) -> tuple[str, Any] | None:
        """Engraved label text in face-local coordinates."""
        s = self.spec
        if not s.label:
            return None
        face = _norm_face(s.label_face)
        t = self._thickness(face)
        depth = min(0.6, t / 2)
        txt = text_solid(s.label, s.label_size, depth + EPS)
        bb = txt.bounding_box()
        span = self._il if face in ("top", "bottom", "-y", "+y") else self._iw
        if bb.max.X - bb.min.X > 0.8 * span:  # shrink long labels to fit
            txt = text_solid(s.label, s.label_size * 0.8 * span / (bb.max.X - bb.min.X), depth + EPS)
        txt = txt.moved(Location((s.label_offset[0], s.label_offset[1], t - depth)))
        return face, txt

    def _snap_features(self) -> tuple[list[Part], list[Part]]:
        """(bumps on the lid lip, matching V-grooves in the base walls)."""
        bumps, grooves = [], []
        if self._screws or self._lip_h <= 0:
            return bumps, grooves
        il, iw = self._il, self._iw
        z = self._h_base - self._lip_h / 2
        lx = min(10.0, il / 4)
        ly = min(10.0, iw / 3)
        for sy in (-1, 1):  # long walls: two each
            for x in (-il / 4, il / 4):
                bumps.append(_diamond(lx, 0.5, "x").moved(Location((x, sy * (iw / 2 - LIP_GAP), z))))
                grooves.append(_diamond(lx + 1.0, 0.45, "x").moved(Location((x, sy * iw / 2, z))))
        for sx in (-1, 1):
            bumps.append(_diamond(ly, 0.5, "y").moved(Location((sx * (il / 2 - LIP_GAP), 0, z))))
            grooves.append(_diamond(ly + 1.0, 0.45, "y").moved(Location((sx * il / 2, 0, z))))
        return [_part(b) for b in bumps], [_part(g) for g in grooves]

    def _build_base(self) -> Part:
        s, b = self.spec, self.board
        ol, ow, _ = self.outer_size
        H = self._h_base
        r_in = max(s.corner_radius - s.wall, 0.0)
        outer = rounded_box(ol, ow, H, s.corner_radius, bottom_chamfer=min(0.4, max(s.corner_radius - 0.01, 0.0)))
        cavity = rounded_box(self._il, self._iw, self._ih + EPS, r_in).moved(Location((0, 0, s.floor)))
        base = _part(outer - cavity)
        adds: list[Part] = []
        cuts: list[Part] = []
        # lid-screw pillars
        ls = get_size(s.lid_screw)
        if self._screws:
            keepout = self._pillar_keepout()
            for x, y in self._pillars:  # may sink into the walls: clip to the outer shell and the board
                cyl = bd.Cylinder(self._pillar_r, self._ih + EPS, align=(Align.CENTER, Align.CENTER, Align.MIN))
                adds.append(_part((cyl.moved(Location((x, y, s.floor - EPS))) & outer) - keepout))
            eng = self._lid_engagement()
            depth = min(eng + 1.5, self._ih - 0.6)
            for x, y in self._pillars:
                cuts.append(_part(bd.Cylinder(self._pillar_hole / 2, depth + EPS, align=(Align.CENTER, Align.CENTER, Align.MIN))
                                  .moved(Location((x, y, H - depth)))))
        # board standoffs
        bm = location_to_matrix(self.board_location)
        bs = get_size(s.board_screw)
        hole = "insert" if s.board_fastening == "insert" else "tap"
        for hx, hy in b.holes:
            x, y, _, _ = bm @ np.array([hx, hy, 0.0, 1.0])
            so = standoff(s.board_screw, s.standoff_height + EPS, hole=hole, printer=self.printer)
            adds.append(_part(so.moved(Location((x, y, s.floor - EPS)))))
            if hole == "insert":
                need = bs.insert_depth + 1.0
                extra = min(need - s.standoff_height, s.floor - 0.8)
                if extra > 0:
                    d = bs.insert_hole_d + self._comp
                    cuts.append(_part(bd.Cylinder(d / 2, extra + 2 * EPS, align=(Align.CENTER, Align.CENTER, Align.MIN))
                                      .moved(Location((x, y, s.floor - extra)))))
        # panel items on walls / floor
        for it in self._face_items("base"):
            adds.extend(it.adds)
            cuts.extend(it.cuts)
        wall_cuts, _ = self._port_cutters()
        if self._screws and wall_cuts:  # cutters reach into the cavity for the lid lip: spare the pillars
            guard = [_part(bd.Cylinder(self._pillar_r, self._h_base + 2.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
                           .moved(Location((x, y, -1.0)))) for x, y in self._pillars]
            wall_cuts = [_part(c.cut(*guard)) for c in wall_cuts]
        cuts.extend(wall_cuts)
        for v in s.vents:
            face, cut = self._vent_cutter(v)
            if face != "top":
                cuts.append(cut)
        label = self._label_cutter()
        if label is not None and label[0] != "top":
            cuts.append(label[1])
        _, grooves = self._snap_features()
        cuts.extend(grooves)
        if adds:
            base = _part(base.fuse(*adds).clean())
        if cuts:
            base = _part(base.cut(*cuts).clean())
        return self._single_solid(base, "base")

    def _lid_chamfer(self) -> float:
        return min(0.4, self.spec.lid_thickness / 3)

    def _pillar_keepout(self) -> Part:
        """Board outline grown by _KEEP, full cavity height: pillars are trimmed by it."""
        s, b = self.spec, self.board
        k = _KEEP
        face = bd.RectangleRounded(b.length + 2 * k, b.width + 2 * k, b.corner_radius + k,
                                   align=(Align.MIN, Align.MIN))
        x0, y0, _ = location_to_matrix(self.board_location)[:3, 3]
        return _extrude(face, -1.0, self._h_base + 1.0).moved(Location((x0 - k, y0 - k, 0)))

    def _lid_engagement(self) -> float:
        s = self.spec
        L = self._lid_screw_length()
        return L - s.lid_thickness

    def _lid_screw_length(self) -> int:
        s = self.spec
        max_eng = self._ih - 1.0
        return _pick_length(s.lid_thickness + MIN_ENGAGEMENT, s.lid_thickness + max_eng, s.lid_thickness + LID_ENGAGEMENT)

    def _build_lid(self) -> Part:
        s = self.spec
        ol, ow, _ = self.outer_size
        H, t = self._h_base, s.lid_thickness
        plate = rounded_box(ol, ow, t, s.corner_radius, bottom_chamfer=self._lid_chamfer())
        plate = _part(plate.rotate(bd.Axis.X, 180).moved(Location((0, 0, H + t))))
        adds: list[Part] = []
        cuts: list[Part] = []
        if self._lip_h > 0:
            r_in = max(s.corner_radius - s.wall, 0.0)
            lo_l, lo_w = self._il - 2 * LIP_GAP, self._iw - 2 * LIP_GAP
            outer = rounded_box(lo_l, lo_w, self._lip_h + EPS, max(r_in - LIP_GAP, 0.0))
            inner = rounded_box(lo_l - 2 * LIP_W, lo_w - 2 * LIP_W, self._lip_h + 3 * EPS,
                                max(r_in - LIP_GAP - LIP_W, 0.0)).moved(Location((0, 0, -EPS)))
            lip = _part((outer - inner).moved(Location((0, 0, H - self._lip_h))))
            for (x, y), (ex, ey) in zip(self._pillars, self._pillar_insets):
                sx, sy = math.copysign(1.0, x), math.copysign(1.0, y)
                rx = max(ex, 0.0) + self._pillar_r + 0.4  # remove the whole corner of the lip
                ry = max(ey, 0.0) + self._pillar_r + 0.4
                notch = bd.Box(rx + 2.0, ry + 2.0, self._lip_h + 3 * EPS, align=(Align.CENTER, Align.CENTER, Align.MIN))
                cx = sx * (self._il / 2 + 2.0 - (rx + 2.0) / 2)
                cy = sy * (self._iw / 2 + 2.0 - (ry + 2.0) / 2)
                lip = _part(lip - notch.moved(Location((cx, cy, H - self._lip_h - EPS))))
            wall_cuts, _ = self._port_cutters()
            if wall_cuts:
                lip = _part(lip.cut(*wall_cuts))
            adds.append(lip)
        bumps, _ = self._snap_features()
        adds.extend(bumps)
        for it in self._face_items("lid"):
            adds.extend(it.adds)
            cuts.extend(it.cuts)
        if self._screws:
            ls = get_size(s.lid_screw)
            d = ls.clearance_normal + self._comp
            for x, y in self._pillars:
                cuts.append(_part(bd.Cylinder(d / 2, t + 2 * EPS, align=(Align.CENTER, Align.CENTER, Align.MIN))
                                  .moved(Location((x, y, H - EPS)))))
        _, top_cuts = self._port_cutters()
        cuts.extend(top_cuts)
        for v in s.vents:
            face, cut = self._vent_cutter(v)
            if face == "top":
                cuts.append(cut)
        label = self._label_cutter()
        if label is not None and label[0] == "top":
            cuts.append(label[1])
        lid = plate
        if adds:
            lid = _part(lid.fuse(*adds).clean())
        if cuts:
            lid = _part(lid.cut(*cuts).clean())
        return self._single_solid(lid, "lid")

    @staticmethod
    def _single_solid(part: Part, what: str) -> Part:
        solids = part.solids()
        if len(solids) > 1:
            big = max(solids, key=lambda sl: sl.volume)
            lost = sum(sl.volume for sl in solids) - big.volume
            log.warning("enclosure %s: %d loose pieces (%.2f mm³) dropped", what, len(solids) - 1, lost)
            return _part(bd.Compound([big]))
        return part

    # -- outputs --------------------------------------------------------------------------------
    @property
    def parts(self) -> list[PartSpec]:
        """The printed parts: [base, lid]."""
        return [self.base, self.lid]

    def _screw_part(self, size: str, length: int) -> PartSpec:
        key = (size, length)
        cache = self.__dict__.setdefault("_screw_cache", {})
        if key not in cache:
            cache[key] = PartSpec(f"{size}x{length} socket screw", screw(size, length), kind="fastener",
                                  material="steel", color="#9aa0a6", meta={"size": size, "length": length})
        return cache[key]

    def board_screw_length(self) -> int:
        """Stock length of the board screws (PCB thickness + engagement in the standoff)."""
        s, b = self.spec, self.board
        bs = get_size(s.board_screw)
        max_eng = bs.insert_depth if s.board_fastening == "insert" else s.standoff_height - 0.5
        return _pick_length(b.thickness + 3.0, b.thickness + max_eng, b.thickness + max_eng)

    def assembly(self, *, include_board: bool = True, include_screws: bool = True,
                 include_modules: bool = True) -> Assembly:
        """Assembly of the enclosure (``explode`` offsets set for exploded views)."""
        s, b = self.spec, self.board
        lift = self.outer_size[2] + 15.0
        asm = Assembly(f"enclosure_{b.key}")
        asm.add(self.base, id="base")
        asm.add(self.lid, id="lid", explode=(0, 0, lift))
        if include_board:
            asm.add(b.part(), self.board_location, id="board", explode=(0, 0, lift * 0.45))
        if include_screws:
            bm = location_to_matrix(self.board_location)
            L = self.board_screw_length()
            for i, (hx, hy) in enumerate(b.holes):
                x, y, z, _ = bm @ np.array([hx, hy, b.thickness, 1.0])
                asm.add(self._screw_part(s.board_screw, L), (x, y, z), id=f"board_screw_{i}",
                        explode=(0, 0, lift * 0.7))
            if self._screws:
                L = self._lid_screw_length()
                for i, (x, y) in enumerate(self._pillars):
                    asm.add(self._screw_part(s.lid_screw, L), (x, y, self._h_base + s.lid_thickness),
                            id=f"lid_screw_{i}", explode=(0, 0, lift * 1.5))
        if include_modules:
            for it in self._items:
                fm = self._face_matrix(it.face)
                if it.face == "top":
                    ex = (0, 0, lift)
                else:
                    ex = tuple(float(v) for v in fm[:3, 2] * 25.0)
                asm.add(it.module.part(f"{it.module.key}_{it.index}"), it.loc, id=f"module_{it.index}_{it.module.key}",
                        explode=ex)
        return asm

    def port_depths(self) -> dict[str, dict[str, Any]]:
        """Per edge port: ``depth`` = mm from the connector mouth to the outer wall face along the
        port axis (negative: the mouth sticks out), ``cut`` (has a cutout), ``finger`` (operated
        by a fingertip: microSD, buttons) and ``notch`` (a fingertip notch is cut)."""
        bm = location_to_matrix(self.board_location)
        cut = {p.name for p in self._wall_ports}
        out: dict[str, dict[str, Any]] = {}
        for p in self.board.edge_ports():
            c = bm @ np.array([*p.center, 1.0])
            if p.edge in ("+x", "-x"):
                depth = self._il / 2 + self.spec.wall - math.copysign(1.0, p.direction[0]) * c[0]
            else:
                depth = self._iw / 2 + self.spec.wall - math.copysign(1.0, p.direction[1]) * c[1]
            out[p.name] = {"depth": float(depth), "cut": p.name in cut, "finger": p.kind in FINGERTIP,
                           "notch": p.name in cut and self._finger_notch(p)}
        return out

    def _face_features(self) -> dict[str, list[tuple[str, Any]]]:
        """2D footprints (shapely polygons, face-local u/v) of the openings on every face."""
        from shapely.geometry import box as sbox

        s = self.spec
        out: dict[str, list[tuple[str, Any]]] = {}
        for i, v in enumerate(s.vents):
            (w, h), (u0, v0) = v.size, v.offset
            out.setdefault(_norm_face(v.face), []).append(
                (f"vent {i} ({v.style})", sbox(u0 - w / 2, v0 - h / 2, u0 + w / 2, v0 + h / 2)))
        for face, rects in self._wall_port_rects().items():
            for r in rects:
                out.setdefault(face, []).append(
                    ("port " + "+".join(r["ports"]), sbox(r["u0"], r["v0"], r["u1"], r["v1"])))
        bm = location_to_matrix(self.board_location)
        inv = np.linalg.inv(self._face_matrix("top"))
        for p in self._top_ports:
            u, v = (inv @ (bm @ np.array([*p.center, 1.0])))[:2]
            w, h = self._port_cut_size(p)
            out.setdefault("top", []).append((f"port {p.name}", sbox(u - w / 2, v - h / 2, u + w / 2, v + h / 2)))
        for it in self._items:
            m, rot = it.module, float(it.spec.rotation)
            if m.panel_mount == "surface":
                continue
            for j, (kind, (wx, wy), size) in enumerate(m.windows):
                w, h = (size[0], size[0]) if kind == "circle" else size
                pts = [_rot2(wx + dx, wy + dy, rot) for dx in (-w / 2 - 0.3, w / 2 + 0.3) for dy in (-h / 2 - 0.3, h / 2 + 0.3)]
                us = [x + it.spec.offset[0] for x, _ in pts]
                vs = [y + it.spec.offset[1] for _, y in pts]
                out.setdefault(it.face, []).append(
                    (f"panel {it.index} {m.key} window {j}", sbox(min(us), min(vs), max(us), max(vs))))
        lab = self._label_local()
        if lab is not None:
            bb = lab[1].bounding_box()
            out.setdefault(lab[0], []).append(("label", sbox(bb.min.X, bb.min.Y, bb.max.X, bb.max.Y)))
        return out

    # -- checks ---------------------------------------------------------------------------------
    def checks(self) -> Report:
        """Port access, interference, wall thickness, standoff clearance, screw engagement."""
        from piforge.analysis.access import check_port_access

        s, b, pr = self.spec, self.board, self.printer
        rep = Report(title=f"enclosure:{b.key}")
        for code, sev, msg, subject, hint, data in self._feature_findings:
            rep.add(code, sev, msg, subject, hint=hint, **data)
        thin = [(n, v) for n, v in (("wall", s.wall), ("floor", s.floor), ("lid", s.lid_thickness)) if v < pr.min_wall]
        for n, v in thin:
            rep.add("ENCL.WALL_THIN", Severity.ERROR, f"The {n} is {v:g} mm; {pr.name} needs ≥ {pr.min_wall:g} mm.",
                    f"enclosure:{n}", hint=f"Make the {n} at least {pr.min_wall:g} mm (two perimeters).",
                    thickness=v, min_wall=pr.min_wall)
        if not thin:
            rep.add("ENCL.WALLS", Severity.INFO,
                    f"Wall {s.wall:g} mm, floor {s.floor:g} mm, lid {s.lid_thickness:g} mm (≥ {pr.min_wall:g} mm).",
                    "enclosure", wall=s.wall, floor=s.floor, lid=s.lid_thickness)
        # standoffs vs the board's bottom-side parts
        margin = s.standoff_height - b.bottom_clearance
        if margin < 0:
            rep.add("ENCL.STANDOFF_CLEARANCE", Severity.ERROR,
                    f"Standoffs ({s.standoff_height:g} mm) are lower than the parts under the board "
                    f"({b.bottom_clearance:g} mm): it will not sit flat.", "enclosure:standoffs",
                    hint=f"Use standoff_height ≥ {b.bottom_clearance + 0.5:g} mm.",
                    standoff_height=s.standoff_height, bottom_clearance=b.bottom_clearance)
        elif margin < 0.5:
            rep.add("ENCL.STANDOFF_CLEARANCE", Severity.WARNING,
                    f"Only {margin:.1f} mm between the floor and the parts under the board.", "enclosure:standoffs",
                    hint="Raise standoff_height by 1 mm.", standoff_height=s.standoff_height,
                    bottom_clearance=b.bottom_clearance)
        else:
            rep.add("ENCL.STANDOFF_CLEARANCE", Severity.INFO,
                    f"Standoffs {s.standoff_height:g} mm leave {margin:.1f} mm under the board's bottom parts.",
                    "enclosure:standoffs", standoff_height=s.standoff_height, bottom_clearance=b.bottom_clearance)
        bs = get_size(s.board_screw)
        od = max(2 * bs.d + 1.0, bs.insert_hole_d + 2.4 if s.board_fastening == "insert" else 0.0)
        if od > b.pad_d + 1e-9:
            rep.add("ENCL.STANDOFF_PAD", Severity.WARNING,
                    f"Standoff Ø{od:.1f} mm is wider than the board's {b.pad_d:g} mm keep-out pad.",
                    "enclosure:standoffs", hint="Use a smaller board screw or tap fastening.", od=od, pad_d=b.pad_d)
        L = self.board_screw_length()
        eng = L - b.thickness
        avail = s.standoff_height + (min(bs.insert_depth + 1.0 - s.standoff_height, s.floor - 0.8)
                                     if s.board_fastening == "insert" else 0.0)
        if s.board_fastening == "insert" and avail < bs.insert_depth:
            rep.add("ENCL.BOARD_SCREW", Severity.WARNING,
                    f"Insert pockets are {avail:.1f} mm deep but {s.board_screw} inserts are {bs.insert_depth:g} mm long.",
                    "enclosure:standoffs", hint="Raise standoff_height or the floor thickness.",
                    pocket_mm=avail, insert_mm=bs.insert_depth)
        else:
            sev = Severity.INFO if eng >= 3.0 else Severity.WARNING
            rep.add("ENCL.BOARD_SCREW", sev,
                    f"Board screws {s.board_screw}×{L} engage {eng:.1f} mm in the standoffs ({s.board_fastening}).",
                    "enclosure:standoffs", hint="" if sev == Severity.INFO else "Use taller standoffs.",
                    length=L, engagement_mm=eng)
        if self._screws:
            L = self._lid_screw_length()
            eng = L - s.lid_thickness
            depth = min(eng + 1.5, self._ih - 0.6)
            ok = eng >= MIN_ENGAGEMENT and depth >= eng
            rep.add("ENCL.LID_SCREW", Severity.INFO if ok else Severity.ERROR,
                    f"Lid screws {s.lid_screw}×{L} engage {eng:.1f} mm in {depth:.1f} mm deep pilot holes "
                    f"(need ≥ {MIN_ENGAGEMENT:g} mm).", "enclosure:lid",
                    hint="" if ok else "Raise inner_height or use thinner lid / longer screws.",
                    length=L, engagement_mm=eng, hole_depth_mm=depth)
        else:
            sev = Severity.INFO if self._lip_h >= 2.0 else Severity.WARNING
            rep.add("ENCL.SNAP", sev,
                    f"Snap lid: {self._lip_h:.1f} mm lip with 0.5 mm bumps in matching 45° grooves.",
                    "enclosure:lid", lip_mm=self._lip_h)
        if self._ih < self._stack - 1e-6:
            rep.add("ENCL.TOO_LOW", Severity.ERROR,
                    f"Cavity {self._ih:.1f} mm is lower than the board stack {self._stack:.1f} mm.",
                    "enclosure", hint="Increase inner_height (or leave it None).",
                    inner_height=self._ih, stack=self._stack)
        # ventilation
        fans = [it for it in self._items if it.module.key.startswith("fan")]
        if b.key in HOT_BOARDS and not s.vents and not fans:
            rep.add("ENCL.NO_VENTS", Severity.WARNING,
                    f"A closed box around a {b.name} has no vents or fan: it will throttle under sustained load.",
                    "enclosure:vents", hint="Add VentSpec(...) on the top and a side wall, or a fan_30mm / fan_40mm "
                                            "panel item.", board=b.key)
        # openings that overlap on a face
        for face, feats in self._face_features().items():
            for i in range(len(feats)):
                for j in range(i + 1, len(feats)):
                    (na, a), (nb, bb) = feats[i], feats[j]
                    if na.split(" window")[0] == nb.split(" window")[0] and "window" in na:
                        continue  # windows of one module
                    area = a.intersection(bb).area
                    if area > 1e-3:
                        rep.add("ENCL.FEATURE_OVERLAP", Severity.WARNING,
                                f"On the {face} face, {na} and {nb} overlap ({area:.1f} mm²).", f"enclosure:{face}",
                                hint="Move one of them (offset) or shrink it; overlapping cutouts leave thin "
                                     "slivers and weaken the face.", face=face, features=[na, nb],
                                area_mm2=round(area, 3))
        # port access (selected edge ports, or all of them for ports=None / ports=(), + top ports)
        asm = self.assembly(include_screws=False)
        lid_shape = asm.world_shape("lid")
        obstacles: list[Any] = [("enclosure_base", self.base.shape), ("enclosure_lid", lid_shape)]
        for it in self._items:
            obstacles.append((f"module {it.module.key}", it.module.shape().moved(it.loc)))
        excluded = {p.name for p in self.excluded_ports}
        for p in self.excluded_ports:
            rep.add("ENCL.PORT_EXCLUDED", Severity.INFO,
                    f"Port {p.name} ({p.kind}) is not in ports=…: no cutout, the wall covers it.", f"port:{p.name}",
                    port=p.name)
        names = [p.name for p in b.edge_ports() if p.name not in excluded] + [p.name for p in self._top_ports]
        rep.extend(check_port_access(b, self.board_location, obstacles, ports=names))
        self._check_port_depths(rep, obstacles)
        rep.extend(asm.check_interference(kinds={"printed", "pcb", "reference"}))
        return rep

    def _check_port_depths(self, rep: Report, obstacles: list[Any]) -> None:
        """``ENCL.PORT_DEPTH`` (INFO) per cut wall port; ``ENCL.PORT_RECESS`` (WARNING) when a plug
        mouth sits > PORT_RECESS_MAX behind the outer face or a fingertip cannot get within
        FINGER_REACH of a card slot / button."""
        from piforge.analysis.access import finger_gap

        b = self.board
        for name, d in self.port_depths().items():
            if not d["cut"]:
                continue
            p = b.port(name)
            data = dict(d, port=name, kind=p.kind, depth=round(d["depth"], 3))
            if d["finger"]:
                gap = finger_gap(b, p, self.board_location, obstacles)
                data["finger_gap"] = round(gap, 2)
                bad = gap > FINGER_REACH + 1e-6
                msg = (f"A fingertip gets within {gap:.1f} mm of the {name} ({p.kind}) mouth "
                       f"(mouth {d['depth']:.1f} mm behind the outer face{', fingertip notch' if d['notch'] else ''}).")
                hint = ("Keep finger_notches=True, reduce side_clearance, or reach it with a tool."
                        if bad else "")
            else:
                bad = d["depth"] > PORT_RECESS_MAX + 1e-6
                msg = f"The {name} ({p.kind}) mouth is {d['depth']:.1f} mm behind the outer face."
                hint = "Reduce side_clearance / extra_space on that side; short plugs may not seat." if bad else ""
            if bad:
                rep.add("ENCL.PORT_RECESS", Severity.WARNING, msg + " That is too deep.", f"port:{name}",
                        hint=hint, **data)
            else:
                rep.add("ENCL.PORT_DEPTH", Severity.INFO, msg, f"port:{name}", **data)

    def print_checks(self, *, wall_samples: int = 800) -> Report:
        """Printability of base and lid in their print orientation (:func:`piforge.fab.analyze_mesh`)."""
        from piforge.fab.analyze import analyze_mesh
        from piforge.mech.export import to_trimesh

        reps = []
        for part in self.parts:
            res = analyze_mesh(to_trimesh(part.shape), self.printer, self.spec.material, name=part.name,
                               rotation=part.print_rotation, wall_samples=wall_samples)
            reps.append(res.report)
        return Report.merge(*reps, title=f"print:enclosure_{self.board.key}")


__all__ = ["FACES", "FINGERTIP", "FINGER_REACH", "Enclosure", "EnclosureSpec", "PanelItem", "VentSpec"]
