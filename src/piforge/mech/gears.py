"""Involute spur gears and racks for 3D printing, plus a 2D mesh-interference check.

Tooth profiles are *generated* the way a hobbing machine does it: an ISO 53 basic rack (pressure
angle α, addendum 1·m, dedendum (1 + clearance)·m) rolls along the pitch circle and its swept area
is removed from the blank. That yields exact involute flanks plus the trochoidal undercut of
small pinions, so generated gears mesh without interference down to few teeth.

Backlash is split evenly: each gear's tooth is thinned by ``backlash / 2`` at the pitch circle.
Gears: axis Z, centred on the origin, z ∈ [0, thickness (+ hub_h)], tooth 0 centred on +X.
2D profiles are shapely polygons (fast); solids are build123d Parts (lazy import).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from shapely import affinity
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from piforge.core.errors import ValidationError
from piforge.core.report import Report, Severity

if TYPE_CHECKING:  # pragma: no cover
    from build123d import Location, Part

EPS = 0.01
_SIMPLIFY = 0.002  # mm — profile polygon simplification tolerance


def _check(module: float, teeth: int, pressure_angle: float) -> None:
    if not (isinstance(module, (int, float)) and math.isfinite(module) and module > 0):
        raise ValidationError(f"module must be > 0 mm, got {module!r}")
    if int(teeth) != teeth or teeth < 5:
        raise ValidationError(f"a gear needs an integer number of teeth >= 5, got {teeth!r}")
    if not 10.0 <= pressure_angle <= 30.0:
        raise ValidationError(f"pressure_angle must be 10–30°, got {pressure_angle!r}")


def gear_geometry(module: float, teeth: int, pressure_angle: float = 20.0, profile_shift: float = 0.0,
                  *, clearance: float = 0.25) -> dict:
    """Standard spur-gear dimensions (mm): pitch_d, base_d, tip_d, root_d, circular_pitch,
    tooth_thickness (at the pitch circle, no backlash), plus module/teeth/pressure_angle.

    ``profile_shift`` x (in modules) moves the generating rack outwards: tip and root grow by
    2·x·m and the tooth thickens by 2·x·m·tan α.
    """
    _check(module, teeth, pressure_angle)
    m, z, a = float(module), int(teeth), math.radians(pressure_angle)
    d = m * z
    return {
        "module": m, "teeth": z, "pressure_angle": float(pressure_angle), "profile_shift": float(profile_shift),
        "pitch_d": d,
        "base_d": d * math.cos(a),
        "tip_d": d + 2 * m * (1 + profile_shift),
        "root_d": d - 2 * m * (1 + clearance - profile_shift),
        "circular_pitch": math.pi * m,
        "tooth_thickness": math.pi * m / 2 + 2 * profile_shift * m * math.tan(a),
    }


def center_distance(module: float, z1: int, z2: int) -> float:
    """Nominal centre distance m·(z1 + z2)/2 of two unshifted external gears, mm."""
    return float(module) * (int(z1) + int(z2)) / 2.0


def _rack_tooth(m: float, alpha: float, thickness_ref: float, y_ref: float, clearance: float) -> np.ndarray:
    """One cutter tooth (trapezoid, pointing to −y) centred on x = 0; reference line at y_ref."""
    t = math.tan(alpha)
    y_tip, y_top = y_ref - (1 + clearance) * m, y_ref + 1.5 * m
    w_tip = thickness_ref / 2 - (1 + clearance) * m * t
    w_top = thickness_ref / 2 + 1.5 * m * t
    if w_tip <= 0:
        raise ValidationError("backlash/profile shift leave no cutter tip — reduce them")
    return np.array([(-w_tip, y_tip), (w_tip, y_tip), (w_top, y_top), (-w_top, y_top)])


def gear_profile(module: float, teeth: int, *, pressure_angle: float = 20.0, profile_shift: float = 0.0,
                 backlash: float = 0.1, clearance: float = 0.25, steps: int | None = None) -> Polygon:
    """2D gear outline (shapely Polygon), centred on the origin, tooth 0 centred on +X."""
    g = gear_geometry(module, teeth, pressure_angle, profile_shift, clearance=clearance)
    if backlash < 0:
        raise ValidationError("backlash must be >= 0 mm")
    m, z, a = g["module"], g["teeth"], math.radians(pressure_angle)
    rp, r_tip = g["pitch_d"] / 2, g["tip_d"] / 2
    s_gear = g["tooth_thickness"] - backlash / 2
    if s_gear <= 0.2 * m:
        raise ValidationError(f"backlash {backlash} mm leaves almost no tooth")
    t_cut = g["circular_pitch"] - s_gear  # cutter tooth = gear tooth space
    y_ref = rp + profile_shift * m
    tooth = _rack_tooth(m, a, t_cut, y_ref, clearance)
    y_min = tooth[:, 1].min()
    w_max = np.abs(tooth[:, 0]).max()
    reach = w_max + math.sqrt(max(r_tip ** 2 - y_min ** 2, 0.0)) + m  # rack travel until it clears
    phi_max = reach / rp
    n = steps or max(120, int(reach / (0.01 * m)))  # ≈0.01·m rack travel per step
    polys = []
    for phi in np.linspace(-phi_max, phi_max, 2 * n + 1):
        c, s = math.cos(-phi), math.sin(-phi)
        x = tooth[:, 0] - rp * phi  # rack translation while the gear turns by phi
        y = tooth[:, 1]
        polys.append(Polygon(np.column_stack([c * x - s * y, s * x + c * y])))
    gap = unary_union(polys)  # one tooth space, centred on +Y
    gaps = unary_union([affinity.rotate(gap, 360.0 * k / z, origin=(0, 0)) for k in range(z)])
    blank = Point(0, 0).buffer(r_tip, quad_segs=max(64, 8 * z))
    gear = blank.difference(gaps)
    if gear.geom_type != "Polygon":  # numerical crumbs: keep the main body
        gear = max(gear.geoms, key=lambda p: p.area)
    # tooth 0: tooth centres sit half a pitch from the gap at +Y → rotate onto +X
    gear = affinity.rotate(gear, -(90.0 + 180.0 / z), origin=(0, 0))
    return gear.simplify(_SIMPLIFY * m, preserve_topology=True)


def _polygon_to_face(poly: Polygon):
    import build123d as bd

    pts = [(float(x), float(y)) for x, y in poly.exterior.coords[:-1]]
    face = bd.Face(bd.Wire.make_polygon([bd.Vector(x, y, 0) for x, y in pts], close=True))
    for ring in poly.interiors:
        hole = bd.Face(bd.Wire.make_polygon([bd.Vector(float(x), float(y), 0) for x, y in ring.coords[:-1]],
                                            close=True))
        face = face - hole
    return face


def spur_gear(module: float, teeth: int, thickness: float, *, pressure_angle: float = 20.0,
              backlash: float = 0.1, bore: float | None = None, hub_d: float | None = None,
              hub_h: float = 0.0, clearance: float = 0.25, profile_shift: float = 0.0) -> "Part":
    """Printable involute spur gear: axis Z, z ∈ [0, thickness + hub_h], tooth 0 on +X.

    ``bore`` is the designed hole Ø (add the printer's hole compensation yourself); ``hub_d`` /
    ``hub_h`` add a cylindrical hub on top (hub_d defaults to 2 × bore).
    """
    import build123d as bd

    from piforge.mech.primitives import _as_part

    if not thickness > 0:
        raise ValidationError("thickness must be > 0 mm")
    if hub_h < 0:
        raise ValidationError("hub_h must be >= 0 mm")
    g = gear_geometry(module, teeth, pressure_angle, profile_shift, clearance=clearance)
    poly = gear_profile(module, teeth, pressure_angle=pressure_angle, profile_shift=profile_shift,
                        backlash=backlash, clearance=clearance)
    body = bd.extrude(_polygon_to_face(poly), thickness, dir=(0, 0, 1))
    if hub_h > 0:
        hd = hub_d if hub_d is not None else 2.0 * (bore or g["root_d"] / 4)
        if hd >= g["root_d"]:
            raise ValidationError(f"hub_d {hd} mm must be smaller than the root Ø {g['root_d']:.2f} mm")
        body = body + bd.Cylinder(hd / 2, hub_h + EPS, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)
                                  ).moved(bd.Location((0, 0, thickness - EPS)))
    if bore:
        if bore >= g["root_d"] - 2 * module:
            raise ValidationError(f"bore {bore} mm leaves too little rim under the teeth")
        h = thickness + hub_h
        body = body - bd.Cylinder(bore / 2, h + 2 * EPS, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)
                                  ).moved(bd.Location((0, 0, -EPS)))
    return _as_part(body)


def rack_profile(module: float, length: float, height: float, *, pressure_angle: float = 20.0,
                 backlash: float = 0.1, clearance: float = 0.25) -> Polygon:
    """2D rack outline: x ∈ [0, length], body from y = 0, pitch line at y = ``height``,
    teeth tips at height + m; a tooth is centred on every x = (k + ½)·π·m."""
    if not (length > 0 and height > (1 + clearance) * module + 0.8):
        raise ValidationError(f"rack needs length > 0 and height > dedendum + 0.8 mm "
                              f"({(1 + clearance) * module + 0.8:.2f} mm)")
    m, a = float(module), math.radians(pressure_angle)
    p = math.pi * m
    s = p / 2 - backlash / 2
    t = math.tan(a)
    y_root, y_tip = height - (1 + clearance) * m, height + m
    w_root, w_tip = s / 2 + (1 + clearance) * m * t, s / 2 - m * t
    if w_tip <= 0:
        raise ValidationError("rack teeth come to a point — reduce backlash")
    teeth = []
    for k in range(int(length / p) + 2):
        xc = (k + 0.5) * p
        teeth.append(Polygon([(xc - w_root, y_root - EPS), (xc + w_root, y_root - EPS),
                              (xc + w_tip, y_tip), (xc - w_tip, y_tip)]))
    body = Polygon([(0, 0), (length, 0), (length, y_root), (0, y_root)])
    out = unary_union([body, *teeth]).intersection(Polygon([(0, -1), (length, -1), (length, y_tip + 1),
                                                            (0, y_tip + 1)]))
    if out.geom_type != "Polygon":
        out = max(out.geoms, key=lambda q: q.area)
    return out


def rack(module: float, length: float, thickness: float, height: float, *, pressure_angle: float = 20.0,
         backlash: float = 0.1) -> "Part":
    """Printable rack: teeth along X pointing +Y, pitch line at y = ``height``, z ∈ [0, thickness].

    A mating gear of z teeth sits with its centre at y = height + m·z/2 and must be phased along
    X — use :func:`rack_mesh_location` (and :func:`check_rack_mesh`).
    """
    import build123d as bd

    from piforge.mech.primitives import _as_part

    if not thickness > 0:
        raise ValidationError("thickness must be > 0 mm")
    poly = rack_profile(module, length, height, pressure_angle=pressure_angle, backlash=backlash)
    return _as_part(bd.extrude(_polygon_to_face(poly), thickness, dir=(0, 0, 1)))


def rack_mesh_location(module: float, teeth: int, rack_height: float, x_near: float = 0.0) -> "Location":
    """Where to put an (unrotated, as generated) gear so it meshes with :func:`rack` / :func:`rack_profile`.

    The rack (same module, generated at x = 0) has a tooth space centred on every x = k·p
    (p = π·m). The gear centre goes to y = rack_height + m·z/2 and to the x nearest ``x_near`` at
    which its lowest tooth sits in a space: z ≡ 0 (mod 4) → x = k·p (a tooth points straight
    down); z ≡ 2 (mod 4) → x = (k + ½)·p (a space points down, onto a rack tooth); any other z →
    x = k·p + r·δ, δ being the angle (rad) from the nearest tooth to straight down (rolling).
    Returns a build123d ``Location`` (translation only).
    """
    from build123d import Location

    g = gear_geometry(module, teeth)
    m, z = g["module"], g["teeth"]
    p, r = g["circular_pitch"], g["pitch_d"] / 2
    pitch_deg = 360.0 / z
    k = round(270.0 / pitch_deg)  # tooth k of the generated gear sits at k·360/z degrees
    delta = math.radians(270.0 - k * pitch_deg)
    x0 = r * delta
    n = round((float(x_near) - x0) / p)
    return Location((x0 + n * p, float(rack_height) + m * z / 2, 0.0))


def check_rack_mesh(module: float, teeth: int, rack_height: float, gear_x: float | None = None, *,
                    length: float | None = None, thickness: float = 5.0, steps: int = 8,
                    backlash: float = 0.1, extra_distance: float = 0.0, pressure_angle: float = 20.0) -> Report:
    """Roll a generated gear one tooth pitch along a generated rack and look for overlap.

    Rack as :func:`rack_profile` (x ∈ [0, length], pitch line at y = ``rack_height``); gear centre
    at x = ``gear_x`` (default: :func:`rack_mesh_location` near the middle of the rack) and
    y = rack_height + m·z/2 + ``extra_distance``. Emits ``GEAR.INTERFERENCE`` (ERROR) or ``GEAR.OK``.
    """
    if steps < 1:
        raise ValidationError("steps must be >= 1")
    g = gear_geometry(module, teeth, pressure_angle)
    m, z = g["module"], g["teeth"]
    p, r = g["circular_pitch"], g["pitch_d"] / 2
    if length is None:
        length = g["tip_d"] + 4 * p
    if gear_x is None:
        gear_x = rack_mesh_location(m, z, rack_height, x_near=length / 2).position.X
    rk = rack_profile(m, length, rack_height, pressure_angle=pressure_angle, backlash=backlash)
    gp = gear_profile(m, z, pressure_angle=pressure_angle, backlash=backlash)
    yc = rack_height + r + extra_distance
    hits: list[float] = []
    worst = 0.0
    for i in range(steps):
        th = 360.0 / z * i / steps  # gear turns clockwise by th while rolling +x by r·th
        q = affinity.translate(affinity.rotate(gp, -th, origin=(0, 0)), gear_x + math.radians(th) * r, yc)
        area = q.intersection(rk).area
        if area > 1e-4:
            hits.append(round(th, 4))
            worst = max(worst, area)
    rep = Report(title=f"rack mesh m{m:g} z{z}")
    subject = f"gears:m{m:g}:{z}xrack"
    if hits:
        rep.add("GEAR.INTERFERENCE", Severity.ERROR,
                f"Gear m{m:g} z{z} at x = {gear_x:.3f} mm overlaps the rack by up to {worst * thickness:.2f} mm³ "
                f"at {len(hits)}/{steps} positions.", subject,
                hint="Place the gear with rack_mesh_location() (tooth in a rack space) at the nominal height.",
                angles=hits, max_overlap_mm3=worst * thickness, gear_x=gear_x)
    else:
        alpha = math.radians(pressure_angle)
        ra, rb = g["tip_d"] / 2, g["base_d"] / 2
        ratio = (math.sqrt(max(ra ** 2 - rb ** 2, 0.0)) - r * math.sin(alpha) + m / math.sin(alpha)) \
            / (p * math.cos(alpha))
        sev = Severity.INFO if ratio >= 1.2 else Severity.WARNING
        rep.add("GEAR.OK", sev, f"Gear m{m:g} z{z} meshes freely with the rack at x = {gear_x:.3f} mm "
                f"(contact ratio {ratio:.2f}).", subject,
                hint="" if sev == Severity.INFO else "Contact ratio < 1.2: use more teeth.",
                contact_ratio=ratio, gear_x=gear_x)
    return rep


def check_mesh(module: float, z1: int, z2: int, thickness: float = 5.0, *, steps: int = 8,
               backlash: float = 0.1, extra_distance: float = 0.0, pressure_angle: float = 20.0) -> Report:
    """Rotate two generated gears through one tooth pitch and look for overlap.

    Gear 1 at the origin, gear 2 at (centre_distance + ``extra_distance``, 0). Emits
    ``GEAR.INTERFERENCE`` (ERROR; data: angles, max overlap mm³ = area × thickness) or ``GEAR.OK``
    (INFO, with the contact ratio and the gear-2 tip clearance).
    """
    if steps < 1:
        raise ValidationError("steps must be >= 1")
    rep = Report(title=f"gear mesh m{module:g} z{z1}/z{z2}")
    g1 = gear_geometry(module, z1, pressure_angle)
    g2 = gear_geometry(module, z2, pressure_angle)
    a = center_distance(module, z1, z2) + extra_distance
    p1 = gear_profile(module, z1, pressure_angle=pressure_angle, backlash=backlash)
    p2 = gear_profile(module, z2, pressure_angle=pressure_angle, backlash=backlash)
    phase2 = 180.0 - 180.0 / z2  # a tooth space of gear 2 faces tooth 0 of gear 1
    hits: list[float] = []
    worst = 0.0
    for i in range(steps):
        th = 360.0 / z1 * i / steps
        q1 = affinity.rotate(p1, th, origin=(0, 0))
        q2 = affinity.translate(affinity.rotate(p2, phase2 - th * z1 / z2, origin=(0, 0)), a, 0)
        area = q1.intersection(q2).area
        if area > 1e-4:
            hits.append(round(th, 4))
            worst = max(worst, area)
    subject = f"gears:m{module:g}:{z1}x{z2}"
    if hits:
        rep.add("GEAR.INTERFERENCE", Severity.ERROR,
                f"Gears m{module:g} z{z1}/z{z2} at {a:.3f} mm overlap by up to {worst * thickness:.2f} mm³ "
                f"at {len(hits)}/{steps} positions.", subject,
                hint=f"Use the nominal centre distance {center_distance(module, z1, z2):.3f} mm, "
                     "more backlash, or profile shift.",
                angles=hits, max_overlap_mm3=worst * thickness, center_distance=a)
    else:
        alpha = math.radians(pressure_angle)
        ra1, ra2 = g1["tip_d"] / 2, g2["tip_d"] / 2
        rb1, rb2 = g1["base_d"] / 2, g2["base_d"] / 2
        a0 = center_distance(module, z1, z2)
        aw = math.acos(min(1.0, a0 * math.cos(alpha) / a))  # working pressure angle
        ratio = (math.sqrt(max(ra1 ** 2 - rb1 ** 2, 0)) + math.sqrt(max(ra2 ** 2 - rb2 ** 2, 0))
                 - a * math.sin(aw)) / (math.pi * module * math.cos(alpha))
        tip_gap = a - ra2 - g1["root_d"] / 2
        sev = Severity.INFO if ratio >= 1.2 else Severity.WARNING
        rep.add("GEAR.OK", sev,
                f"Gears m{module:g} z{z1}/z{z2} mesh freely at {a:.3f} mm (contact ratio {ratio:.2f}, "
                f"tip clearance {tip_gap:.2f} mm).", subject,
                hint="" if sev == Severity.INFO else "Contact ratio < 1.2: teeth may lose contact; "
                                                     "move the gears closer or use more teeth.",
                contact_ratio=ratio, tip_clearance=tip_gap, center_distance=a)
    return rep
