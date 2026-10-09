"""3D wiring harness: wires from pin anchor to pin anchor, routed through the assembly.

``route_harness(circuit, assembly, anchors_by_ref, channels=…)`` turns the circuit's nets into
physical wires between the :class:`~piforge.mech.anchors.PinAnchor` contacts of placed parts:

1. **Anchors.** ``anchors_by_ref`` maps every circuit ref to ``(assembly node id, anchors in that
   node's part frame)`` — :attr:`BoardModel.anchors` (40-pin header), :attr:`ModuleModel.anchors`
   (ULN2003, 28BYJ-48 lead, DC jack, A3144…), :meth:`SplitFlapModule.hall_anchors`, or a
   :class:`Perfboard` layout (labelled pads and headers on a protoboard). A pin of a ref matches the
   anchor with the same physical ``number`` (when both have one), else the same pin name.
2. **Topology.** Per net, the same star as ``elec/wiring.md`` (:func:`piforge.elec.wiring.wiring_table`):
   each pin is wired from the hub (the board's header pin, or the nearest pin along the explicit
   ``connect()`` groups, or the pin a part ties internally — a motor's COM to its driver's M+).
   ``hubs={"5V": "U2"}`` moves a net's hub to another part (a perfboard rail),
   ``chains=[(("5V", "GND"), ["H1", …, "H8"])]`` daisy-chains those parts (a sensor bus). Pins on the
   same node (a perfboard's chips, a board's two 5 V pins) are joined on the board: no wire.
3. **Cables.** Wires between the same two nodes form one cable (a DuPont ribbon, a motor's own lead);
   its wires leave their contacts straight (``STUB_MM``: housing / sleeve), gather into a bundle
   and follow one centreline.
4. **Routing.** The centreline is orthogonal: a direct Manhattan path (for runs up to ``direct_max``)
   or a drop into the nearest declared :class:`Channel` (a trunk along the housing — tie anchors, a
   rear channel), along the channel network, and a drop out again; every candidate leg is tested
   against the solid parts and the shortest clear route wins. Inside a channel each wire gets its own
   slot of the channel's cross-section (``width`` × ``height``, ``stack`` from the centre or the
   bottom), so parallel cables form one tidy loom.
5. **Geometry.** Each wire becomes a tube (Ø by AWG, bends of ``bend_radius``) plus its connector
   housing, a ``kind="wire"`` node in the world frame (:meth:`Harness.add_to`).

Colours: 5 V red, GND black, 3V3 orange, other rails brown, SPI MOSI blue / SCLK yellow / CE green /
MISO purple, I2C SDA blue / SCL yellow, others from a fixed palette; a part's own lead keeps its
factory colours; ``colors={key: name}`` overrides by net name, ``REF.PIN`` or pin name.
Lengths: routed length + ``slack`` (fraction) + ``slack_mm`` service loop; factory leads keep their
length (``WIRE.LEAD_TOO_SHORT`` when the route needs more).

Findings (:meth:`Harness.checks`): ``WIRE.SUMMARY``, ``WIRE.UNROUTED``, ``WIRE.NO_ANCHOR``,
``WIRE.COLLISION`` (a wire runs through a solid part), ``WIRE.TOO_LONG``, ``WIRE.LEAD_TOO_SHORT``,
``WIRE.LEAD_SLACK``, ``WIRE.CHANNEL_FULL``.
"""

from __future__ import annotations

import csv
import fnmatch
import heapq
import io
import itertools
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import numpy as np

from piforge.core.errors import NotFoundError, ValidationError
from piforge.core.report import Report, Severity
from piforge.mech.anchors import ANCHOR_KINDS, STUB_MM, PinAnchor
from piforge.mech.part import PartSpec

if TYPE_CHECKING:  # pragma: no cover
    from piforge.elec.model import Circuit, Net, PinRef
    from piforge.mech.assembly import Assembly

log = logging.getLogger(__name__)

__all__ = ["AWG_OD", "COLOR_HEX", "Channel", "Harness", "Perfboard", "PinAnchor", "STUB_MM", "Wire",
           "WireEnd", "route_harness", "ANCHOR_KINDS"]

Vec3 = tuple[float, float, float]

COLOR_HEX = {
    "red": "#d62a1e", "black": "#1c1c1c", "orange": "#f07d14", "brown": "#7a4a22", "blue": "#1f5fd1",
    "yellow": "#f2c40c", "green": "#21a043", "purple": "#8a3fb8", "pink": "#ef6fae", "cyan": "#18b3c9",
    "grey": "#8d9196", "white": "#eeeeea", "olive": "#7f8a2a", "lime": "#9bd12a", "violet": "#8a3fb8",
}
AWG_OD = {  # src: typical UL1007 PVC hook-up wire outer diameters (mm)
    18: 2.0, 20: 1.8, 22: 1.6, 24: 1.4, 26: 1.2, 28: 1.0,
}
HOUSING_COLOR = "#161616"
_SPI_COLORS = {"MOSI": "blue", "SCLK": "yellow", "MISO": "purple", "CE": "green", "CS": "green"}
_I2C_COLORS = {"SDA": "blue", "SCL": "yellow"}
_UART_COLORS = {"TX": "green", "RX": "white"}
_PALETTE = ("green", "purple", "pink", "cyan", "grey", "white", "olive", "lime")
_ORDERS = tuple(itertools.permutations((0, 1, 2)))
_AXES = "xyz"


# ---------------------------------------------------------------------------------------------
# small vector helpers (plain floats; the harness has a few hundred points)
# ---------------------------------------------------------------------------------------------
def _v(p: Iterable[float]) -> np.ndarray:
    return np.asarray(list(p), dtype=float)


def _t(p: np.ndarray) -> Vec3:
    return (float(p[0]), float(p[1]), float(p[2]))


def _manhattan(p: np.ndarray, q: np.ndarray, order: Sequence[int]) -> list[np.ndarray]:
    """Axis-parallel path p → q moving along the axes in ``order`` (zero legs skipped)."""
    pts = [p.copy()]
    cur = p.copy()
    for ax in order:
        if abs(q[ax] - cur[ax]) > 1e-9:
            cur = cur.copy()
            cur[ax] = q[ax]
            pts.append(cur)
    if np.linalg.norm(pts[-1] - q) > 1e-9:
        pts.append(q.copy())
    return pts


def _length(pts: Sequence[np.ndarray]) -> float:
    return float(sum(np.linalg.norm(b - a) for a, b in zip(pts[:-1], pts[1:])))


def _clean(pts: Sequence[np.ndarray], tol: float = 1e-6) -> list[np.ndarray]:
    """Drop repeated and collinear interior points."""
    out: list[np.ndarray] = []
    for p in pts:
        if out and np.linalg.norm(p - out[-1]) < tol:
            continue
        out.append(np.asarray(p, dtype=float))
    changed = True
    while changed and len(out) > 2:
        changed = False
        for i in range(1, len(out) - 1):
            a, b, c = out[i - 1], out[i], out[i + 1]
            d1, d2 = b - a, c - b
            n1, n2 = np.linalg.norm(d1), np.linalg.norm(d2)
            if n1 < tol or n2 < tol or np.linalg.norm(np.cross(d1 / n1, d2 / n2)) < 1e-9 and np.dot(d1, d2) > 0:
                del out[i]
                changed = True
                break
    return out


def _dominant(d: Sequence[float]) -> int:
    return int(np.argmax(np.abs(np.asarray(d, dtype=float))))


def _perp(d: Sequence[float]) -> Vec3:
    """A unit vector perpendicular to ``d`` (prefers −Z, then +X)."""
    d = np.asarray(d, dtype=float)
    for c in ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        c = np.asarray(c)
        v = c - np.dot(c, d) * d
        if np.linalg.norm(v) > 0.3:
            return _t(v / np.linalg.norm(v))
    return (1.0, 0.0, 0.0)


