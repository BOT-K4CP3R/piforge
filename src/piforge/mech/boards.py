"""Raspberry Pi board models: outline, mounting holes, connectors (ports) and tall components.

Board frame (binding for every consumer): origin at the PCB's lower-left corner seen from the top
with the 40-pin GPIO header along the TOP long edge; x along the long edge (85 mm on a Pi 4),
y along the short edge, z = 0 at the PCB bottom face, PCB top at z = ``thickness``.

All numbers live in :mod:`piforge.mech._board_data` with ``# src:`` comments (official
Raspberry Pi mechanical drawings, cross-checked with NopSCADlib). Data access is kernel-free;
only :meth:`BoardModel.shape` imports build123d.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from piforge.core.errors import NotFoundError, ValidationError
from piforge.mech._board_data import BOARD_DATA
from piforge.mech.anchors import PinAnchor
from piforge.mech.part import PartSpec

if TYPE_CHECKING:  # pragma: no cover
    from build123d import Compound

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]

_EDGE_DIRS: dict[str, Vec3] = {"-x": (-1.0, 0.0, 0.0), "+x": (1.0, 0.0, 0.0),
                               "-y": (0.0, -1.0, 0.0), "+y": (0.0, 1.0, 0.0), "+z": (0.0, 0.0, 1.0)}
_EPS = 0.01

# colours for the reference model (PCB green, connector metal, plastic, chips)
_COLORS = {"pcb": "#1d6b3a", "metal": "#c9ccd1", "dark": "#2b2b2b", "plastic": "#e8e4d8",
           "chip": "#3a3a3a", "header": "#202020"}
_KIND_COLOR = {"usb-c": "metal", "micro-hdmi": "metal", "mini-hdmi": "metal", "hdmi": "metal",
               "micro-usb": "metal", "usb-a-dual": "metal", "rj45": "metal", "audio-jack": "dark",
               "microsd": "metal", "header": "header", "button": "dark", "fpc": "plastic"}


@dataclass(frozen=True)
class Port:
    """A connector (or user-accessible feature) on the board, in the board frame (mm).

    ``center`` is the centre of the connector mouth (where a plug enters), ``direction`` the outward
    unit vector. ``opening`` is the connector face outline (width along the edge — or along x for
    top ports — and height) WITHOUT clearance; ``plug`` the envelope (w, h, depth) of a typical
    mating plug/card/finger extending ``depth`` outwards from the mouth. ``body_center`` and
    ``body_size`` describe the connector body as an axis-aligned box.
    """

    name: str
    kind: str
    center: Vec3
    direction: Vec3
    opening: Vec2
    plug: Vec3
    body_center: Vec3
    body_size: Vec3
    edge: str = ""  # "-x" | "+x" | "-y" | "+y" | "+z"
    side: str = "top"  # PCB side the part is mounted on

    @property
    def horizontal(self) -> bool:
        """True for edge connectors (direction in the board plane)."""
        return abs(self.direction[2]) < 1e-9


@dataclass(frozen=True)
class BoardModel:
    """Mechanical model of a board; see the module docstring for the frame convention."""

    key: str
    name: str
    length: float
    width: float
    thickness: float
    corner_radius: float
    holes: tuple[Vec2, ...]
    hole_d: float
    ports: tuple[Port, ...]
    components: tuple[tuple[str, Vec3, Vec3], ...]  # (name, centre, size) boxes, board frame
    bottom_clearance: float  # tallest bottom-side feature, mm below the PCB bottom face
    top_height: float  # tallest top-side feature, mm above the PCB top face
    source: str
    pad_d: float = 6.0  # keep-out pad around the mounting holes (standoff OD limit)
    extra_holes: tuple[tuple[float, float, float], ...] = ()  # (x, y, d) non-mounting holes
    notes: str = ""
    aliases: tuple[str, ...] = field(default=(), repr=False)

    def port(self, name: str) -> Port:
        """Return the port called ``name`` (case-insensitive); NotFoundError lists close names."""
        key = str(name).strip().lower()
        for p in self.ports:
            if p.name == key:
                return p
        raise NotFoundError(f"port on {self.key}", name, [p.name for p in self.ports])

    @property
    def port_names(self) -> tuple[str, ...]:
        """Names of all ports, in table order."""
        return tuple(p.name for p in self.ports)

    def edge_ports(self) -> tuple[Port, ...]:
        """Ports that open through a side wall (horizontal direction)."""
        return tuple(p for p in self.ports if p.horizontal)

    @property
    def size(self) -> Vec3:
        """PCB size (length, width, thickness) in mm."""
        return (self.length, self.width, self.thickness)

    def shape(self, *, components: bool = True) -> "Compound":
        """Reference 3D model in the board frame: PCB with holes (+ connector/component boxes).

        Children carry labels and colours (PCB green, connectors metal, chips dark) for GLB export.
        """
        cache_key = (self, components)  # frozen + hashable: variants made with replace() get their own
        cached = _SHAPE_CACHE.get(cache_key)
        if cached is None:
            cached = _build_shape(self, components)
            _SHAPE_CACHE[cache_key] = cached
        return copy.copy(cached)

    def part(self, name: str | None = None) -> PartSpec:
        """The board as a reference :class:`PartSpec` (kind ``pcb``) ready for an Assembly."""
        return PartSpec(name or self.name, self.shape(), kind="pcb", material="FR4", color=_COLORS["pcb"],
                        meta={"board": self.key})

    @property
    def anchors(self) -> tuple[PinAnchor, ...]:
        """Wire anchors (board frame) of the 40-pin GPIO header: one per pin, ``number`` 1…40.

        Standard 2 × 20 header at 2.54 mm pitch centred on the ``gpio`` port: pin 1 at the SD-card
        end (−x) on the inner row (towards the board centre), even pins on the board-edge row; the
        anchor is the pin tip (top of the header), exit +Z (a DuPont housing slides on from above).
        Pin names/labels come from the board's electrical definition (``piforge.elec``).
        """
        return _header_anchors(self.key)


_SHAPE_CACHE: dict[tuple[BoardModel, bool], Any] = {}
_ANCHOR_CACHE: dict[str, tuple[PinAnchor, ...]] = {}
HEADER_PITCH = 2.54  # src: 0.1 in pin pitch of the Raspberry Pi 2 × 20 GPIO header (Pi mechanical drawings)
_LABEL_FUNCS = ("SPI", "I2C", "UART", "PWM", "PCM", "GPCLK")


def _pin_label(name: str, functions: tuple[str, ...]) -> str:
    for fam in _LABEL_FUNCS:
        hit = next((f for f in functions if f.startswith(fam) and "_" in f), None)
        if hit:
            bus, line = hit.split("_", 1)
            return f"{name} / {bus} {line.replace('_N', '').replace('_', ' ')}"
    return name


def _header_anchors(key: str) -> tuple[PinAnchor, ...]:
    cached = _ANCHOR_CACHE.get(key)
    if cached is not None:
        return cached
    board = get_board(key)
    port = board.port("gpio")
    cx, cy, top = port.center
    names: dict[str, tuple[str, tuple[str, ...]]] = {}
    try:
        from piforge.elec.library import get_def  # kernel-free

        names = {p.number: (p.name, tuple(p.functions)) for p in get_def(board.key).pins}
    except Exception:  # noqa: BLE001 - a board without an electrical definition still gets numbered anchors
        names = {}
    out = []
    for n in range(1, 41):
        col, row = (n - 1) // 2, (n - 1) % 2  # row 0 = odd pins = inner row, row 1 = even pins = edge row
        x = cx + (col - 9.5) * HEADER_PITCH
        y = cy + (row - 0.5) * HEADER_PITCH
        name, funcs = names.get(str(n), (f"P{n}", ()))
        out.append(PinAnchor(pin=name, number=str(n), pos=(x, y, top), dir=(0.0, 0.0, 1.0), kind="dupont",
                             label=_pin_label(name, funcs)))
    _ANCHOR_CACHE[key] = tuple(out)
    return _ANCHOR_CACHE[key]


def _build_shape(board: BoardModel, with_components: bool) -> "Compound":
    import build123d as bd

    t = board.thickness
    pcb = bd.extrude(bd.RectangleRounded(board.length, board.width, board.corner_radius,
                                         align=(bd.Align.MIN, bd.Align.MIN)), t)
    cutters = [bd.Cylinder(board.hole_d / 2, t + 2 * _EPS).moved(bd.Location((x, y, t / 2)))
               for x, y in board.holes]
    cutters += [bd.Cylinder(d / 2, t + 2 * _EPS).moved(bd.Location((x, y, t / 2)))
                for x, y, d in board.extra_holes]
    for c in cutters:
        pcb = pcb - c
    pcb = bd.Part(pcb.wrapped)
    pcb.label, pcb.color = "pcb", bd.Color(_COLORS["pcb"])
    children = [pcb]
    if with_components:
        for p in board.ports:
            box = bd.Box(*p.body_size).moved(bd.Location(p.body_center))
            box.label = f"port:{p.name}"
            box.color = bd.Color(_COLORS[_KIND_COLOR.get(p.kind, "metal")])
            children.append(box)
        for name, center, size in board.components:
            box = bd.Box(*size).moved(bd.Location(center))
            box.label = name
            colour = "header" if "header" in name else "plastic" if name in (
                "csi", "dsi", "pcie_fpc", "mipi0", "mipi1", "uart", "fan_connector") else "chip"
            box.color = bd.Color(_COLORS[colour])
            children.append(box)
    return bd.Compound(children=children, label=board.name)


# ----------------------------------------------------------------------------------------------
# building BoardModel objects from the raw tables
# ----------------------------------------------------------------------------------------------
def _port_from_record(rec: dict[str, Any], board: dict[str, Any]) -> Port:
    t = float(board["thickness"])
    length, width = float(board["length"]), float(board["width"])
    edge = rec["edge"]
    if edge not in _EDGE_DIRS:
        raise ValidationError(f"{board['key']}.{rec['name']}: bad edge {edge!r}")
    direction = _EDGE_DIRS[edge]
    side = rec.get("side", "top")
    bw, bdepth, bh = (float(v) for v in rec["body"])
    if edge == "+z":
        x, y = (float(v) for v in rec["pos"])
        top = t + float(rec["z"])
        center = (x, y, top)
        body_center = (x, y, t + bh / 2)
        body_size = (bw, bdepth, bh)
    else:
        pos, mouth, z = float(rec["pos"]), float(rec["mouth"]), float(rec["z"])
        zc = t + z if side == "top" else -z
        zb = t + bh / 2 if side == "top" else -bh / 2
        if edge in ("-y", "+y"):
            plane = 0.0 if edge == "-y" else width
            y_mouth = plane - mouth if edge == "-y" else plane + mouth
            center = (pos, y_mouth, zc)
            y_body = y_mouth + bdepth / 2 if edge == "-y" else y_mouth - bdepth / 2
            body_center, body_size = (pos, y_body, zb), (bw, bdepth, bh)
        else:
            plane = 0.0 if edge == "-x" else length
            x_mouth = plane - mouth if edge == "-x" else plane + mouth
            center = (x_mouth, pos, zc)
            x_body = x_mouth + bdepth / 2 if edge == "-x" else x_mouth - bdepth / 2
            body_center, body_size = (x_body, pos, zb), (bdepth, bw, bh)
    return Port(name=rec["name"], kind=rec["kind"], center=center, direction=direction,
                opening=tuple(float(v) for v in rec["opening"]),  # type: ignore[arg-type]
                plug=tuple(float(v) for v in rec["plug"]),  # type: ignore[arg-type]
                body_center=body_center, body_size=body_size, edge=edge, side=side)


def _board_from_record(rec: dict[str, Any]) -> BoardModel:
    t = float(rec["thickness"])
    ports = tuple(_port_from_record(p, rec) for p in rec["ports"])
    comps = []
    for name, (x, y), (sx, sy, h), side in rec["components"]:
        zc = t + h / 2 if side == "top" else -h / 2
        comps.append((name, (float(x), float(y), zc), (float(sx), float(sy), float(h))))
    tops = [p.body_center[2] + p.body_size[2] / 2 - t for p in ports]
    tops += [c[1][2] + c[2][2] / 2 - t for c in comps]
    bottoms = [-(p.body_center[2] - p.body_size[2] / 2) for p in ports]
    bottoms += [-(c[1][2] - c[2][2] / 2) for c in comps]
    return BoardModel(
        key=rec["key"], name=rec["name"], length=float(rec["length"]), width=float(rec["width"]),
        thickness=t, corner_radius=float(rec["corner_radius"]),
        holes=tuple((float(x), float(y)) for x, y in rec["holes"]), hole_d=float(rec["hole_d"]),
        ports=ports, components=tuple(comps),
        bottom_clearance=max([float(rec["bottom_clearance"]), *bottoms]),
        top_height=round(max(tops), 6), source=rec["source"], pad_d=float(rec["pad_d"]),
        extra_holes=tuple(tuple(float(v) for v in h) for h in rec["extra_holes"]),  # type: ignore[misc]
        notes=rec.get("notes", ""), aliases=tuple(rec.get("aliases", ())))


BOARDS: dict[str, BoardModel] = {k: _board_from_record(v) for k, v in BOARD_DATA.items()}


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-.]+", "", str(name).strip().lower()).replace("plus", "+")


_ALIASES: dict[str, str] = {}
for _b in BOARDS.values():
    for _a in (_b.key, _b.name, *_b.aliases):
        _ALIASES[_norm(_a)] = _b.key


def get_board(key: "str | BoardModel") -> BoardModel:
    """Look up a board by key (``rpi5``, ``rpi4b``, ``rpi3bp``, ``rpizero2w``) or a common alias."""
    if isinstance(key, BoardModel):
        return key
    k = _ALIASES.get(_norm(key))
    if k is None:
        raise NotFoundError("board", key, BOARDS)
    return BOARDS[k]


def _check_tables() -> None:
    """Sanity-check the raw data at import (catches typos in _board_data)."""
    for b in BOARDS.values():
        for p in b.ports:
            if not math.isclose(math.hypot(*p.direction), 1.0):
                raise ValidationError(f"{b.key}.{p.name}: direction must be a unit vector")
        for x, y in b.holes:
            if not (0 < x < b.length and 0 < y < b.width):
                raise ValidationError(f"{b.key}: hole ({x}, {y}) outside the PCB")


_check_tables()
