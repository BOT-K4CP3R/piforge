"""Parametric building blocks: rounded boxes, shells, slots, vent cutters, text, bottom chamfers.

Conventions: millimetres, Z up. Unless stated otherwise every function returns a build123d ``Part``
centred in XY with its bottom on z = 0. Cutters (vents, holes) overlap the faces they open by
:data:`EPS` so boolean results never contain coplanar faces or zero-thickness slivers.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

import build123d as bd
from build123d import Axis, Location, Part
from OCP.TopAbs import TopAbs_COMPOUND

from piforge.core.errors import PiForgeError, ValidationError

log = logging.getLogger(__name__)

EPS = 0.01  # mm — how far every cutter pokes through the faces it opens
_TOL = 1e-9


def _require_positive(**values: float) -> None:
    for name, value in values.items():
        if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
            raise ValidationError(f"{name} must be a positive number of mm, got {value!r}")


def _require_non_negative(**values: float) -> None:
    for name, value in values.items():
        if not (isinstance(value, (int, float)) and math.isfinite(value) and value >= 0):
            raise ValidationError(f"{name} must be >= 0 mm, got {value!r}")


def _as_part(shape: object) -> Part:
    """Wrap any build123d shape (Solid, Compound, Part) as a ``Part`` backed by a TopoDS_Compound.

    ``Part(solid.wrapped)`` would wrap a bare solid, and Compound-based properties such as
    ``volume`` then silently return 0 — so non-compounds are put inside a real compound.
    """
    wrapped = shape.wrapped  # type: ignore[attr-defined]
    if wrapped.ShapeType() == TopAbs_COMPOUND:
        return shape if isinstance(shape, Part) else Part(wrapped)
    return Part(bd.Compound([shape]).wrapped)  # type: ignore[list-item]


def _faces_to_part(faces: Iterable[bd.Face], depth: float) -> Part:
    """Extrude planar faces (on z = 0) by ``depth`` and centre the result on z = 0."""
    faces = list(faces)
    solids = [bd.extrude(f, depth) for f in faces]
    part = Part(bd.Compound(solids).wrapped)
    return part.moved(Location((0, 0, -depth / 2)))


def rounded_box(length: float, width: float, height: float, radius: float = 0.0, *,
                top_radius: float = 0.0, bottom_chamfer: float = 0.0) -> Part:
    """Box L×W×H (mm) with vertical edges rounded by ``radius``.

    ``top_radius`` fillets the top edges (at most ``radius`` when ``radius`` > 0; equal radii are
    clamped to ``radius - EPS`` because exactly spherical corners mesh badly), ``bottom_chamfer``
    chamfers the bottom edges (0.3–0.5 mm absorbs elephant's foot). Centred in XY, bottom on z = 0.
    """
    _require_positive(length=length, width=width, height=height)
    _require_non_negative(radius=radius, top_radius=top_radius, bottom_chamfer=bottom_chamfer)
    if radius >= min(length, width) / 2:
        raise ValidationError(
            f"rounded_box radius {radius} mm must be < half the shorter side ({min(length, width) / 2} mm)")
    if top_radius + bottom_chamfer >= height:
        raise ValidationError(
            f"top_radius + bottom_chamfer ({top_radius + bottom_chamfer} mm) must be < height ({height} mm)")
    if radius > 0 and top_radius > radius + _TOL:
        raise ValidationError(f"top_radius {top_radius} mm must be <= corner radius {radius} mm")
    if top_radius >= min(length, width) / 2:
        raise ValidationError("top_radius must be < half the shorter side")
    if radius > 0 and bottom_chamfer >= radius:
        raise ValidationError(f"bottom_chamfer {bottom_chamfer} mm must be < corner radius {radius} mm")

    if radius > 0 and top_radius > radius - EPS:
        # equal radii make spherical corner patches with degenerate poles → non-watertight meshes
        log.debug("rounded_box: top_radius %.3f clamped to radius - EPS = %.3f", top_radius, radius - EPS)
        top_radius = radius - EPS
    sketch = bd.RectangleRounded(length, width, radius) if radius > 0 else bd.Rectangle(length, width)
    part = _as_part(bd.extrude(sketch, height))
    try:
        if top_radius > 0:
            part = _as_part(part.fillet(top_radius, part.edges().group_by(Axis.Z)[-1]))
        if bottom_chamfer > 0:
            part = _as_part(part.chamfer(bottom_chamfer, None, part.edges().group_by(Axis.Z)[0]))
    except Exception as exc:  # OCC raises StdFail_NotDone & co.
        raise PiForgeError(
            f"rounded_box({length}, {width}, {height}, radius={radius}, top_radius={top_radius}, "
            f"bottom_chamfer={bottom_chamfer}) failed in the CAD kernel: {exc}") from exc
    return part


def hollow_box(length: float, width: float, height: float, wall: float, floor: float,
               radius: float = 0.0, *, open_top: bool = True) -> Part:
    """Shell L×W×H with side walls ``wall`` thick and a bottom ``floor`` thick (mm).

    ``open_top=False`` closes it with a top of thickness ``floor`` (a sealed box). The inner corner
    radius is ``max(radius - wall, 0)`` so the wall thickness stays constant around corners.
    """
    _require_positive(length=length, width=width, height=height, wall=wall, floor=floor)
    _require_non_negative(radius=radius)
    if 2 * wall >= min(length, width):
        raise ValidationError(f"walls ({wall} mm) leave no cavity in a {length}×{width} mm box")
    top = 0.0 if open_top else floor
    if floor + top >= height:
        raise ValidationError(f"floor/top ({floor + top} mm) leave no cavity in a box {height} mm tall")
    outer = rounded_box(length, width, height, radius)
    il, iw = length - 2 * wall, width - 2 * wall
    ir = max(radius - wall, 0.0)  # < min(il, iw) / 2 because radius < min(length, width) / 2
    cavity_h = (height - floor + EPS) if open_top else (height - floor - top)
    cavity = rounded_box(il, iw, cavity_h, ir).moved(Location((0, 0, floor)))
    return _as_part(outer - cavity)


def slot(length: float, width: float, depth: float) -> Part:
    """Stadium (obround) prism: overall ``length`` along X, ``width`` along Y, z ∈ [0, depth]."""
    _require_positive(length=length, width=width, depth=depth)
    if length < width:
        raise ValidationError(f"slot length {length} mm must be >= its width {width} mm")
    face = bd.Circle(width / 2) if math.isclose(length, width) else bd.SlotOverall(length, width)
    return _as_part(bd.extrude(face, depth))


def _count(span: float, size: float, pitch: float) -> int:
    return int(math.floor((span - size) / pitch + _TOL)) + 1


def vent_slots(area_l: float, area_w: float, *, slot_w: float = 2.0, pitch: float = 4.0,
               depth: float = 10.0, slot_axis: str = "x", r_ends: bool = True) -> Part:
    """Cutter of parallel slots filling an ``area_l`` (X) × ``area_w`` (Y) rectangle.

    ``slot_axis="x"`` makes slots run along X (length ``area_l``), repeated along Y every ``pitch``;
    ``"y"`` the other way round. Centred on the origin in XY, z ∈ [-depth/2, depth/2] so it cuts
    through a wall of thickness < ``depth`` placed at z = 0. ``r_ends`` rounds the slot ends.
    """
    _require_positive(area_l=area_l, area_w=area_w, slot_w=slot_w, pitch=pitch, depth=depth)
    if slot_axis not in ("x", "y"):
        raise ValidationError(f"slot_axis must be 'x' or 'y', got {slot_axis!r}")
    if pitch <= slot_w:
        raise ValidationError(f"pitch {pitch} mm must exceed slot_w {slot_w} mm (slots would merge)")
    length, span = (area_l, area_w) if slot_axis == "x" else (area_w, area_l)
    if span < slot_w or length < slot_w:
        raise ValidationError(f"vent area {area_l}×{area_w} mm is smaller than one {slot_w} mm slot")
    n = _count(span, slot_w, pitch)
    rotation = 0 if slot_axis == "x" else 90
    faces = []
    for i in range(n):
        off = (i - (n - 1) / 2) * pitch
        if r_ends and length > slot_w:
            face = bd.SlotOverall(length, slot_w, rotation=rotation)
        else:
            face = bd.Rectangle(length, slot_w, rotation=rotation)
        pos = (0, off, 0) if slot_axis == "x" else (off, 0, 0)
        faces.append(face.moved(Location(pos)))
    return _faces_to_part(faces, depth)


def hex_vents(area_l: float, area_w: float, *, cell: float = 5.0, web: float = 1.2,
              depth: float = 10.0) -> Part:
    """Honeycomb cutter: hexagonal holes ``cell`` across flats separated by ``web`` (mm).

    Hexagons have flats parallel to X; only whole cells inside the ``area_l`` × ``area_w`` rectangle
    are kept. Centred on the origin in XY, z ∈ [-depth/2, depth/2].
    """
    _require_positive(area_l=area_l, area_w=area_w, cell=cell, web=web, depth=depth)
    r_corner = cell / math.sqrt(3)
    p = cell + web
    dx, dy = p * math.sqrt(3) / 2, p
    half_l, half_w = area_l / 2, area_w / 2
    pts = [(r_corner * math.cos(math.radians(a)), r_corner * math.sin(math.radians(a)))
           for a in range(0, 360, 60)]
    faces = []
    jmax = int(half_l // dx) + 1
    imax = int(half_w // dy) + 2
    for j in range(-jmax, jmax + 1):
        cx = j * dx
        if abs(cx) + r_corner > half_l + _TOL:
            continue
        y0 = dy / 2 if j % 2 else 0.0
        for i in range(-imax, imax + 1):
            cy = y0 + i * dy
            if abs(cy) + cell / 2 > half_w + _TOL:
                continue
            faces.append(bd.Polygon(*[(cx + x, cy + y) for x, y in pts], align=None))
    if not faces:
        raise ValidationError(f"no {cell} mm hexagon fits in a {area_l}×{area_w} mm vent area")
    return _faces_to_part(faces, depth)


def text_solid(text: str, size: float = 6.0, depth: float = 0.6, *, font: str = "Arial",
               bold: bool = True) -> Part:
    """Extruded text (font size ``size`` mm, ``depth`` mm tall), centred in XY, z ∈ [0, depth].

    To put text on a part use :func:`emboss` / :func:`engrave`: they sink the text :data:`EPS`
    into the face (emboss) or let it poke out by ``EPS`` (engrave), so the boolean never meets a
    coplanar face. Doing it by hand, overlap by ``EPS`` the same way — never place the text exactly
    on the face. Unknown fonts fall back to the CAD kernel's default font.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("text_solid needs non-empty text")
    _require_positive(size=size, depth=depth)
    style = bd.FontStyle.BOLD if bold else bd.FontStyle.REGULAR
    try:
        sketch = bd.Text(text, size, font=font, font_style=style)
        part = _as_part(bd.extrude(sketch, depth))
    except Exception as exc:
        raise PiForgeError(f"text_solid({text!r}) failed in the CAD kernel: {exc}") from exc
    bb = part.bounding_box()
    return part.moved(Location((-(bb.min.X + bb.max.X) / 2, -(bb.min.Y + bb.max.Y) / 2, -bb.min.Z)))


_FACE_DIRS = {"top": (0, 0, 1), "+z": (0, 0, 1), "bottom": (0, 0, -1), "-z": (0, 0, -1),
              "right": (1, 0, 0), "+x": (1, 0, 0), "left": (-1, 0, 0), "-x": (-1, 0, 0),
              "back": (0, 1, 0), "+y": (0, 1, 0), "front": (0, -1, 0), "-y": (0, -1, 0)}


def _text_plane(part: Part, face: "str | bd.Face | bd.Plane") -> bd.Plane:
    """Plane on ``face`` with z = outward normal and x chosen so text reads upright on side walls."""
    if isinstance(face, bd.Plane):
        return face
    if isinstance(face, str):
        normal = _FACE_DIRS.get(face.strip().lower())
        if normal is None:
            raise ValidationError(f"face {face!r} must be a planar Face, a Plane or one of {sorted(_FACE_DIRS)}")
        bb = part.bounding_box()
        lo, hi = (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)
        origin = [(a + b) / 2 for a, b in zip(lo, hi)]
        axis = next(i for i, v in enumerate(normal) if v)
        origin[axis] = hi[axis] if normal[axis] > 0 else lo[axis]
        z = bd.Vector(*normal)
    elif isinstance(face, bd.Face):
        try:
            z = bd.Plane(face).z_dir  # outward normal (face orientation respected)
        except Exception as exc:
            raise ValidationError(f"face must be planar to carry text: {exc}") from exc
        origin = face.center()
    else:
        raise ValidationError(f"face must be a planar Face, a Plane or a side name, got {type(face).__name__}")
    x = bd.Vector(1, 0, 0) if abs(z.Z) > 0.999 else bd.Vector(0, 0, 1).cross(z).normalized()
    return bd.Plane(origin=origin, x_dir=x, z_dir=z)


def _placed_text(part: Part, text: str, face: "str | bd.Face | bd.Plane", z0: float, height: float,
                 size: float, at: tuple[float, float], rotation: float, font: str, bold: bool) -> Part:
    """Text spanning plane-local z ∈ [z0, z0 + height], moved onto ``face`` of ``part``."""
    solid = text_solid(text, size, height, font=font, bold=bold)
    plane = _text_plane(part, face)
    local = Location((float(at[0]), float(at[1]), z0)) * Location((0, 0, 0), (0, 0, float(rotation)))
    return solid.moved(plane.location * local)


def emboss(part: Part, text: str, *, face: "str | bd.Face | bd.Plane" = "top", size: float = 6.0,
           height: float = 0.6, at: tuple[float, float] = (0.0, 0.0), rotation: float = 0.0,
           font: str = "Arial", bold: bool = True) -> Part:
    """Raise ``text`` exactly ``height`` mm out of ``face`` of ``part`` and fuse it on (watertight).

    ``face``: ``"top"``/``"bottom"``/``"front"`` (−y)/``"back"`` (+y)/``"left"`` (−x)/``"right"``
    (or ``"+z"``, ``"-y"``…) = that side of the part's bounding box, text centred on it; a planar
    build123d ``Face`` (text centred on the face, along its outward normal); or a ``Plane``. ``at``
    is an (x, y) offset in the face's 2D frame (on side walls x is horizontal and y points up, so
    text reads upright), ``rotation`` turns the text about the face normal (degrees). The text
    starts :data:`EPS` below the face so the union never has coplanar faces.
    """
    _require_positive(size=size, height=height)
    return _as_part(part + _placed_text(part, text, face, -EPS, height + EPS, size, at, rotation, font, bold))


def engrave(part: Part, text: str, *, face: "str | bd.Face | bd.Plane" = "top", size: float = 6.0,
            depth: float = 0.6, at: tuple[float, float] = (0.0, 0.0), rotation: float = 0.0,
            font: str = "Arial", bold: bool = True) -> Part:
    """Cut ``text`` ``depth`` mm into ``face`` of ``part`` (watertight); arguments as :func:`emboss`.

    The cutter pokes :data:`EPS` out of the face so the cut never leaves a zero-thickness skin.
    Raises :class:`ValidationError` when ``depth`` is not smaller than the part's bounding-box
    extent along the face normal (the text would cut straight through).
    """
    _require_positive(size=size, depth=depth)
    plane = _text_plane(part, face)
    bb = part.bounding_box()
    n = plane.z_dir
    extent = (abs(n.X) * (bb.max.X - bb.min.X) + abs(n.Y) * (bb.max.Y - bb.min.Y)
              + abs(n.Z) * (bb.max.Z - bb.min.Z))
    if depth >= extent - _TOL:
        raise ValidationError(f"engrave depth {depth} mm must be < the part's {extent:.3g} mm thickness there")
    return _as_part(part - _placed_text(part, text, plane, -depth, depth + EPS, size, at, rotation, font, bold))


def _bottom_edges(part: Part, z0: float, tol: float = 1e-4) -> list[bd.Edge]:
    out = []
    for e in part.edges():
        bb = e.bounding_box()
        if abs(bb.min.Z - z0) < tol and abs(bb.max.Z - z0) < tol:
            out.append(e)
    return out


def chamfer_bottom(part: Part, size: float = 0.4) -> Part:
    """Chamfer every edge lying in the lowest plane (z = min z) by ``size`` mm.

    Counters elephant's foot: the first layers squish outwards, a 0.3–0.5 mm chamfer keeps the
    printed footprint at its designed size. Falls back to the outer boundary only when the kernel
    cannot chamfer inner edges; raises :class:`PiForgeError` if neither works.
    """
    _require_positive(size=size)
    part = _as_part(part)
    z0 = part.bounding_box().min.Z
    edges = _bottom_edges(part, z0)
    if not edges:
        raise ValidationError("chamfer_bottom: the part has no edges in its bottom plane")
    try:
        out = _as_part(part.chamfer(size, None, edges))
        if out.is_valid:
            return out
    except Exception as exc:  # noqa: BLE001 - retry with fewer edges below
        log.debug("chamfer_bottom: full chamfer failed (%s), retrying outer boundary only", exc)
    outer: list[bd.Edge] = []
    for face in part.faces():
        bb = face.bounding_box()
        if abs(bb.min.Z - z0) < 1e-4 and abs(bb.max.Z - z0) < 1e-4:
            outer.extend(face.outer_wire().edges())
    try:
        out = _as_part(part.chamfer(size, None, outer))
        if out.is_valid:
            log.warning("chamfer_bottom: chamfered only the outer bottom boundary (inner edges failed)")
            return out
    except Exception as exc:
        raise PiForgeError(
            f"chamfer_bottom: the CAD kernel could not chamfer the bottom edges by {size} mm ({exc}); "
            "try a smaller size") from exc
    raise PiForgeError(f"chamfer_bottom: chamfer by {size} mm produced an invalid solid; try a smaller size")