# ---------------------------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Channel:
    """A straight, axis-parallel cable run (a trunk on tie anchors, a rear channel, a riser).

    ``start``/``end`` is the bundle centreline (world mm). The cross-section offers ``width`` mm
    across the first perpendicular axis (y for an x-channel, x for y/z-channels) and ``height`` mm
    across the second (z, or y for a z-channel); ``stack="bottom"`` fills the second axis from its
    low side (a loom lying on tie anchors), ``"center"`` around the centreline. Channels whose
    centrelines cross (or touch end-to-segment) are joined automatically. ``only`` restricts the
    channel to cables whose name matches one of the patterns (``("W-DRV*",)``).
    """

    name: str
    start: Vec3
    end: Vec3
    width: float = 10.0
    height: float = 10.0
    stack: str = "center"
    only: tuple[str, ...] = ()  # cable-name patterns (fnmatch) allowed in this channel; () = every cable

    def allows(self, cable: str | None) -> bool:
        return not self.only or any(fnmatch.fnmatchcase(cable or "", pat) for pat in self.only)

    def __post_init__(self) -> None:
        object.__setattr__(self, "only", (self.only,) if isinstance(self.only, str) else tuple(self.only))
        a, b = _v(self.start), _v(self.end)
        d = b - a
        if np.linalg.norm(d) < 1.0:
            raise ValidationError(f"Channel {self.name!r}: start and end must be at least 1 mm apart")
        if sum(abs(x) > 1e-6 for x in d) != 1:
            raise ValidationError(f"Channel {self.name!r} must be parallel to the X, Y or Z axis")
        if self.stack not in ("center", "bottom"):
            raise ValidationError(f"Channel {self.name!r}: stack must be 'center' or 'bottom'")
        if self.width <= 0 or self.height <= 0:
            raise ValidationError(f"Channel {self.name!r}: width and height must be > 0")
        object.__setattr__(self, "start", _t(a))
        object.__setattr__(self, "end", _t(b))

    @property
    def axis(self) -> int:
        return _dominant(_v(self.end) - _v(self.start))

    @property
    def perp_axes(self) -> tuple[int, int]:
        return {0: (1, 2), 1: (0, 2), 2: (0, 1)}[self.axis]

    @property
    def length(self) -> float:
        return float(np.linalg.norm(_v(self.end) - _v(self.start)))

    def point(self, t: float) -> np.ndarray:
        a, b = _v(self.start), _v(self.end)
        return a + (b - a) * t

    def project(self, p: np.ndarray) -> tuple[float, np.ndarray]:
        """(t ∈ [0, 1], closest centreline point) for ``p``."""
        a, b = _v(self.start), _v(self.end)
        d = b - a
        t = float(np.clip(np.dot(p - a, d) / np.dot(d, d), 0.0, 1.0))
        return t, a + d * t


# ---------------------------------------------------------------------------------------------
# perfboard layout
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _PadGroup:
    name: str
    col: int
    row: int
    pins: tuple
    along: str
    kind: str


