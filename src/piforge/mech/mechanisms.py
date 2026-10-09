"""Printable mechanisms: snap-fit cantilever (with strain check), hinge, cable clip, DIN-rail clip,
funnel, chute, bearing seat, servo mount, shaft coupler.

Every function returns a single watertight build123d ``Part`` (mm, Z up, bottom on z = 0 unless
stated) designed to print in the orientation it is modelled in. Holes include the generic
printer's hole compensation (pass ``printer=`` where offered for another profile).
"""

from __future__ import annotations

import math

import build123d as bd
from build123d import Align, Location, Part

from piforge.core.errors import NotFoundError, ValidationError
from piforge.core.report import Report, Severity
from piforge.fab.profiles import get_material, get_printer
from piforge.mech.fasteners import get_size, hole_compensation
from piforge.mech.primitives import _as_part

EPS = 0.01
_MIN = (Align.CENTER, Align.CENTER, Align.MIN)

# src: ISO 15 / SKF catalogue deep-groove ball bearings: (bore d, outer D, width B) mm
BEARINGS: dict[str, tuple[float, float, float]] = {
    "608": (8.0, 22.0, 7.0),
    "625": (5.0, 16.0, 5.0),
    "623": (3.0, 10.0, 4.0),
    "688": (8.0, 16.0, 5.0),
    "6000": (10.0, 26.0, 8.0),
}


def _pos(**values: float) -> None:
    for k, v in values.items():
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            raise ValidationError(f"{k} must be a positive number of mm, got {v!r}")


def _part(shape) -> Part:
    """Wrap a build123d result as a compound-backed Part (correct ``.volume``)."""
    return _as_part(shape)


def _prism_xy(points: list[tuple[float, float]], depth: float) -> Part:
    """Polygon in the XY plane extruded along +Z by ``depth``."""
    face = bd.Face(bd.Wire.make_polygon([bd.Vector(x, y, 0) for x, y in points], close=True))
    return _part(bd.extrude(face, depth, dir=(0, 0, 1)))


