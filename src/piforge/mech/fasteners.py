"""Metric fasteners: data table, hole cutters for 3D-printed parts, nut traps, screws, standoffs.

Every hole cutter is a build123d ``Part`` with its axis on Z, centred on the origin, entry at the
top. Through-cutters span z ∈ [-EPS, depth + EPS] so they poke EPS = 0.01 mm through both faces
(no coplanar faces after booleans). Holes are enlarged by the printer's ``hole_compensation``
(FDM holes print undersized); ``printer=None`` means the ``generic`` profile.

The CAD kernel is imported lazily so the data table is usable without it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from piforge.core.errors import NotFoundError, ValidationError
from piforge.fab.profiles import PrinterProfile, get_printer

if TYPE_CHECKING:  # pragma: no cover
    from build123d import Part

EPS = 0.01  # mm, same as piforge.mech.primitives.EPS
# src: est — FDM loose-fit practice (cf. PrinterProfile.clearance_loose = 0.4 per side); a countersunk
#      head seats on its cone, so a smaller gap is enough
CBORE_CLEARANCE = 0.3  # mm per side around a socket head in a counterbore
CSK_CLEARANCE = 0.2  # mm per side around a countersunk head


@dataclass(frozen=True)
class MetricSize:
    """Dimensions (mm) of an ISO metric coarse-thread screw size and its common hardware."""

    name: str
    d: float  # nominal diameter
    pitch: float  # coarse pitch
    clearance_close: float  # ISO 273 fine
    clearance_normal: float  # ISO 273 medium
    clearance_loose: float  # ISO 273 coarse
    tap_plastic: float  # pilot hole for thread-forming a machine screw into PLA/PETG
    head_socket_d: float  # ISO 4762 socket head cap screw, dk max
    head_socket_h: float  # ISO 4762 head height k
    head_csk_d: float  # 90° countersunk head, theoretical max diameter
    nut_af: float  # ISO 4032 hex nut, width across flats
    nut_h: float  # ISO 4032 hex nut height
    insert_hole_d: float  # recommended hole for brass heat-set inserts
    insert_depth: float  # insert length

    def clearance(self, fit: str = "normal") -> float:
        """Clearance hole diameter for ``fit`` ∈ {close, normal, loose} (before compensation)."""
        table = {"close": self.clearance_close, "normal": self.clearance_normal,
                 "loose": self.clearance_loose}
        if fit not in table:
            raise ValidationError(f"fit must be one of {tuple(table)}, got {fit!r}")
        return table[fit]


# src: pitch ISO 261 coarse; clearances ISO 273 fine/medium/coarse; tap_plastic = ISO 2306 tap drill
#      (d − P), the usual pilot for thread-forming screws in PLA/PETG (printer compensation added
#      separately); socket heads ISO 4762 (dk max, k); countersunk heads = max of ISO 10642 and
#      ISO 7046-1 theoretical dk; nuts ISO 4032 (s, m max); heat-set inserts: CNC Kitchen / Ruthex
#      datasheets (recommended hole Ø × insert length, e.g. M3 4.0 × 5.7).
METRIC: dict[str, MetricSize] = {
    s.name: s
    for s in [
        #          name   d     P     close normal loose tap   sock_d sock_h csk_d nut_af nut_h ins_d ins_l
        MetricSize("M2", 2.0, 0.40, 2.2, 2.4, 2.6, 1.6, 3.8, 2.0, 4.4, 4.0, 1.6, 3.2, 4.0),
        MetricSize("M2.5", 2.5, 0.45, 2.7, 2.9, 3.1, 2.05, 4.5, 2.5, 5.5, 5.0, 2.0, 3.6, 5.7),
        MetricSize("M3", 3.0, 0.50, 3.2, 3.4, 3.6, 2.5, 5.5, 3.0, 6.72, 5.5, 2.4, 4.0, 5.7),
        MetricSize("M4", 4.0, 0.70, 4.3, 4.5, 4.8, 3.3, 7.0, 4.0, 9.4, 7.0, 3.2, 5.6, 8.1),
        MetricSize("M5", 5.0, 0.80, 5.3, 5.5, 5.8, 4.2, 8.5, 5.0, 11.2, 8.0, 4.7, 6.4, 9.5),
    ]
}


def get_size(size: "str | MetricSize") -> MetricSize:
    """Look up a metric size such as ``"M3"`` (case-insensitive); sizes pass through unchanged."""
    if isinstance(size, MetricSize):
        return size
    key = str(size).strip().upper()
    if key in METRIC:
        return METRIC[key]
    raise NotFoundError("metric size", size, METRIC)


def hole_compensation(printer: "str | PrinterProfile | None" = None) -> float:
    """Diameter added to printed holes for ``printer`` (``None`` → the ``generic`` profile), mm."""
    return get_printer(printer if printer is not None else "generic").hole_compensation


def _bd():
    import build123d

    return build123d


def _check_depth(depth: float) -> None:
    if not (isinstance(depth, (int, float)) and math.isfinite(depth) and depth > 0):
        raise ValidationError(f"hole depth must be a positive number of mm, got {depth!r}")


def _cylinder(d: float, z0: float, z1: float) -> "Part":
    bd = _bd()
    cyl = bd.Cylinder(d / 2, z1 - z0, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    return bd.Part(cyl.moved(bd.Location((0, 0, z0))).wrapped)


def _through(d: float, depth: float) -> "Part":
    return _cylinder(d, -EPS, depth + EPS)


def clearance_hole(size: "str | MetricSize", depth: float, *, fit: str = "normal",
                   printer: "str | PrinterProfile | None" = None) -> "Part":
    """Through-hole cutter for a screw to pass freely; z ∈ [-EPS, depth + EPS]."""
    s = get_size(size)
    _check_depth(depth)
    return _through(s.clearance(fit) + hole_compensation(printer), depth)


def tap_hole(size: "str | MetricSize", depth: float, *,
             printer: "str | PrinterProfile | None" = None) -> "Part":
    """Pilot-hole cutter for driving a machine screw straight into plastic; z ∈ [-EPS, depth + EPS]."""
    s = get_size(size)
    _check_depth(depth)
    return _through(s.tap_plastic + hole_compensation(printer), depth)


def insert_hole(size: "str | MetricSize", *, extra_depth: float = 1.0,
                printer: "str | PrinterProfile | None" = None) -> "Part":
    """Cutter for a brass heat-set insert: Ø ``insert_hole_d`` + compensation.

    Depth = insert length + ``extra_depth`` (room for displaced plastic); z ∈ [-EPS, depth + EPS],
    the insert goes in from the top. Place it so its top is flush with the boss top.
    """
    s = get_size(size)
    if extra_depth < 0:
        raise ValidationError("extra_depth must be >= 0 mm")
    return _through(s.insert_hole_d + hole_compensation(printer), s.insert_depth + extra_depth)


def counterbore_hole(size: "str | MetricSize", depth: float, *, head_depth: float | None = None,
                     printer: "str | PrinterProfile | None" = None) -> "Part":
    """Clearance hole plus a socket-head counterbore at the top (z = depth).

    Counterbore Ø = head_socket_d + 2·0.3 + compensation; ``head_depth`` defaults to the head height
    + 0.2 mm so the head sits just below the surface.
    """
    s = get_size(size)
    _check_depth(depth)
    hd = s.head_socket_h + 0.2 if head_depth is None else float(head_depth)
    if not 0 < hd < depth:
        raise ValidationError(f"head_depth {hd} mm must be between 0 and the hole depth {depth} mm")
    comp = hole_compensation(printer)
    bore = _cylinder(s.head_socket_d + 2 * CBORE_CLEARANCE + comp, depth - hd, depth + EPS)
    return _through(s.clearance_normal + comp, depth) + bore


def countersink_hole(size: "str | MetricSize", depth: float, *,
                     printer: "str | PrinterProfile | None" = None) -> "Part":
    """Clearance hole with a 90° countersink at the top (z = depth) for flat-head screws."""
    bd = _bd()
    s = get_size(size)
    _check_depth(depth)
    comp = hole_compensation(printer)
    r_top = (s.head_csk_d + 2 * CSK_CLEARANCE + comp) / 2
    r_hole = (s.clearance_normal + comp) / 2
    cone_h = r_top - r_hole  # 90° included angle
    if cone_h >= depth:
        raise ValidationError(f"{s.name} countersink needs a part thicker than {cone_h:.2f} mm")
    cone = bd.Cone(r_hole, r_top, cone_h, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    cone = bd.Part(cone.moved(bd.Location((0, 0, depth - cone_h))).wrapped)
    cap = _cylinder(2 * r_top, depth - EPS, depth + EPS)
    return _through(2 * r_hole, depth) + cone + cap


def nut_trap(size: "str | MetricSize", depth: float, *, clearance: float = 0.2) -> "Part":
    """Hexagonal pocket for a nut: across flats = nut_af + 2·clearance, flats parallel to X.

    z ∈ [-EPS, depth + EPS] (pocket opening at the top or bottom face, overlapping by EPS).
    """
    bd = _bd()
    s = get_size(size)
    _check_depth(depth)
    if clearance < 0:
        raise ValidationError("clearance must be >= 0 mm")
    af = s.nut_af + 2 * clearance
    r = af / math.sqrt(3)  # corner radius; corners on ±X → flats parallel to X
    pts = [(r * math.cos(math.radians(a)), r * math.sin(math.radians(a))) for a in range(0, 360, 60)]
    prism = bd.extrude(bd.Polygon(*pts, align=None), depth + 2 * EPS)
    return bd.Part(prism.moved(bd.Location((0, 0, -EPS))).wrapped)


_HEADS = ("socket", "button", "countersunk")
_SOCKET_KEY = {"M2": 1.5, "M2.5": 2.0, "M3": 2.5, "M4": 3.0, "M5": 4.0}  # src: ISO 4762 s (hex key)
_BUTTON_KEY = {"M2": 1.3, "M2.5": 1.5, "M3": 2.0, "M4": 2.5, "M5": 3.0}  # src: ISO 7380-1 s


def screw(size: "str | MetricSize", length: float, *, head: str = "socket") -> "Part":
    """Reference model of a screw (no thread): head on top, shank down to z = -length.

    ``socket`` (ISO 4762) and ``button`` (ISO 7380, approx.) heads sit on z = 0 (their underside);
    a ``countersunk`` head (90°) has its top flush with z = 0 and counts in the length.
    """
    bd = _bd()
    s = get_size(size)
    _check_depth(length)
    if head not in _HEADS:
        raise ValidationError(f"head must be one of {_HEADS}, got {head!r}")
    if head == "countersunk":
        r_head = s.head_csk_d / 2
        cone_h = r_head - s.d / 2
        if cone_h >= length:
            raise ValidationError(f"{s.name} countersunk screw must be longer than {cone_h:.2f} mm")
        cone = bd.Cone(s.d / 2, r_head, cone_h, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MAX))
        return _cylinder(s.d, -length, -cone_h + EPS) + bd.Part(cone.wrapped)
    if head == "socket":
        hd, hh = s.head_socket_d, s.head_socket_h
        key = _SOCKET_KEY.get(s.name, 0.8 * s.d)
    else:  # button head; dk = 1.9·d, k = 0.55·d match ISO 7380-1 for M3–M5
        hd, hh = 1.9 * s.d, 0.55 * s.d
        key = _BUTTON_KEY.get(s.name, 0.6 * s.d)
    body = _cylinder(s.d, -length, EPS) + _cylinder(hd, 0.0, hh)
    socket_depth = min(0.5 * hh, hh - 0.4)
    if socket_depth > 0.2:
        r = key / math.sqrt(3)
        pts = [(r * math.cos(math.radians(a)), r * math.sin(math.radians(a))) for a in range(0, 360, 60)]
        recess = bd.extrude(bd.Polygon(*pts, align=None), socket_depth + EPS)
        body = body - bd.Part(recess.moved(bd.Location((0, 0, hh - socket_depth))).wrapped)
    return body


def standoff(size: "str | MetricSize", height: float, *, od: float | None = None, hole: str = "tap",
             printer: "str | PrinterProfile | None" = None) -> "Part":
    """Cylindrical boss, z ∈ [0, height], with a hole from the top for the screw.

    ``hole`` ∈ {tap (thread-forming pilot, full height), insert (heat-set insert pocket),
    clearance (through), none}. Default ``od`` = max(2·d + 1, insert Ø + 2.4) — e.g. 6.0 mm for
    M2.5, which fits the 6 mm keep-out pad around Raspberry Pi mounting holes.
    """
    bd = _bd()
    s = get_size(size)
    _check_depth(height)
    if hole not in ("tap", "insert", "clearance", "none"):
        raise ValidationError(f"hole must be one of tap, insert, clearance, none — got {hole!r}")
    if od is None:
        od = max(2 * s.d + 1.0, s.insert_hole_d + 2.4 if hole == "insert" else 0.0)
    comp = hole_compensation(printer)
    hole_d = {"tap": s.tap_plastic + comp, "insert": s.insert_hole_d + comp,
              "clearance": s.clearance_normal + comp, "none": 0.0}[hole]
    if od <= hole_d + 0.8:
        raise ValidationError(f"standoff od {od} mm leaves less than 0.4 mm wall around a {hole_d:.2f} mm hole")
    boss = _cylinder(od, 0.0, height)
    if hole == "none":
        return boss
    z0 = height - (s.insert_depth + 1.0) if hole == "insert" else -EPS
    if z0 <= 0:  # pocket deeper than the boss: go through, overlapping the bottom face by EPS
        z0 = -EPS
    cutter = _cylinder(hole_d, z0, height + EPS)
    return bd.Part((boss - cutter).wrapped)