class Perfboard:
    """A 2.54 mm protoboard layout: named headers / pad rows bound to circuit pins (module frame).

    ``Perfboard("perfboard_50x70", ref="PB1")`` uses the module database board (centred, PCB bottom
    at z = 0, component side +Z). Column ``c`` / row ``r`` of the hole grid sit at
    ``((c − (cols − 1)/2)·2.54, (r − (rows − 1)/2)·2.54)``. :meth:`header` places a male pin header
    (anchors on the pin tips, DuPont from above), :meth:`pads` bare solder pads; each pin entry is
    ``(ref, pin)`` or ``(ref, pin, peer)`` — the circuit pin the pad is soldered to and, for rail
    pads, the ref the wire from this pad goes to — or ``None`` for an unused position.
    """

    def __init__(self, module: str = "perfboard_50x70", *, ref: str = "PB1", pitch: float = 2.54):
        from piforge.mech.modules import get_module

        self.model = get_module(module)
        if self.model.pcb is None:
            raise ValidationError(f"Perfboard needs a module with a PCB, {module!r} has none")
        self.ref = str(ref)
        self.pitch = float(pitch)  # src: 0.1 in protoboard hole pitch
        L, W, T = self.model.pcb
        self.cols, self.rows, self.thickness = int(L // self.pitch), int(W // self.pitch), T
        self.groups: list[_PadGroup] = []

    def xy(self, col: int, row: int) -> tuple[float, float]:
        if not (0 <= col < self.cols and 0 <= row < self.rows):
            raise ValidationError(f"perfboard hole ({col}, {row}) outside the {self.cols} × {self.rows} grid")
        return ((col - (self.cols - 1) / 2) * self.pitch, (row - (self.rows - 1) / 2) * self.pitch)

    def _add(self, name: str, col: int, row: int, pins: Sequence, along: str, kind: str) -> None:
        if along not in ("x", "y", "-x", "-y"):
            raise ValidationError("along must be 'x', 'y', '-x' or '-y'")
        if any(g.name == name for g in self.groups):
            raise ValidationError(f"perfboard group {name!r} already exists")
        for k in range(len(pins)):
            self.xy(*self._cell(col, row, along, k))  # bounds check
        self.groups.append(_PadGroup(name, col, row, tuple(pins), along, kind))

    @staticmethod
    def _cell(col: int, row: int, along: str, k: int) -> tuple[int, int]:
        sgn = -1 if along.startswith("-") else 1
        return (col + sgn * k, row) if along.endswith("x") else (col, row + sgn * k)

    def header(self, name: str, col: int, row: int, pins: Sequence, *, along: str = "y") -> None:
        """A 1 × n male header ``name`` starting at hole (col, row), running ``along`` the grid."""
        self._add(name, col, row, pins, along, "dupont")

    def pads(self, name: str, col: int, row: int, pins: Sequence, *, along: str = "y") -> None:
        """A row of bare solder pads (wires soldered in from the component side)."""
        self._add(name, col, row, pins, along, "solder")

    def anchors(self) -> dict[str, list[PinAnchor]]:
        """``{ref: [PinAnchor…]}`` in the perfboard's module frame (anchor ids ``PB1.J1-3``)."""
        out: dict[str, list[PinAnchor]] = {}
        for g in self.groups:
            for k, entry in enumerate(g.pins):
                if entry is None:
                    continue
                ref, pin, *rest = entry
                peer = rest[0] if rest else None
                x, y = self.xy(*self._cell(g.col, g.row, g.along, k))
                z = self.thickness + (8.5 if g.kind == "dupont" else 0.0)  # src: common 8.5 mm male header
                out.setdefault(ref, []).append(PinAnchor(
                    pin=pin, pos=(x, y, z), dir=(0.0, 0.0, 1.0), kind=g.kind, peer=peer,
                    id=f"{self.ref}.{g.name}-{k + 1}", label=f"{g.name}.{k + 1} ({ref}.{pin})"))
        return out

    def anchors_by_ref(self, node: str) -> dict[str, tuple[str, list[PinAnchor]]]:
        """``anchors_by_ref`` entries for :func:`route_harness` (every bound ref → this node)."""
        return {ref: (node, anchors) for ref, anchors in self.anchors().items()}

    def part(self, name: str = "perfboard headers") -> PartSpec | None:
        """Header bodies (black plastic + pins) in the module frame, to add as a child of the
        perfboard node (``asm.add(pb.part(), parent="perfboard")``); ``None`` without headers."""
        import build123d as bd

        solids = []
        for g in self.groups:
            if g.kind != "dupont":
                continue
            for k in range(len(g.pins)):
                x, y = self.xy(*self._cell(g.col, g.row, g.along, k))
                body = bd.Box(2.5, 2.5, 2.5).moved(bd.Location((x, y, self.thickness + 1.25)))
                body.color, body.label = bd.Color("#202020"), f"{g.name}_body"
                pin = bd.Box(0.64, 0.64, 6.0).moved(bd.Location((x, y, self.thickness + 2.5 + 3.0)))
                pin.color, pin.label = bd.Color("#c9a227"), f"{g.name}_pin"
                solids += [body, pin]
        if not solids:
            return None
        return PartSpec(name, bd.Compound(children=solids, label=name), kind="reference", material="module",
                        color="#202020", meta={"role": "perfboard headers"})


# ---------------------------------------------------------------------------------------------
# wires and the harness
# ---------------------------------------------------------------------------------------------
@dataclass
class WireEnd:
    """One end of a wire: the circuit pin (``ref``/``pin``/``number``) and the contact it lands on."""

    ref: str
    pin: str
    number: str
    label: str
    connector: str
    node: str
    anchor: PinAnchor  # world frame
    fan: int = 0       # n-th wire on this contact (offset along anchor.fan)

    def start(self, od: float) -> np.ndarray:
        fan = _v(self.anchor.fan or _perp(self.anchor.dir))
        return _v(self.anchor.pos) + fan * self.fan * (od + 0.2)

    def breakout(self, od: float) -> np.ndarray:
        return self.start(od) + _v(self.anchor.dir) * self.anchor.stub

    def to_dict(self) -> dict:
        return {"ref": self.ref, "pin": self.pin, "number": self.number, "label": self.label,
                "connector": self.connector}


@dataclass
class Wire:
    """One routed wire (world-frame polyline from contact to contact)."""

    id: str
    a: WireEnd
    b: WireEnd
    net: str
    signal: str
    color_name: str
    gauge_awg: int
    cable: str | None = None
    lead_mm: float | None = None
    points: list[Vec3] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    route_mm: float = 0.0
    length_mm: float = 0.0
    group: str = ""  # internal cable key (node pair)

    @property
    def color(self) -> str:
        return COLOR_HEX.get(self.color_name, "#9aa4b2")

    @property
    def od(self) -> float:
        return AWG_OD.get(self.gauge_awg, 1.6)

    def record(self) -> dict:
        """The scene.json ``wire`` record (contract with the web viewer)."""
        return {"id": self.id, "from": self.a.to_dict(), "to": self.b.to_dict(), "net": self.net,
                "signal": self.signal, "color_name": self.color_name, "gauge_awg": self.gauge_awg,
                "length_mm": round(self.length_mm, 1), "route_mm": round(self.route_mm, 1),
                "cable": self.cable, "channels": list(self.channels)}


class _Obstacles:
    """World shapes + bounding boxes of the solid parts, for segment-vs-part tests (OCC booleans)."""

    def __init__(self, assembly: "Assembly", skip_kinds: Iterable[str] = ("wire",)):
        skip = set(skip_kinds)
        self.ids = [n.id for n in assembly.nodes if n.part.kind not in skip]
        self.shapes = assembly._world_shapes(self.ids, {})  # noqa: SLF001 - same package
        lo, hi = [], []
        for i in self.ids:
            bb = self.shapes[i].bounding_box()
            lo.append((bb.min.X, bb.min.Y, bb.min.Z))
            hi.append((bb.max.X, bb.max.Y, bb.max.Z))
        self.lo, self.hi = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
        self._cache: dict[tuple, list[tuple[str, float]]] = {}
        self.calls = 0

    def hits(self, p: np.ndarray, q: np.ndarray, r: float, *, ignore: Iterable[str] = (),
             min_volume: float = 0.05) -> list[tuple[str, float]]:
        """Parts (id, overlap mm³) a cylinder of radius ``r`` from ``p`` to ``q`` runs through."""
        key = (tuple(np.round(p, 2)), tuple(np.round(q, 2)), round(r, 3), tuple(sorted(ignore)), min_volume)
        if key in self._cache:
            return self._cache[key]
        lo, hi = np.minimum(p, q) - r, np.maximum(p, q) + r
        mask = np.all(self.lo < hi - 1e-6, axis=1) & np.all(self.hi > lo + 1e-6, axis=1)
        cands = [self.ids[k] for k in np.nonzero(mask)[0] if self.ids[k] not in ignore]
        out: list[tuple[str, float]] = []
        if cands and np.linalg.norm(q - p) > 1e-6:
            from piforge.mech.assembly import _common_volume

            cyl = _cylinder(p, q, r)
            for c in cands:
                self.calls += 1
                try:
                    vol = _common_volume(cyl, self.shapes[c])
                except Exception:  # noqa: BLE001 - a failed boolean counts as clear
                    continue
                if vol > min_volume:
                    out.append((c, vol))
        self._cache[key] = out
        return out

    def path_clear(self, pts: Sequence[np.ndarray], r: float, *, ignore: Iterable[str] = ()) -> bool:
        return all(not self.hits(a, b, r, ignore=ignore) for a, b in zip(pts[:-1], pts[1:]))


def _cylinder(p: np.ndarray, q: np.ndarray, r: float) -> Any:
    import build123d as bd

    d = q - p
    n = float(np.linalg.norm(d))
    return bd.Solid.make_cylinder(r, n, bd.Plane(origin=_t(p), z_dir=_t(d / n)))


class Harness:
    """Routed wires + contact points; see the module docstring. Built by :func:`route_harness`."""

    def __init__(self, name: str, wires: list[Wire], connectors: list[dict], channels: Sequence[Channel], *,
                 unrouted: list[dict], no_anchor: list[dict], channel_overflow: list[dict],
                 max_length: float, min_volume: float, bend_radius: float | None,
                 obstacles: _Obstacles | None):
        self.name = name
        self.wires = wires
        self._connectors = connectors
        self.channels = tuple(channels)
        self.unrouted = unrouted
        self.no_anchor = no_anchor
        self.channel_overflow = channel_overflow
        self.max_length = max_length
        self.min_volume = min_volume
        self.bend_radius = bend_radius
        self._obstacles = obstacles
        self._collisions: list[dict] | None = None
        self._added: list[str] = []

    def __repr__(self) -> str:
        return f"Harness({self.name!r}, wires={len(self.wires)}, cables={len(self.cables())})"

    def wire(self, wid: str) -> Wire:
        for w in self.wires:
            if w.id == wid:
                return w
        raise NotFoundError("wire", wid, [w.id for w in self.wires])

    def cables(self) -> dict[str, list[Wire]]:
        """``{cable name: wires}`` for multi-wire cables and factory leads."""
        out: dict[str, list[Wire]] = {}
        for w in self.wires:
            if w.cable:
                out.setdefault(f"{w.cable}:{w.group}" if w.lead_mm else w.cable, []).append(w)
        return out

    # -- contract outputs ------------------------------------------------------------------------
    def connectors(self) -> list[dict]:
        """Every contact of every anchored part: ``{"id", "ref", "pin", "label", "node", "pos", "dir"}``
        (world mm) plus ``"used"`` and the ids of the ``"wires"`` landing on it."""
        used: dict[str, list[str]] = {}
        for w in self.wires:
            used.setdefault(w.a.connector, []).append(w.id)
            used.setdefault(w.b.connector, []).append(w.id)
        return [{**c, "used": c["id"] in used, "wires": used.get(c["id"], [])} for c in self._connectors]

    def add_to(self, assembly: "Assembly", *, prefix: str = "wire_") -> list[str]:
        """Add one ``kind="wire"`` node per wire (tube + housings, world frame, identity matrix) and
        the contact list (``assembly.connectors``); returns the node ids. Calling it again on the
        same assembly does nothing."""
        if self._added and all(i in assembly for i in self._added):
            return list(self._added)
        ids = []
        for w in self.wires:
            shape = wire_shape(w, bend_radius=self.bend_radius)
            part = PartSpec(f"wire {w.id}", shape, kind="wire", material=f"PVC {w.gauge_awg} AWG", color=w.color,
                            meta={"wire": w.id})
            ids.append(assembly.add(part, None, id=f"{prefix}{w.id}", wire=w.record()))
        known = {c["id"] for c in assembly.connectors}
        assembly.connectors.extend(c for c in self.connectors() if c["id"] not in known)
        self._added = ids
        return ids

    # -- cut list ----------------------------------------------------------------------------------
    def cut_list(self) -> list[dict]:
        """One row per wire: id, cable, from/to (ref.pin @ connector), net, signal, colour, AWG, lengths."""
        return [{"wire": w.id, "cable": w.cable or "", "from": f"{w.a.ref}.{w.a.pin}", "from_connector": w.a.connector,
                 "to": f"{w.b.ref}.{w.b.pin}", "to_connector": w.b.connector, "net": w.net, "signal": w.signal,
                 "color": w.color_name, "gauge_awg": w.gauge_awg, "route_mm": round(w.route_mm, 1),
                 "length_mm": round(w.length_mm, 1), "factory_lead": bool(w.lead_mm)} for w in self.wires]

    def cut_list_csv(self) -> str:
        rows = self.cut_list()
        buf = io.StringIO()
        fields = ["wire", "cable", "from", "from_connector", "to", "to_connector", "net", "signal", "color",
                  "gauge_awg", "route_mm", "length_mm", "factory_lead"]
        wr = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
        return buf.getvalue()

    def cut_list_markdown(self) -> str:
        out = [f"# Cut list — {self.name}", "",
               f"{len(self.wires)} wires ({sum(1 for w in self.wires if w.lead_mm)} of them factory leads), "
               f"{sum(w.length_mm for w in self.wires if not w.lead_mm) / 1000:.2f} m to cut. Lengths include "
               "service slack; route = routed length in the 3D model.", "",
               "| Wire | Cable | From | Connector | To | Connector | Signal | Colour | AWG | Route mm | Cut mm |",
               "|---|---|---|---|---|---|---|---|---:|---:|---:|"]
        for w in self.wires:
            cut = f"{w.length_mm:.0f}" + (" (lead)" if w.lead_mm else "")
            out.append(f"| {w.id} | {w.cable or ''} | {w.a.ref}.{w.a.pin} | {w.a.connector} | {w.b.ref}.{w.b.pin} | "
                       f"{w.b.connector} | {w.signal} | {w.color_name} | {w.gauge_awg} | {w.route_mm:.0f} | {cut} |")
        totals: dict[tuple[str, int], float] = {}
        for w in self.wires:
            if not w.lead_mm:
                totals[(w.color_name, w.gauge_awg)] = totals.get((w.color_name, w.gauge_awg), 0.0) + w.length_mm
        out += ["", "## Wire to buy", "", "| Colour | AWG | Total m |", "|---|---:|---:|"]
        for (col, awg), mm in sorted(totals.items()):
            out.append(f"| {col} | {awg} | {mm / 1000:.2f} |")
        return "\n".join(out) + "\n"

    # -- checks --------------------------------------------------------------------------------------
    def collisions(self) -> list[dict]:
        """Wire segments running through solid parts: ``[{"wire", "node", "volume_mm3", "at"}]``."""
        if self._collisions is not None:
            return self._collisions
        out: list[dict] = []
        obs = self._obstacles
        if obs is None:
            self._collisions = out
            return out
        for w in self.wires:
            pts = [_v(p) for p in w.points]
            r = w.od / 2 - 0.1
            worst: dict[str, tuple[float, Vec3]] = {}
            for k, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
                ignore = set()
                if k == 0:
                    ignore.add(w.a.node)
                if k == len(pts) - 2:
                    ignore.add(w.b.node)
                for node, vol in obs.hits(a, b, r, ignore=ignore, min_volume=self.min_volume):
                    if vol > worst.get(node, (0.0, None))[0]:
                        worst[node] = (vol, _t((a + b) / 2))
            for node, (vol, at) in worst.items():
                out.append({"wire": w.id, "node": node, "volume_mm3": round(vol, 3), "at": [round(v, 1) for v in at]})
        self._collisions = out
        return out

    def checks(self, *, collisions: bool = True) -> Report:
        """Harness findings (source ``harness`` in a build)."""
        rep = Report(title=f"harness: {self.name}")
        for u in self.unrouted:
            rep.add("WIRE.UNROUTED", Severity.WARNING,
                    f"Net {u['net']}: {u['ref']}.{u['pin']} is not wired in 3D ({u['reason']}).",
                    subject=f"net:{u['net']}", hint="Give the part an entry in anchors_by_ref.", **u)
        for u in self.no_anchor:
            rep.add("WIRE.NO_ANCHOR", Severity.WARNING,
                    f"{u['ref']} has no contact for pin {u['pin']} (net {u['net']}).", subject=f"part:{u['ref']}",
                    hint="Add a PinAnchor for that pin (module data 'anchors' or the perfboard layout).", **u)
        for o in self.channel_overflow:
            rep.add("WIRE.CHANNEL_FULL", Severity.WARNING,
                    f"Channel {o['channel']} carries {o['wires']} wires: the loom needs {o['need_mm']:.1f} mm of "
                    f"{o['room_mm']:.1f} mm.", subject=f"channel:{o['channel']}", **o)
        if collisions:
            for c in self.collisions():
                w = self.wire(c["wire"])
                rep.add("WIRE.COLLISION", Severity.ERROR,
                        f"Wire {w.id} ({w.a.ref}.{w.a.pin} → {w.b.ref}.{w.b.pin}) runs through {c['node']} "
                        f"({c['volume_mm3']:.1f} mm³ near {tuple(c['at'])}).", subject=f"wire:{w.id}",
                        hint="Add or move a channel, or move the part out of the wire's way.", **c)
        for w in self.wires:
            if w.lead_mm:
                if w.route_mm > w.lead_mm:
                    rep.add("WIRE.LEAD_TOO_SHORT", Severity.ERROR,
                            f"{w.cable} of {w.a.ref if w.a.anchor.lead_mm else w.b.ref} ({w.color_name}) needs "
                            f"{w.route_mm:.0f} mm but is {w.lead_mm:.0f} mm long.", subject=f"wire:{w.id}",
                            hint="Move the driver closer or add an extension.", wire=w.id, route_mm=w.route_mm,
                            lead_mm=w.lead_mm)
            elif w.length_mm > self.max_length:
                rep.add("WIRE.TOO_LONG", Severity.WARNING,
                        f"Wire {w.id} ({w.a.ref}.{w.a.pin} → {w.b.ref}.{w.b.pin}) is {w.length_mm:.0f} mm "
                        f"(> {self.max_length:.0f} mm).", subject=f"wire:{w.id}", wire=w.id, length_mm=w.length_mm)
        leads: dict[str, list[Wire]] = {}
        for w in self.wires:
            if w.lead_mm:
                leads.setdefault(w.group, []).append(w)
        for group, ws in leads.items():
            longest = max(x.route_mm for x in ws)
            if longest <= ws[0].lead_mm:  # type: ignore[operator]
                rep.add("WIRE.LEAD_SLACK", Severity.INFO,
                        f"{ws[0].cable} {group}: route {longest:.0f} mm of {ws[0].lead_mm:.0f} mm — "
                        f"{ws[0].lead_mm - longest:.0f} mm to coil and tie.", subject=f"cable:{group}",  # type: ignore[operator]
                        route_mm=round(longest, 1), lead_mm=ws[0].lead_mm)
        cut = sum(w.length_mm for w in self.wires if not w.lead_mm)
        rep.add("WIRE.SUMMARY", Severity.INFO,
                f"{len(self.wires)} wires in {len(self.cables())} cables/leads, {cut / 1000:.2f} m to cut; "
                f"{len(self.channels)} channel(s).", subject="harness", wires=len(self.wires),
                cables=len(self.cables()), cut_m=round(cut / 1000, 3))
        return rep

    def to_dict(self) -> dict:
        return {"name": self.name, "wires": [w.record() | {"points": [list(p) for p in w.points]} for w in self.wires],
                "connectors": self.connectors(), "channels": [c.__dict__ for c in self.channels]}


# ---------------------------------------------------------------------------------------------
# geometry of one wire
# ---------------------------------------------------------------------------------------------
def wire_shape(w: Wire, *, bend_radius: float | None = None) -> Any:
    """Tube along ``w.points`` (bends of ``bend_radius``, default 2.5 × Ø) + connector housings."""
    import build123d as bd

    r = w.od / 2
    bend = bend_radius if bend_radius is not None else 2.5 * w.od
    pts = _clean([_v(p) for p in w.points])
    pieces: list[Any] = []
    # straight pieces between tangent points, arcs (swept) at the corners, spheres where too tight
    n = len(pts)
    trims = [0.0] * n
    radii = [0.0] * n
    for i in range(1, n - 1):
        d1, d2 = pts[i] - pts[i - 1], pts[i + 1] - pts[i]
        l1, l2 = float(np.linalg.norm(d1)), float(np.linalg.norm(d2))
        cosang = float(np.clip(np.dot(d1, d2) / (l1 * l2), -1.0, 1.0))
        phi = math.acos(cosang)  # turning angle
        if phi < 1e-3:
            continue
        tanh = math.tan(phi / 2)
        rr = min(bend, 0.45 * l1 / tanh if tanh > 1e-9 else bend, 0.45 * l2 / tanh if tanh > 1e-9 else bend)
        if rr >= r * 1.15:
            radii[i] = rr
            trims[i] = rr * tanh
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        d = b - a
        L = float(np.linalg.norm(d))
        u = d / L
        s, e = a + u * trims[i], b - u * trims[i + 1]
        if np.linalg.norm(e - s) > 1e-3:
            pieces.append(_cylinder(s, e, r))
    for i in range(1, n - 1):
        if radii[i] > 0:
            d1 = (pts[i] - pts[i - 1]) / np.linalg.norm(pts[i] - pts[i - 1])
            d2 = (pts[i + 1] - pts[i]) / np.linalg.norm(pts[i + 1] - pts[i])
            p0, p2 = pts[i] - d1 * trims[i], pts[i] + d2 * trims[i]
            bis = d2 - d1
            bis /= np.linalg.norm(bis)
            centre = pts[i] + bis * math.hypot(radii[i], trims[i])
            mid = centre + ((pts[i] - centre) / np.linalg.norm(pts[i] - centre)) * radii[i]
            try:
                arc = bd.Edge.make_three_point_arc(_t(p0), _t(mid), _t(p2))
                prof = bd.Face(bd.Wire.make_circle(r, bd.Plane(origin=_t(p0), z_dir=_t(d1))))
                pieces.append(bd.Solid.sweep(prof, bd.Wire([arc])))
            except Exception:  # noqa: BLE001 - fall back to a ball joint
                pieces.append(bd.Solid.make_sphere(r).moved(bd.Location(_t(pts[i]))))
        elif i < n - 1:
            pieces.append(bd.Solid.make_sphere(r).moved(bd.Location(_t(pts[i]))))
    for piece in pieces:
        piece.color = bd.Color(w.color)
        piece.label = f"{w.id}"
    for end in (w.a, w.b):
        housing = _housing(end, w)
        if housing is not None:
            housing.color = bd.Color(HOUSING_COLOR)
            housing.label = f"{w.id}_{end.anchor.kind}"
            pieces.append(housing)
    return bd.Compound(children=pieces, label=f"wire {w.id}")


def _housing(end: WireEnd, w: Wire) -> Any:
    """Connector housing / sleeve solid at a wire end (None for a factory lead exit)."""
    import build123d as bd

    a = end.anchor
    kind = a.kind
    if kind == "lead" or end.fan > 0 and kind in ("dupont", "jst_xh"):
        return None
    d = _v(a.dir)
    start = end.start(w.od)
    if kind == "dupont":
        sx, sy, L = 2.4, 2.4, a.stub
    elif kind == "jst_xh":
        sx, sy, L = 2.4, 5.0, a.stub
    else:  # solder / lug: heat-shrink sleeve
        L = min(a.stub, 5.0)
        return bd.Solid.make_cylinder(w.od / 2 + 0.35, L, bd.Plane(origin=_t(start), z_dir=_t(d)))
    x_dir = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    if kind == "jst_xh":  # the plug's long side runs along the pin row: x for the ULN2003 socket
        x_dir = (0.0, 1.0, 0.0) if abs(d[1]) < 0.9 else (1.0, 0.0, 0.0)
    plane = bd.Plane(origin=_t(start + d * (L / 2)), x_dir=x_dir, z_dir=_t(d))
    box = bd.Solid.make_box(sx, sy, L).moved(bd.Location((-sx / 2, -sy / 2, -L / 2)))
    return box.moved(bd.Location(plane))


# ---------------------------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------------------------
def _net_style(circuit: "Circuit", net: "Net", index: int) -> tuple[str, str, bool]:
    """(colour name, signal text, is_rail) from the net's pins (see the module docstring)."""
    from piforge.elec.wiring import _rail_volts

    v = _rail_volts(net)
    if v is not None:
        if v == 0.0:
            return "black", "GND", True
        if abs(v - 5.0) < 0.3:
            return "red", net.name, True
        if abs(v - 3.3) < 0.2:
            return "orange", net.name, True
        return "brown", net.name, True
    gpio = next((r for r in net.refs if r.part.category == "board" and r.pin.name.startswith("GPIO")), None)
    if gpio is not None:
        enabled = {k for k, on in circuit.config(gpio.part).get("interfaces", {}).items() if on}
        for f in gpio.pin.functions:
            fam, _, line = f.partition("_")
            line = line.replace("_N", "")
            if fam.lower() in enabled:
                if fam.startswith("SPI"):
                    key = "CE" if line.startswith("CE") else line
                    return _SPI_COLORS.get(key, "grey"), f"SPI {line.replace('_', ' ')} ({gpio.pin.name})", False
                if fam.startswith("I2C"):
                    return _I2C_COLORS.get(line, "grey"), f"I2C {line} ({gpio.pin.name})", False
                if fam.startswith("UART"):
                    return _UART_COLORS.get(line, "grey"), f"UART {line} ({gpio.pin.name})", False
        return _PALETTE[index % len(_PALETTE)], gpio.pin.name, False
    for r in net.refs:
        for f in r.pin.functions:
            if f.startswith("SPI_"):
                line = f[4:]
                return _SPI_COLORS.get("CE" if line.startswith("C") else line, "grey"), f"SPI {line}", False
    return _PALETTE[index % len(_PALETTE)], "", False


def _lookup(table: Mapping[str, Any] | None, keys: Sequence[str]) -> Any:
    if not table:
        return None
    for k in keys:
        if k in table:
            return table[k]
    return None


@dataclass
class _Cable:
    key: tuple[str, str]
    wires: list[Wire]
    name: str | None
    centre: list[np.ndarray] = field(default_factory=list)
    seg_channel: list[str | None] = field(default_factory=list)  # channel of segment k (centre[k] → centre[k+1])
    radius: float = 1.0
    clear: bool = True


def route_harness(circuit: "Circuit", assembly: "Assembly",
                  anchors_by_ref: Mapping[str, tuple[str, Iterable[PinAnchor]]], *,
                  channels: Iterable[Channel] = (), hubs: Mapping[str, str] | None = None,
                  chains: Iterable[tuple[Iterable[str] | str, Sequence[str]]] = (),
                  cables: Mapping[str, tuple[str, str]] | None = None,
                  colors: Mapping[str, str] | None = None, gauges: Mapping[str, int] | None = None,
                  gauge_awg: int = 22, power_awg: int = 20, slack: float = 0.08, slack_mm: float = 20.0,
                  bend_radius: float | None = None, direct_max: float = 150.0, max_length: float = 600.0,
                  min_volume: float = 0.5, avoid_collisions: bool = True,
                  via: Mapping[str, Sequence[str]] | None = None, name: str = "harness") -> Harness:
    """Route the circuit's connections between anchored parts; see the module docstring.

    ``anchors_by_ref``: ``{ref: (node id, [PinAnchor…] in the node's part frame)}``.
    ``hubs``: ``{net name: ref}`` — that ref's pins are the net's star centre.
    ``chains``: ``[(net name(s), [ref, …])]`` — daisy-chain those refs in that net, starting from
    where the first one would be wired from.
    ``cables``: ``{cable name: (ref_a, ref_b)}`` names the cable between the nodes of two refs
    (default ``W-<a>-<b>``; factory leads keep their own name).
    ``colors``/``gauges``: overrides keyed by net name, ``REF.PIN``, ref or pin name (either end).
    ``gauge_awg``: default wire gauge; ``power_awg``: wires on a rail that touch a PSU.
    ``direct_max``: longest Manhattan run allowed outside the channels (mm).
    ``avoid_collisions``: test candidate routes against the solid parts (OCC booleans).
    ``via``: ``{cable-name pattern: [channel names]}`` — matching cables must run in one of those
    channels (no direct run), e.g. ``{"W-DRV*": ["floor_trunk"]}``.
    """
    from piforge.elec.wiring import _hubs, _wire_source

    channels = tuple(channels)
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        raise ValidationError("channel names must be unique")
    if gauge_awg not in AWG_OD or power_awg not in AWG_OD:
        raise ValidationError(f"gauge must be one of {sorted(AWG_OD)} AWG")

    # -- anchors in the world frame --------------------------------------------------------------
    world: dict[str, tuple[str, list[PinAnchor]]] = {}
    connectors: list[dict] = []
    seen_ids: set[str] = set()
    for ref, (node, anchors) in anchors_by_ref.items():
        m = assembly.world_matrix(node)
        lst = [a.transformed(m) for a in anchors]
        world[ref] = (node, lst)
        for a in lst:
            cid = a.id or f"{ref}.{a.number or a.pin}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            connectors.append({"id": cid, "ref": ref, "pin": a.number or a.pin, "label": a.label or a.pin,
                               "node": node, "pos": [round(v, 3) for v in a.pos],
                               "dir": [round(v, 4) for v in a.dir], "kind": a.kind})

    def cid_of(ref: str, a: PinAnchor) -> str:
        return a.id or f"{ref}.{a.number or a.pin}"

    def anchors_of(pr: "PinRef") -> list[PinAnchor]:
        ent = world.get(pr.part.ref)
        if ent is None:
            return []
        hits = [a for a in ent[1] if a.number and pr.pin.number and a.number == pr.pin.number
                and (a.pin == pr.pin.name or not a.pin)]
        return hits or [a for a in ent[1] if a.pin == pr.pin.name and not (a.number and pr.pin.number
                                                                              and a.number != pr.pin.number)]

    def node_of(pr: "PinRef") -> str | None:
        ent = world.get(pr.part.ref)
        return ent[0] if ent else None

    chain_list = []
    for nets, refs in chains:
        nets = (nets,) if isinstance(nets, str) else tuple(nets)
        chain_list.append((set(nets), list(refs)))

    # -- connections per net ------------------------------------------------------------------------
    unrouted: list[dict] = []
    no_anchor: list[dict] = []
    conns: list[tuple["Net", "PinRef", "PinRef"]] = []
    board = circuit.board
    for net in circuit.nets:
        if len(net.refs) < 2:
            continue
        if hubs and net.name in hubs:
            hub_pins = [r for r in net.refs if r.part.ref == hubs[net.name]]
            if not hub_pins:
                raise ValidationError(f"hubs[{net.name!r}] = {hubs[net.name]!r} has no pin in that net")
        else:
            hub_pins = _hubs(net, board)
        edges: list[tuple["PinRef", "PinRef"]] = []
        moved_hub = bool(hubs and net.name in hubs)
        for r in net.refs:
            if r in hub_pins:
                continue
            src = _wire_source(net, r, hub_pins)
            if src is None:
                continue
            if moved_hub and src not in hub_pins and board is not None and src.part is board:
                src = hub_pins[0]  # the board's same-named pins were the old star centre: wire from the hub
            edges.append((src, r))
        for nets_set, refs in chain_list:
            if net.name not in nets_set:
                continue
            members = [next((r for r in net.refs if r.part.ref == ref), None) for ref in refs]
            members = [m for m in members if m is not None]
            if not members:
                continue
            first_src = next((s for s, t in edges if t == members[0]), hub_pins[0])
            ms = set(members)
            edges = [(s, t) for s, t in edges if t not in ms]
            edges.append((first_src, members[0]))
            edges += list(zip(members[:-1], members[1:]))
        seen_pairs: set[frozenset] = set()
        for s, t in edges:
            ns, nt = node_of(s), node_of(t)
            if ns is None or nt is None:
                for pr in (s, t):
                    if node_of(pr) is None:
                        unrouted.append({"net": net.name, "ref": pr.part.ref, "pin": pr.pin.name,
                                         "reason": f"{pr.part.ref} has no anchors"})
                continue
            if ns == nt:
                continue  # joined on the board (perfboard trace, same-named header pins)
            pair = frozenset((ns, nt))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            conns.append((net, s, t))

    # -- choose contacts, build wires ---------------------------------------------------------------
    use_count: dict[str, int] = {}
    wires: list[Wire] = []
    sig_index: dict[str, int] = {}

    def pick(pr: "PinRef", node: str, net: "Net", other: "PinRef") -> tuple[str, str, PinAnchor] | None:
        """(ref, connector id, anchor) for the wire end on ``node`` (island of ``pr``)."""
        island = [x for x in net.refs if node_of(x) == node]
        cands: list[tuple[int, float, str, str, PinAnchor, "PinRef"]] = []
        far = None
        oth = anchors_of(other)
        if oth:
            far = _v(oth[0].pos)
        for x in island:
            for a in anchors_of(x):
                cid = cid_of(x.part.ref, a)
                if a.peer is not None and a.peer != other.part.ref:
                    rank = 3
                elif a.peer == other.part.ref:
                    rank = 0
                elif x == pr:
                    rank = 1
                else:
                    rank = 2
                rank += 0 if use_count.get(cid, 0) == 0 else 1
                dist = float(np.linalg.norm(_v(a.pos) - far)) if far is not None else 0.0
                cands.append((rank, dist, cid, x.part.ref, a, x))
        if not cands:
            no_anchor.append({"net": net.name, "ref": pr.part.ref, "pin": pr.pin.name})
            return None
        cands.sort(key=lambda c: (c[0], c[1], c[2]))
        _r, _d, cid, ref, a, x = cands[0]
        return ref, cid, a, x  # type: ignore[return-value]

    for k, (net, s, t) in enumerate(conns):
        ns, nt = node_of(s), node_of(t)
        ea = pick(s, ns, net, t)
        eb = pick(t, nt, net, s)
        if ea is None or eb is None:
            continue
        (ra, ca, aa, xa), (rb, cb, ab, xb) = ea, eb  # type: ignore[misc]
        fa, fb = use_count.get(ca, 0), use_count.get(cb, 0)
        use_count[ca], use_count[cb] = fa + 1, fb + 1
        idx = sig_index.setdefault(net.name, len(sig_index))
        cname, signal, rail = _net_style(circuit, net, idx)
        keys = [net.name, f"{xa.part.ref}.{xa.pin.name}", f"{xb.part.ref}.{xb.pin.name}", xb.part.ref, xa.part.ref,
                xb.pin.name, xa.pin.name]
        lead = aa if aa.lead_mm else ab if ab.lead_mm else None
        if lead is not None and lead.color:
            cname = lead.color
        cname = _lookup(colors, keys) or cname
        if not signal:
            signal = f"{xb.part.ref} {xb.pin.name}"
        gauge = (lead.gauge_awg if lead is not None and lead.gauge_awg else None) or _lookup(gauges, keys)
        if gauge is None:
            psu = any(p.part.category == "power" for p in (xa, xb))
            gauge = power_awg if rail and psu else gauge_awg
        if int(gauge) not in AWG_OD:
            raise ValidationError(f"gauge {gauge} AWG is not one of {sorted(AWG_OD)}")
        end_a = WireEnd(xa.part.ref, xa.pin.name, xa.pin.number, aa.label or xa.pin.name, ca, ns, aa, fa)
        end_b = WireEnd(xb.part.ref, xb.pin.name, xb.pin.number, ab.label or xb.pin.name, cb, nt, ab, fb)
        wires.append(Wire(id="", a=end_a, b=end_b, net=net.name, signal=signal, color_name=cname,
                          gauge_awg=int(gauge), cable=lead.cable if lead is not None else None,
                          lead_mm=lead.lead_mm if lead is not None else None,
                          group="|".join(sorted((ns, nt)))))

    # -- cables ---------------------------------------------------------------------------------------
    groups: dict[str, list[Wire]] = {}
    for w in wires:
        groups.setdefault(w.group, []).append(w)
    ref_node = {ref: node for ref, (node, _a) in world.items()}
    named = {}
    for cname_, (ra_, rb_) in (cables or {}).items():
        if ra_ not in ref_node or rb_ not in ref_node:
            raise NotFoundError("anchored ref", ra_ if ra_ not in ref_node else rb_, ref_node)
        named["|".join(sorted((ref_node[ra_], ref_node[rb_])))] = cname_
    cable_objs: list[_Cable] = []
    for key, ws in groups.items():
        lead = next((w for w in ws if w.lead_mm), None)
        if key in named:
            nm = named[key]
        elif lead is not None:
            nm = lead.cable
        elif len(ws) > 1:
            nm = f"W-{ws[0].a.ref}-{ws[0].b.ref}"
        else:
            nm = None
        for w in ws:
            w.cable = nm
        a_node, b_node = ws[0].a.node, ws[0].b.node
        cable_objs.append(_Cable((a_node, b_node), ws, nm))
    # stable wire ids: by cable, then net order
    order = sorted(cable_objs, key=lambda c: (c.name is None, c.name or "", c.key))
    n = 0
    for c in order:
        for w in c.wires:
            n += 1
            w.id = f"W{n}"

    # -- obstacles + routing ----------------------------------------------------------------------
    obstacles = _Obstacles(assembly)
    graph = _ChannelGraph(channels)
    for ch_name in [n_ for pats in (via or {}).values() for n_ in pats]:
        if ch_name not in graph.channels:
            raise NotFoundError("channel", ch_name, graph.channels)
    for c in order:
        forced = next((list(chs) for pat, chs in (via or {}).items() if fnmatch.fnmatchcase(c.name or "", pat)), None)
        _route_cable(c, graph, obstacles if avoid_collisions else None, direct_max, forced)
    overflow = _assign_slots(order, channels)
    for c in order:
        _wire_paths(c, channels)
    for w in wires:
        w.route_mm = _length([_v(p) for p in w.points])
        w.length_mm = float(w.lead_mm) if w.lead_mm else round(w.route_mm * (1 + slack) + slack_mm, 1)
    wires.sort(key=lambda w: int(w.id[1:]))
    log.info("harness %s: %d wires, %d cables, %d boolean tests", name, len(wires), len(order), obstacles.calls)
    return Harness(name, wires, connectors, channels, unrouted=unrouted, no_anchor=no_anchor,
                   channel_overflow=overflow, max_length=max_length, min_volume=min_volume,
                   bend_radius=bend_radius, obstacles=obstacles)