def _cyl(d: float, h: float, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Part:
    return _part(bd.Cylinder(d / 2, h, align=_MIN).moved(Location((x, y, z))))


# ----------------------------------------------------------------------------------------------
# snap fit
# ----------------------------------------------------------------------------------------------
def snap_fit_strain(length: float, thickness: float, deflection: float) -> float:
    """Peak strain of a constant-section cantilever snap: ε = 1.5·t·y / L² (fraction)."""
    _pos(length=length, thickness=thickness, deflection=deflection)
    return 1.5 * thickness * deflection / length ** 2


def snap_fit_cantilever(length: float, thickness: float, width: float, deflection: float, *,
                        material: str = "PLA", hook: tuple[float, float] | None = None) -> tuple[Part, Report]:
    """Cantilever snap hook and its strain check.

    Geometry (prints flat, bending in the layer plane): anchor block x ∈ [−4, 0], beam x ∈ [0, L]
    of thickness ``t`` along Y and ``width`` along Z; at the tip a hook protruding +Y by the
    hook depth (default = ``deflection``) with a 90° retention face and a ~30° lead-in ramp
    (``hook`` = (depth, ramp length)). Report: ``SNAP.STRAIN`` INFO when ε ≤ the material's
    allowable strain, WARNING up to 1.5×, ERROR above (data: strain, allowable, force_n).
    """
    _pos(length=length, thickness=thickness, width=width, deflection=deflection)
    mat = get_material(material)
    eps = snap_fit_strain(length, thickness, deflection)
    allow = mat.allowable_strain
    # deflection force P = w·t²·E·ε / (6·L)
    force = width * thickness ** 2 * mat.youngs_modulus_mpa * eps / (6 * length)
    rep = Report(title="snap-fit")
    data = dict(strain=eps, allowable=allow, material=mat.name, force_n=force, length=length,
                thickness=thickness, deflection=deflection)
    if eps <= allow + 1e-12:
        rep.add("SNAP.STRAIN", Severity.INFO,
                f"Snap strain {100 * eps:.2f} % ≤ {100 * allow:.1f} % allowed for {mat.name} "
                f"(deflection force ≈ {force:.1f} N).", "snap", **data)
    else:
        sev = Severity.WARNING if eps <= 1.5 * allow + 1e-12 else Severity.ERROR
        rep.add("SNAP.STRAIN", sev,
                f"Snap strain {100 * eps:.2f} % exceeds {100 * allow:.1f} % allowed for {mat.name}"
                + (" — fine for a one-time assembly only." if sev == Severity.WARNING else " — it will crack."),
                "snap", hint="Make the beam longer (ε ∝ 1/L²), thinner, or the hook shallower; "
                             "PETG tolerates more strain than PLA.", **data)
    depth, ramp = hook if hook is not None else (deflection, deflection / math.tan(math.radians(30)))
    _pos(hook_depth=depth, hook_ramp=ramp)
    if ramp + 1.0 > length:
        raise ValidationError("hook ramp is longer than the beam")
    t, L, anchor = thickness, length, 4.0
    # ramp from the tip (L, t) up to the crest; vertical retention face at x = L − ramp − 1
    clean = [(-anchor, -2.0), (0.0, -2.0), (0.0, 0.0), (L, 0.0), (L, t), (L - ramp, t + depth),
             (L - ramp - 1.0, t + depth), (L - ramp - 1.0, t), (0.0, t), (0.0, t + 2.0), (-anchor, t + 2.0)]
    return _prism_xy(clean, width), rep


# ----------------------------------------------------------------------------------------------
# hinge
# ----------------------------------------------------------------------------------------------
def hinge(length: float, pin_d: float = 3.0, knuckles: int = 5, clearance: float = 0.3, *,
          leaf_width: float = 12.0, leaf_t: float = 2.0) -> tuple[Part, Part]:
    """Print-in-place two-leaf hinge along X (axis at y = 0, z = knuckle radius).

    Leaf A (−Y side) owns the odd knuckles and a solid pin Ø ``pin_d`` running through all of
    them; leaf B (+Y side) owns the even knuckles, bored Ø pin_d + 2·clearance. Knuckles are
    separated axially by ``clearance``; leaves stay ``clearance`` away from the other leaf's
    knuckles. Returns (leaf_a, leaf_b) in the assembled (and printing) position.

    For print-in-place use ``clearance=0.4``–``0.5`` mm: the default 0.3 mm is the radial gap of
    a well-tuned printer and often fuses on generic FDM machines (first layers squish, the pin's
    underside sags). Keep 0.3 mm only for hinges assembled from separately printed leaves or
    printers calibrated for tight sliding fits.
    """
    _pos(length=length, pin_d=pin_d, clearance=clearance, leaf_width=leaf_width, leaf_t=leaf_t)
    k = int(knuckles)
    if k < 2:
        raise ValidationError("a hinge needs at least 2 knuckles")
    seg = (length - (k - 1) * clearance) / k
    if seg < 2.0:
        raise ValidationError(f"{k} knuckles do not fit in {length} mm (segments < 2 mm)")
    od = pin_d + 2 * clearance + 2 * 1.6
    r = od / 2
    if leaf_t > od:
        raise ValidationError("leaf thickness must not exceed the knuckle diameter")
    zc = r

    def knuckle(i: int) -> Part:
        x0 = i * (seg + clearance)
        c = bd.Cylinder(r, seg, rotation=(0, 90, 0), align=_MIN)
        return _part(c.moved(Location((x0, 0, zc))))

    def segment_box(i: int, y0: float, y1: float) -> Part:
        x0 = i * (seg + clearance)
        b = bd.Box(seg, y1 - y0, leaf_t, align=(Align.MIN, Align.MIN, Align.MIN))
        return _part(b.moved(Location((x0, y0, 0))))

    def leaf(sign: int, mine: list[int]) -> Part:
        gap = r + clearance
        y_lo, y_hi = (-(gap + leaf_width), -gap) if sign < 0 else (gap, gap + leaf_width)
        plate = _part(bd.Box(length, y_hi - y_lo, leaf_t, align=(Align.MIN, Align.MIN, Align.MIN))
                      .moved(Location((0, y_lo, 0))))
        out = plate
        for i in mine:
            bridge = segment_box(i, -gap - EPS, 0.0) if sign < 0 else segment_box(i, 0.0, gap + EPS)
            out = out + knuckle(i) + bridge
        return _part(out)

    a_idx = [i for i in range(k) if i % 2 == 0]
    b_idx = [i for i in range(k) if i % 2 == 1]
    a = leaf(-1, a_idx)
    b = leaf(+1, b_idx)
    pin = _part(bd.Cylinder(pin_d / 2, length, rotation=(0, 90, 0), align=_MIN).moved(Location((0, 0, zc))))
    a = _part(a + pin)
    bore = _part(bd.Cylinder(pin_d / 2 + clearance, length + 2 * EPS, rotation=(0, 90, 0), align=_MIN)
                 .moved(Location((-EPS, 0, zc))))
    b = _part(b - bore)
    return a, b


# ----------------------------------------------------------------------------------------------
# clips
# ----------------------------------------------------------------------------------------------
def cable_clip(cable_d: float, *, width: float = 8.0, base: bool = True, wall: float = 1.6,
               screw: str = "M3") -> Part:
    """Snap-in cable clip: C-ring around a Ø ``cable_d`` cable (opening 0.75·d at the top),
    optionally on a base tab with a ``screw`` clearance hole. Profile in XY, extruded ``width``
    along Z (prints flat on its side)."""
    _pos(cable_d=cable_d, width=width, wall=wall)
    ri, ro = cable_d / 2, cable_d / 2 + wall
    ring = bd.Circle(ro) - bd.Circle(ri)
    opening = 0.75 * cable_d
    cut = bd.Rectangle(opening, ro + 1, align=(Align.CENTER, Align.MIN))
    sk = ring - cut.moved(Location((0, 0)))
    clip = _part(bd.extrude(sk, width, dir=(0, 0, 1)).moved(Location((0, ro, 0))))
    if base:
        s = get_size(screw)
        tab_w = s.head_socket_d + 4.0
        tab = _part(bd.Box(2 * ro + 2 * tab_w, wall + 0.4, width, align=(Align.CENTER, Align.MIN, Align.MIN))
                    .moved(Location((0, 0, 0))))
        clip = _part(clip + tab)
        hole_d = s.clearance_normal + hole_compensation()
        for sx in (-1, 1):
            hole = bd.Cylinder(hole_d / 2, wall + 0.4 + 2 * EPS, rotation=(-90, 0, 0), align=_MIN)
            clip = _part(clip - hole.moved(Location((sx * (ro + tab_w / 2), -EPS, width / 2))))
    return clip


def din_rail_clip(width: float = 10.0) -> Part:
    """Clip for a 35 mm top-hat DIN rail (EN 60715: 35 × 7.5 mm, 1 mm flanges).

    Profile in XY extruded ``width`` along Z: a 3 mm back plate spanning the rail, a fixed hook
    at −X and a flexible spring hook at +X (slot behind it). Mount your part on the +Y face.
    """
    _pos(width=width)
    rail, flange_t = 35.0, 1.0  # src: EN 60715 top-hat rail TH35 (35 mm wide, 1 mm flanges)
    plate_t, lip, leg = 3.0, 1.2, 2.0
    half = rail / 2 + 0.3  # 0.3 mm clearance per side
    y_lip = -(flange_t + 0.4)  # hooks grip 0.4 mm below the flange

    def box(x0: float, x1: float, y0: float, y1: float, z0: float = 0.0, z1: float = width) -> Part:
        b = bd.Box(x1 - x0, y1 - y0, z1 - z0, align=(Align.MIN, Align.MIN, Align.MIN))
        return _part(b.moved(Location((x0, y0, z0))))

    clip = box(-half - leg, half + leg, 0.0, plate_t)
    for sx in (-1, 1):
        x_out = sx * (half + leg)
        clip = _part(clip + box(min(sx * half, x_out), max(sx * half, x_out), y_lip - 2.0, EPS))
        x_in = sx * (half - lip)
        clip = _part(clip + box(min(sx * half, x_in), max(sx * half, x_in) + (EPS if sx < 0 else 0),
                                y_lip - 2.0, y_lip))
    # spring: a slot in the plate beside the +X hook so it flexes outwards (1 mm web left on top)
    clip = _part(clip - box(half - 3.0, half - 1.8, -EPS, plate_t - 1.0, -EPS, width + EPS))
    return clip


# ----------------------------------------------------------------------------------------------
# funnel, chute
# ----------------------------------------------------------------------------------------------
def funnel(top_d: float, bottom_d: float, height: float, wall: float = 1.6) -> Part:
    """Conical funnel: outer Ø ``bottom_d`` at z = 0 → Ø ``top_d`` at z = height, wall ``wall``
    measured normal to the cone surface. Prints narrow end down."""
    _pos(top_d=top_d, bottom_d=bottom_d, height=height, wall=wall)
    half_angle = math.atan2(abs(top_d - bottom_d) / 2, height)
    w = wall / math.cos(half_angle)  # horizontal wall for the requested normal thickness
    if min(top_d, bottom_d) / 2 <= w + 0.5:
        raise ValidationError("funnel outlet too small for its wall thickness")
    outer = bd.Cone(bottom_d / 2, top_d / 2, height, align=_MIN)
    # extend the inner cone by EPS beyond both faces along the same slope
    slope = (top_d - bottom_d) / 2 / height
    rb = bottom_d / 2 - w - slope * EPS
    rt = top_d / 2 - w + slope * EPS
    inner = bd.Cone(rb, rt, height + 2 * EPS, align=_MIN).moved(Location((0, 0, -EPS)))
    return _part(outer - inner)


def chute(length: float, width: float, wall_h: float, *, thickness: float = 1.6, angle: float = 0.0) -> Part:
    """U-channel chute along X: floor ``width`` × ``thickness``, side walls ``wall_h`` above the
    floor. ``angle`` tilts it (degrees, about Y through the origin, lower end at +X) for use in an
    assembly — print it untilted (angle = 0)."""
    _pos(length=length, width=width, wall_h=wall_h, thickness=thickness)
    outer_w = width + 2 * thickness
    pts = [(-outer_w / 2, 0), (outer_w / 2, 0), (outer_w / 2, thickness + wall_h),
           (width / 2, thickness + wall_h), (width / 2, thickness), (-width / 2, thickness),
           (-width / 2, thickness + wall_h), (-outer_w / 2, thickness + wall_h)]
    # profile in YZ, extruded along X
    face = bd.Face(bd.Wire.make_polygon([bd.Vector(0, y, z) for y, z in pts], close=True))
    out = _part(bd.extrude(face, length, dir=(1, 0, 0)))
    if angle:
        out = _part(out.rotate(bd.Axis.Y, float(angle)))
    return out


# ----------------------------------------------------------------------------------------------
# bearing seat, servo mount, coupler
# ----------------------------------------------------------------------------------------------
def bearing_seat(bearing: str = "608", *, wall: float = 2.5, press: bool = True, printer: str = "generic") -> Part:
    """Housing for a deep-groove ball bearing: pocket Ø D (press or slide fit) on a 1.2 mm lip
    (Ø D − 3: carries the outer race only) and a flange with two M3 clearance holes.
    Bearing opening at the top; z ∈ [0, B + 1.2]."""
    key = str(bearing).strip()
    if key not in BEARINGS:
        raise NotFoundError("bearing", bearing, BEARINGS)
    _pos(wall=wall)
    d, D, B = BEARINGS[key]
    prof = get_printer(printer)
    comp = prof.hole_compensation
    pocket = D + comp - (2 * prof.clearance_press - 0.1 if press else -2 * prof.clearance_sliding)
    lip = 1.2
    h = B + lip
    od = D + 2 * wall
    body = _cyl(od, h)
    s = get_size("M3")
    flange_l = od + 2 * (s.head_socket_d + 2.0)
    flange = _part(bd.Box(flange_l, od * 0.6, lip + 1.0, align=_MIN))
    body = _part(body + flange)
    body = _part(body - _cyl(pocket, B + EPS, z=lip))
    body = _part(body - _cyl(D - 3.0 + comp, lip + 2 * EPS, z=-EPS))
    for sx in (-1, 1):
        x = sx * (od / 2 + s.head_socket_d / 2 + 1.0)
        body = _part(body - _cyl(s.clearance_normal + comp, lip + 1.0 + 2 * EPS, x=x, z=-EPS))
    return body


_SERVOS = {"sg90": "sg90_servo", "mg996r": "mg996r_servo"}


def servo_mount(servo: str = "sg90", *, plate: float = 3.0, printer: str = "generic") -> Part:
    """Flat plate holding a servo by its tabs: rectangular pocket for the body (+0.3 mm per
    side) and pilot holes for the tab screws (self-tapping, Ø = 0.8 × tab hole). The servo drops
    in from the top (+Z); plate z ∈ [0, plate]."""
    from piforge.mech.modules import get_module

    key = _SERVOS.get(str(servo).strip().lower(), servo)
    m = get_module(key)
    _pos(plate=plate)
    body = m.component("body")[3]
    tabs = m.component("tabs")[3]
    comp = get_printer(printer).hole_compensation
    L, W = tabs[0] + 10.0, max(tabs[1], body[1]) + 10.0
    out = _part(bd.Box(L, W, plate, align=_MIN))
    pocket = bd.Box(body[0] + 0.6, body[1] + 0.6, plate + 2 * EPS, align=_MIN)
    bx, by = m.component("body")[2][:2]
    out = _part(out - pocket.moved(Location((bx, by, -EPS))))
    for x, y in m.holes:
        out = _part(out - _cyl(0.8 * m.hole_d + comp, plate + 2 * EPS, x=x, y=y, z=-EPS))
    return out


def shaft_coupler(d1: float, d2: float, length: float, *, wall: float = 4.0, set_screw: str = "M3",
                  printer: str = "generic") -> Part:
    """Rigid coupler: cylinder (Ø max(d1, d2) + 2·wall) with a Ø d1 bore in the lower half and a
    Ø d2 bore in the upper half, plus a radial tapped hole for a ``set_screw`` in each half.
    Axis Z, z ∈ [0, length]."""
    _pos(d1=d1, d2=d2, length=length, wall=wall)
    comp = get_printer(printer).hole_compensation
    od = max(d1, d2) + 2 * wall
    half = length / 2
    out = _cyl(od, length)
    out = _part(out - _cyl(d1 + comp, half + EPS, z=-EPS))
    out = _part(out - _cyl(d2 + comp, half + EPS, z=half))
    s = get_size(set_screw)
    for z in (half / 2, half + half / 2):
        hole = bd.Cylinder((s.tap_plastic + comp) / 2, od / 2 + EPS, rotation=(0, 90, 0), align=_MIN)
        out = _part(out - hole.moved(Location((0, 0, z))))
    return out