class _ChannelGraph:
    """Stations along the channels (ends + junctions) and shortest paths between channel points."""

    def __init__(self, channels: Sequence[Channel], tol: float = 0.5):
        self.channels = {c.name: c for c in channels}
        self.junctions: dict[str, list[tuple[float, str]]] = {c.name: [(0.0, f"{c.name}@0"), (1.0, f"{c.name}@1")]
                                                               for c in channels}
        self.points: dict[str, np.ndarray] = {}
        for c in channels:
            self.points[f"{c.name}@0"] = c.point(0.0)
            self.points[f"{c.name}@1"] = c.point(1.0)
        for c1, c2 in itertools.combinations(channels, 2):
            hit = _closest_between(c1, c2)
            if hit is None:
                continue
            t1, t2, p, dist = hit
            if dist <= tol:
                key = f"J:{c1.name}/{c2.name}"
                self.points[key] = p
                self.junctions[c1.name].append((t1, key))
                self.junctions[c2.name].append((t2, key))

    def path(self, ca: str, ta: float, cb: str, tb: float, *,
             allowed: set[str] | None = None) -> tuple[float, list[tuple[np.ndarray, str]]] | None:
        """Shortest route from (ca, ta) to (cb, tb): length and [(point, channel of the segment ending there)]."""
        if ca == cb:
            c = self.channels[ca]
            return abs(tb - ta) * c.length, [(c.point(tb), ca)]
        src, dst = "__A", "__B"
        adj: dict[str, list[tuple[float, str, str]]] = {}

        def link(u: str, v: str, w: float, ch: str) -> None:
            adj.setdefault(u, []).append((w, v, ch))
            adj.setdefault(v, []).append((w, u, ch))

        pts = dict(self.points)
        pts[src], pts[dst] = self.channels[ca].point(ta), self.channels[cb].point(tb)
        for name, stations in self.junctions.items():
            if allowed is not None and name not in allowed:
                continue
            st = sorted(stations + ([(ta, src)] if name == ca else []) + ([(tb, dst)] if name == cb else []))
            L = self.channels[name].length
            for (t1, k1), (t2, k2) in zip(st[:-1], st[1:]):
                link(k1, k2, (t2 - t1) * L, name)
        dist = {src: 0.0}
        prev: dict[str, tuple[str, str]] = {}
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > dist.get(u, math.inf):
                continue
            for w, v, ch in adj.get(u, ()):
                nd = d + w
                if nd < dist.get(v, math.inf) - 1e-9:
                    dist[v], prev[v] = nd, (u, ch)
                    heapq.heappush(heap, (nd, v))
        if dst not in dist:
            return None
        seq: list[tuple[np.ndarray, str]] = []
        u = dst
        while u != src:
            p, ch = prev[u]
            seq.append((pts[u], ch))
            u = p
        return dist[dst], list(reversed(seq))


def _closest_between(c1: Channel, c2: Channel) -> tuple[float, float, np.ndarray, float] | None:
    """Closest points of two channel centrelines: (t1, t2, midpoint, distance)."""
    p1, q1, p2, q2 = _v(c1.start), _v(c1.end), _v(c2.start), _v(c2.end)
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = np.dot(d1, d1), np.dot(d2, d2), np.dot(d2, r)
    c, b = np.dot(d1, r), np.dot(d1, d2)
    den = a * e - b * b
    s = float(np.clip((b * f - c * e) / den, 0, 1)) if den > 1e-12 else 0.0
    t = (b * s + f) / e
    if t < 0:
        t, s = 0.0, float(np.clip(-c / a, 0, 1))
    elif t > 1:
        t, s = 1.0, float(np.clip((b - c) / a, 0, 1))
    x1, x2 = p1 + d1 * s, p2 + d2 * t
    return s, float(t), (x1 + x2) / 2, float(np.linalg.norm(x1 - x2))


def _lattice(n: int, pitch: float) -> list[np.ndarray]:
    """``n`` 3D offsets whose projections onto every axis plane are distinct (Latin square)."""
    m = max(1, math.ceil(math.sqrt(n)))
    out = []
    for i in range(n):
        a, b = i % m, i // m
        c = (a + b) % m
        out.append((np.array([a, b, c], dtype=float) - (m - 1) / 2) * pitch)
    return out


def _route_cable(c: _Cable, graph: _ChannelGraph, obs: _Obstacles | None, direct_max: float,
                 forced: list[str] | None = None) -> None:
    pitch = max(w.od for w in c.wires) + 0.15
    c.radius = max(pitch / 2, math.ceil(math.sqrt(len(c.wires))) * pitch / 2 * 1.1)
    ends = []
    for side in ("a", "b"):
        es = [getattr(w, side) for w in c.wires]
        bos = [e.breakout(w.od) for e, w in zip(es, c.wires)]
        centre = np.mean(bos, axis=0)
        d = np.mean([_v(e.anchor.dir) for e in es], axis=0)
        d = d / np.linalg.norm(d) if np.linalg.norm(d) > 1e-6 else _v(es[0].anchor.dir)
        ax = _dominant(d)
        dd = np.zeros(3)
        dd[ax] = math.copysign(1.0, d[ax])
        ends.append((centre + dd * (c.radius + pitch), ax, {e.node for e in es}))
    (pa, axa, _na), (pb, axb, _nb) = ends
    r_test = max(c.radius - 0.3, 0.4)

    def clear(pts: list[np.ndarray]) -> bool:
        return obs is None or obs.path_clear(pts, r_test)

    def orders_from(ax: int) -> list[tuple[int, ...]]:
        return sorted(_ORDERS, key=lambda o: (o[0] != ax, o))

    candidates: list[tuple[float, bool, list[np.ndarray], list[str | None]]] = []
    allowed = [ch for ch in graph.channels.values() if ch.allows(c.name) and (forced is None or ch.name in forced)]
    if forced is None and np.sum(np.abs(pb - pa)) <= direct_max:
        for order in orders_from(axa):
            pts = _manhattan(pa, pb, order)
            ok = clear(pts)
            candidates.append((_length(pts), ok, pts, [None] * (len(pts) - 1)))
            if ok:
                break

    def drops(p: np.ndarray, ax: int, reverse: bool) -> list[tuple[float, bool, str, float, list[np.ndarray]]]:
        out = []
        chans = sorted(allowed, key=lambda ch: float(np.linalg.norm(ch.project(p)[1] - p)))[:3]
        for ch in chans:
            t, e = ch.project(p)
            best = None
            for order in orders_from(ax):
                pts = _manhattan(p, e, order)
                if reverse:
                    pts = list(reversed(pts))
                ok = clear(pts)
                cand = (_length(pts), ok, ch.name, t, pts)
                if ok:
                    best = cand
                    break
                if best is None:
                    best = cand
            if best is not None:
                out.append(best)
        return out

    if allowed:
        da, db = drops(pa, axa, False), drops(pb, axb, True)
        for (la, oka, cha, ta, pts_a), (lb, okb, chb, tb, pts_b) in itertools.product(da, db):
            mid = graph.path(cha, ta, chb, tb, allowed={ch.name for ch in allowed})
            if mid is None:
                continue
            lm, seq = mid
            pts = list(pts_a)
            seg = [None] * (len(pts_a) - 1)
            for p, ch in seq:
                pts.append(p)
                seg.append(ch)
            pts += pts_b[1:]
            seg += [None] * (len(pts_b) - 1)
            candidates.append((la + lm + lb, oka and okb, pts, seg))
    if not candidates:  # no channel, run too long: direct anyway
        pts = _manhattan(pa, pb, orders_from(axa)[0])
        candidates.append((_length(pts), clear(pts), pts, [None] * (len(pts) - 1)))
    candidates.sort(key=lambda x: (not x[1], x[0]))
    _l, ok, pts, seg = candidates[0]
    c.centre, c.seg_channel, c.clear = pts, seg, ok


def _assign_slots(cables: Sequence[_Cable], channels: Sequence[Channel]) -> list[dict]:
    """Give every wire a slot in each channel it runs through (``w._slots[channel]`` = offset)."""
    overflow = []
    for ch in channels:
        users = [c for c in cables if ch.name in c.seg_channel]
        if not users:
            continue
        users.sort(key=lambda c: (-len(c.wires), c.name or "", c.key))
        ws = [w for c in users for w in c.wires]
        pitch = max(w.od for w in ws) + 0.15
        a1, a2 = ch.perp_axes
        cols = max(1, int(ch.width // pitch))
        rows = math.ceil(len(ws) / cols)
        need = rows * pitch
        if need > ch.height + 1e-6:
            overflow.append({"channel": ch.name, "wires": len(ws), "need_mm": round(need, 2),
                             "room_mm": ch.height})
        used_cols = min(cols, len(ws))
        for k, w in enumerate(ws):
            col, row = k % cols, k // cols
            off = np.zeros(3)
            off[a1] = (col - (used_cols - 1) / 2) * pitch
            if ch.stack == "bottom":
                off[a2] = -ch.height / 2 + pitch / 2 + row * pitch
            else:
                off[a2] = (row - (rows - 1) / 2) * pitch
            w.__dict__.setdefault("_slots", {})[ch.name] = off
    return overflow


def _wire_paths(c: _Cable, channels: Sequence[Channel]) -> None:
    """Polyline of every wire: contact → stub → bundle (lattice / channel slot) → stub → contact."""
    pitch = max(w.od for w in c.wires) + 0.15
    lat = _lattice(len(c.wires), pitch)
    for w, lo in zip(c.wires, lat):
        slots = w.__dict__.get("_slots", {})

        def off(k: int) -> np.ndarray:
            ch = c.seg_channel[k] if 0 <= k < len(c.seg_channel) else None
            return slots.get(ch, lo) if ch else lo

        pts: list[np.ndarray] = [w.a.start(w.od), w.a.breakout(w.od)]
        first = c.centre[0] + off(0)
        pts += _manhattan(pts[-1], first, _jog_order(w.a.anchor.dir))[1:]
        for k in range(1, len(c.centre)):
            p = c.centre[k]
            o_in, o_out = off(k - 1), off(k)
            pts.append(p + o_in)
            if k < len(c.centre) - 1 and np.linalg.norm(o_in - o_out) > 1e-6:
                nxt = p + o_out
                pts += _manhattan(pts[-1], nxt, _jog_axes(c.centre, k))[1:]
        end_bo = w.b.breakout(w.od)
        pts += list(reversed(_manhattan(end_bo, pts[-1], _jog_order(w.b.anchor.dir))))[1:]
        pts.append(w.b.start(w.od))
        pts = _clean(pts)
        w.points = [_t(p) for p in pts]
        w.channels = sorted({ch for ch in c.seg_channel if ch})


def _jog_order(d: Sequence[float]) -> tuple[int, ...]:
    ax = _dominant(d)
    return (ax, *[i for i in range(3) if i != ax])


def _jog_axes(centre: Sequence[np.ndarray], k: int) -> tuple[int, ...]:
    """At a channel change: move across the outgoing direction first, along it last."""
    nxt = centre[k + 1] - centre[k]
    ax = _dominant(nxt)
    return (*[i for i in range(3) if i != ax], ax)
