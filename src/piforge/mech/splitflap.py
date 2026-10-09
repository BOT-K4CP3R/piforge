"""Parametric split-flap digit module (20 flaps, 0–9 twice) driven by a 28BYJ-48 stepper.

Mechanism (the classic Solari / David Bliss principle, as in the open-source "splitflap" project by
Scott Bezek): flaps hang by two side pins in holes on the rims of two spool flanges. The flaps above
the axle stand up and lean forward on each other; the front-most one rests with its free edge against
a small *stop* tab on each side frame. When the spool turns (front of the spool moving down) the
held flap's pin travels down, its edge slides down the stop and slips past it, and the flap falls
forward through ~180° to hang in front of the spool. The window then shows the **top half** of the
new digit on the front of the next standing flap and the **bottom half** on the *back* of the flap
that just fell. One digit = one flap pitch = 360°/20 = 18° of spool rotation.

Module frame (binding, mm, Z up): X = spool axis (motor on +X, flaps span x ∈ [-W/2, W/2]),
Y = depth (viewer at −Y, the front), Z up with the frame bottom at z = 0 and the spool axis at
``(0, 0, axis_z)``. The spool node's revolute joint turns about +X; a positive angle moves the top of
the spool towards the viewer (the flaps' direction of travel). Joint value 0 = flap 0 (digit 0) shown;
value 18·k shows flap k. Twin device ``angle`` drives it (``driven_by``), taken modulo 360.

Parts (see :class:`SplitFlapModule`): ``spool`` (drive flange + hub with the D-bore for the motor
shaft and the magnet pocket), ``spool_cap`` (second flange, keyed on the hub's D-spigot), ``flap``
(×20), ``frame_left`` (axle stub, stop), ``frame_right`` (motor mount, Hall pocket, stop),
``comma`` + ``comma_glyph``, ``bezel_segment(n)`` and ``backbone_segment(n)``. Artwork and the
2-colour flap variant live in :mod:`piforge.mech.splitflap_art`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

import build123d as bd
import numpy as np
from build123d import Align, Location, Part

from piforge.core.errors import ValidationError
from piforge.core.report import Report, Severity
from piforge.fab.profiles import get_printer
from piforge.mech.assembly import Assembly, Joint, matrix_to_location
from piforge.mech.fasteners import get_size
from piforge.mech.modules import get_module
from piforge.mech.part import PartSpec
from piforge.mech.primitives import EPS, _as_part

log = logging.getLogger(__name__)

__all__ = ["SplitFlapModule", "SplitFlapSpec"]

MOTOR_KEY = "stepper_28byj48"
# src: Kiatronics 28BYJ-48 drawing — Ø5 shaft with two flats 3.0 mm apart over its outer 6 mm.
SHAFT_D, SHAFT_FLATS, SHAFT_FLAT_LEN = 5.0, 3.0, 6.0
# src: Allegro A3141/2/3/4 datasheet, package UA (TO-92 flat): 4.06 × 3.00 × 1.52 mm body, Hall
#      element 0.50 mm behind the branded face. A3144-type sensors switch at ≈ 3–5 mm from a 6×3 mm
#      N35 disc magnet (common hobby test data), so ≤ 3 mm sensor-to-magnet distance is required.
HALL_BODY = (4.06, 3.00, 1.52)  # tangential, radial, thickness
HALL_ACTIVE_DEPTH = 0.50
HALL_MAX_DIST = 3.0
# A3144 leads: 0.43 mm thick; the model keeps a short stub in the lead slot.
_LEAD_W, _LEAD_T, _LEAD_L = 2.6, 0.45, 5.0
HALL_LEAD_PITCH = 1.27  # src: Allegro A3144 datasheet, UA package lead pitch
HALL_SLOT_HALF = 2.6  # half width of the lead slot: 3 wires (Ø1.2) on the 1.27 mm leads + margin

_MIN = (Align.MIN, Align.MIN, Align.MIN)
_GAP2D = 0.05  # mm left between touching flaps in the rest-pose solver (no false interference)


# ---------------------------------------------------------------------------------------------
# small geometry helpers (all return compound-backed Parts)
# ---------------------------------------------------------------------------------------------
def _box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> Part:
    return _as_part(bd.Box(x1 - x0, y1 - y0, z1 - z0, align=_MIN).moved(Location((x0, y0, z0))))


def _cyl_x(r: float, x0: float, x1: float, y: float = 0.0, z: float = 0.0) -> Part:
    """Cylinder of radius ``r`` along +X from ``x0`` to ``x1`` through (y, z)."""
    cyl = bd.Cylinder(r, x1 - x0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return _as_part(cyl.moved(Location((x0, y, z), (0, 90, 0))))


def _cyl_y(r: float, y0: float, y1: float, x: float = 0.0, z: float = 0.0) -> Part:
    """Cylinder along +Y from ``y0`` to ``y1`` through (x, z)."""
    cyl = bd.Cylinder(r, y1 - y0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return _as_part(cyl.moved(Location((x, y0, z), (-90, 0, 0))))


def _cyl_z(r: float, z0: float, z1: float, x: float = 0.0, y: float = 0.0) -> Part:
    cyl = bd.Cylinder(r, z1 - z0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return _as_part(cyl.moved(Location((x, y, z0))))


def _plate_yz(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float, radius: float) -> Part:
    """Plate in the YZ plane (thickness along X) with rounded corners."""
    sk = bd.RectangleRounded(y1 - y0, z1 - z0, radius) if radius > 0 else bd.Rectangle(y1 - y0, z1 - z0)
    plane = bd.Plane(origin=(x0, (y0 + y1) / 2, (z0 + z1) / 2), x_dir=(0, 1, 0), z_dir=(1, 0, 0))
    return _as_part(bd.extrude(sk.moved(plane.location), x1 - x0))


def _plate_xz(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float, radius: float) -> Part:
    """Plate in the XZ plane (thickness along Y, from y0 to y1) with rounded corners."""
    sk = bd.RectangleRounded(x1 - x0, z1 - z0, radius) if radius > 0 else bd.Rectangle(x1 - x0, z1 - z0)
    plane = bd.Plane(origin=((x0 + x1) / 2, y0, (z0 + z1) / 2), x_dir=(1, 0, 0), z_dir=(0, 1, 0))
    return _as_part(bd.extrude(sk.moved(plane.location), y1 - y0))


def _rect_cut_xz(cx: float, cz: float, w: float, h: float, y0: float, y1: float, radius: float) -> Part:
    return _plate_xz(cx - w / 2, cx + w / 2, y0, y1, cz - h / 2, cz + h / 2, radius)


def _union(parts: list[Part]) -> Part:
    out = parts[0]
    for p in parts[1:]:
        out = out + p
    return _as_part(out)


def _rot_x_about(part: Part, angle_deg: float, y: float, z: float) -> Part:
    """Rotate ``part`` by ``angle_deg`` about the X axis through (·, y, z)."""
    if abs(angle_deg) < 1e-12:
        return part
    loc = Location((0, y, z)) * Location((0, 0, 0), (angle_deg, 0, 0)) * Location((0, -y, -z))
    return _as_part(part.moved(loc))


def _polar(radius: float, angle_deg: float) -> tuple[float, float]:
    """(y, z) of a point at ``radius`` and angle measured from +Z towards −Y (the front)."""
    a = math.radians(angle_deg)
    return -radius * math.sin(a), radius * math.cos(a)


# ---------------------------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SplitFlapSpec:
    """Parameters of one split-flap module (mm, degrees). Defaults: 50 mm digits, 20 flaps.

    Reasoning behind the defaults (see the module docstring for the mechanism):

    * ``flap_margin`` 6: each flap carries half a digit, so the flap reaches
      ``digit_height/2 + flap_margin`` = 31 mm from its pin — 6 mm above/below the digit.
    * ``pitch_radius`` 14: 20 pin holes Ø2.55 on a Ø28 circle are 4.40 mm apart (1.85 mm web,
      > 2 perimeters); the 1.2 mm flaps stack with room to spare.
    * ``seam`` 0.4: the flap plate extends past its pin by ``pitch_radius·sin(9°) − seam/2`` so the
      upper (pin 9° above the axis) and lower (9° below) flaps leave only a 0.4 mm seam at the split.
    * ``flap_thickness`` 1.2: 6 layers of 0.2 mm — stiff, and room for 0.4 mm inlays on both faces.
    * ``stop_hold`` 2.5: the held flap's edge overlaps the stop by 2.5 mm at rest, so it slips past
      after ≈ 10° of the 18° step (margins on both sides for gear backlash).
    * ``magnet_radius`` 19.5: the 6×3 mm magnet sits outside the pin circle in the 4.4 mm drive
      flange, facing the Hall sensor pocketed flush in the right frame across a 1.2 mm gap. The drive
      flange is therefore Ø49, which is why ``frame_front`` (the bezel's back face) is at −26.5.
    """

    digit_height: float = 50.0
    flaps: int = 20
    flap_width: float = 40.0
    flap_thickness: float = 1.2
    flap_margin: float = 6.0
    pitch_radius: float = 14.0
    pin_width: float = 1.6
    pin_length: float = 2.3
    pin_clearance: float = 0.2  # radial play of the pin in its hole (rotating fit)
    seam: float = 0.4
    flap_gap: float = 0.5  # flap edge to flange inner face
    hub_d: float = 14.0
    drive_flange: float = 4.4
    cap_flange: float = 2.4
    frame_wall: float = 3.0
    gap_drive: float = 1.2  # drive flange to right frame (= magnet-to-Hall air gap)
    gap_cap: float = 1.0
    magnet_d: float = 6.0  # src: common 6 × 3 mm N35 disc magnet
    magnet_h: float = 3.0
    magnet_radius: float = 19.5
    hall_angle: float = 0.0  # where the Hall sensor sits on the frame (0 = top, + towards the front)
    home_angle: float = 0.0  # spool joint angle at which the magnet faces the sensor
    stop_overlap: float = 1.5  # how far the stop reaches over the flap's side edge
    stop_hold: float = 2.5
    stop_gap: float = 0.4  # held flap's lean: stop face this far in front of the flap face
    module_height: float = 100.0
    frame_front: float = -26.5
    frame_back: float = 36.0
    bezel_thickness: float = 3.0
    window_margin: float = 0.75
    comma_width: float = 24.0
    backbone_thickness: float = 5.0
    printer: str = "generic"
    material: str = "PLA"
    frame_color: str = "#3a3f47"
    spool_color: str = "#e0a458"
    flap_color: str = "#1c1c1c"
    ink_color: str = "#f2f2f2"
    bezel_color: str = "#24272c"

    def __post_init__(self) -> None:
        for name in ("digit_height", "flap_width", "flap_thickness", "flap_margin", "pitch_radius",
                     "pin_width", "pin_length", "pin_clearance", "flap_gap", "hub_d", "drive_flange",
                     "cap_flange", "frame_wall", "gap_drive", "gap_cap", "magnet_d", "magnet_h",
                     "magnet_radius", "stop_overlap", "stop_hold", "stop_gap", "module_height",
                     "bezel_thickness", "comma_width", "backbone_thickness"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                raise ValidationError(f"SplitFlapSpec.{name} must be a positive number, got {v!r}")
        if not isinstance(self.flaps, int) or self.flaps < 10 or self.flaps % 10:
            raise ValidationError(f"SplitFlapSpec.flaps must be a multiple of 10 (digits 0–9), got {self.flaps!r}")
        if self.seam < 0:
            raise ValidationError("SplitFlapSpec.seam must be >= 0")
        if self.frame_front >= -self.pitch_radius or self.frame_back <= self.pitch_radius:
            raise ValidationError("frame_front/back must lie outside the pin circle")
        get_printer(self.printer)  # NotFoundError lists close matches


# ---------------------------------------------------------------------------------------------
# module
# ---------------------------------------------------------------------------------------------
class SplitFlapModule:
    """Geometry, assembly and checks of one split-flap digit module (see the module docstring).

    ``SplitFlapModule()`` uses the defaults; ``SplitFlapModule(spec)`` or keyword overrides
    (``SplitFlapModule(digit_height=40)``) customise it. Parts are built lazily and cached.
    """

    def __init__(self, spec: SplitFlapSpec | None = None, **overrides: Any):
        spec = spec or SplitFlapSpec()
        if overrides:
            spec = replace(spec, **overrides)
        self.spec = spec
        s = spec
        self.printer = get_printer(s.printer)
        comp = self.printer.hole_compensation
        n = s.flaps
        self.pitch_angle = 360.0 / n
        half = self.pitch_angle / 2
        # flap: pin axis at local z = 0, plate from −pin_edge to flap_length
        self.flap_length = s.digit_height / 2 + s.flap_margin
        self.pin_edge = s.pitch_radius * math.sin(math.radians(half)) - s.seam / 2
        if self.pin_edge < s.pin_width / 2 + 0.3:
            raise ValidationError(
                f"pitch_radius {s.pitch_radius} mm is too small: the flap must extend "
                f">= {s.pin_width / 2 + 0.3:.2f} mm past its pin to close the seam")
        self.flap_height = self.pin_edge + self.flap_length
        self.axis_z = s.module_height / 2
        # pin hole: the rectangular pin turns inside it, so its diagonal + play
        self.pin_hole_d = math.hypot(s.flap_thickness, s.pin_width) + 2 * s.pin_clearance + comp
        self.pin_pitch = 2 * s.pitch_radius * math.sin(math.radians(half))
        # x layout (flap centred on x = 0, motor on +X)
        w2 = s.flap_width / 2
        self.x_drive_in = w2 + s.flap_gap
        self.x_drive_out = self.x_drive_in + s.drive_flange
        self.x_cap_in = -self.x_drive_in
        self.x_cap_out = self.x_cap_in - s.cap_flange
        self.x_right_in = self.x_drive_out + s.gap_drive
        self.x_right_out = self.x_right_in + s.frame_wall
        self.x_left_in = self.x_cap_out - s.gap_cap
        self.x_left_out = self.x_left_in - s.frame_wall
        self.x_inner = w2 + 0.6  # posts and feet stay outside the flap envelope |x| < W/2
        if s.pin_length <= s.flap_gap + 1.0 or s.pin_length >= s.flap_gap + min(s.cap_flange, s.drive_flange):
            raise ValidationError(
                f"pin_length {s.pin_length} mm must reach > 1 mm into both flanges without poking out "
                f"(flap_gap {s.flap_gap}, flanges {s.cap_flange}/{s.drive_flange} mm)")
        # motor (28BYJ-48 module model: tabs against the frame's outer face, shaft along −X)
        self.motor = get_module(MOTOR_KEY)
        shaft = self.motor.component("shaft")
        self.shaft_offset = (shaft[2][0], shaft[2][1])  # shaft centre in the motor frame
        self.shaft_reach = shaft[2][2] + shaft[3][1] / 2 - self.motor.front_height  # beyond the tab plane
        self.shaft_engagement = self.shaft_reach - s.frame_wall - s.gap_drive
        if self.shaft_engagement < 5.0:
            raise ValidationError(f"the motor shaft enters the hub only {self.shaft_engagement:.1f} mm; "
                                  "reduce frame_wall or gap_drive")
        self.bore_depth = self.shaft_engagement + 1.2
        # spool radii
        self.hub_r = s.hub_d / 2
        self.spigot_r = self.hub_r - 1.25
        self.spigot_flat = self.spigot_r - 1.0  # D-flat (keys the cap so its holes line up)
        self.axle_d = 6.0
        self.axle_bore_d = self.axle_d + 2 * self.printer.clearance_loose + comp
        self.axle_engage = 4.5
        self.magnet_pocket_d = s.magnet_d + 0.3 + comp
        self.magnet_recess = 0.2
        self.drive_r = s.magnet_radius + self.magnet_pocket_d / 2 + 1.8
        self.cap_r = s.pitch_radius + self.pin_hole_d / 2 + 1.7
        if s.magnet_radius - self.magnet_pocket_d / 2 < s.pitch_radius + self.pin_hole_d / 2 + 0.8:
            raise ValidationError("magnet_radius is too small: the magnet pocket would cut the pin holes")
        if s.drive_flange < s.magnet_h + self.magnet_recess + 0.8:
            raise ValidationError("drive_flange is too thin for the magnet pocket (+0.8 mm floor)")
        # rest pose: flap 0 pin 9° above the axis at the front
        self.phi0 = 90.0 - half
        pin0 = _polar(s.pitch_radius, self.phi0)
        self.stop_y = pin0[0] - s.flap_thickness / 2 - s.stop_gap  # stop's back face (flap side)
        self.stop_z = pin0[1] + self.flap_length - s.stop_hold  # stop's bottom face, rel. axis
        self.stop_depth = 2.5
        self.stop_height = 4.0
        if self.stop_y - self.stop_depth <= s.frame_front:
            raise ValidationError("frame_front is too close to the flaps for the stop tab")
        # window in the bezel: the falling flap swings out through it
        self.window_w = s.flap_width + 2 * s.window_margin
        self.window_h = 2 * (self.flap_length + 2.0)
        # fastener positions (frame feet and bezel posts), x of the tap holes
        self.post_x = ((self.x_left_out - self.x_inner) / 2, (self.x_inner + self.x_right_out) / 2)
        self.post_z = (11.0, s.module_height - 11.0)
        self.foot_y = (s.frame_front + 6.0, s.frame_back - 6.0)
        self.tap_d = get_size("M3").tap_plastic + comp
        self.clear_d = get_size("M3").clearance("normal") + comp
        # module pitch: left frame to the end of the motor body
        self.left_ext = -self.x_left_out + 0.3
        right_ext = self.x_right_out + self.motor.front_height + 0.3
        self.pitch = math.ceil((self.left_ext + right_ext) * 2) / 2
        if self.window_h > s.module_height - 2 * 16.5:
            raise ValidationError(f"module_height {s.module_height} mm leaves no room around the "
                                  f"{self.window_h:.0f} mm window for the bezel posts")

    # -- kernel-free kinematics ------------------------------------------------------------------
    def pin_position(self, k: int, spool_angle: float = 0.0) -> tuple[float, float]:
        """(y, z) of flap ``k``'s pin axis relative to the spool axis at ``spool_angle``."""
        return _polar(self.spec.pitch_radius, self.pin_angle(k) + spool_angle)

    def pin_angle(self, k: int) -> float:
        """Angle of hole ``k`` at joint value 0 (from +Z towards the front)."""
        return self.phi0 - self.pitch_angle * k

    @staticmethod
    def flap_digit(k: int) -> int:
        """Digit printed (top half on the front) on flap ``k``: 0–9, 0–9…"""
        return k % 10

    def _flap_poly(self, pin: tuple[float, float], angle: float):
        from shapely.geometry import Polygon

        a = math.radians(angle)
        d = (-math.sin(a), math.cos(a))
        m = (d[1], -d[0])
        t2 = self.spec.flap_thickness / 2
        pts = [(pin[0] + s * d[0] + k * t2 * m[0], pin[1] + s * d[1] + k * t2 * m[1])
               for s, k in ((-self.pin_edge, -1), (self.flap_length, -1), (self.flap_length, 1),
                            (-self.pin_edge, 1))]
        return Polygon(pts)

    def _stop_poly(self):
        from shapely.geometry import box

        return box(self.stop_y - self.stop_depth, self.stop_z, self.stop_y, self.stop_z + self.stop_height)

    @cached_property
    def rest_poses(self) -> dict[int, tuple[float, float, float]]:
        """Static rest pose of every flap at joint value 0: k → (pin y, pin z, angle°), rel. axis.

        ``angle`` is the direction pin → free edge measured from +Z towards the front: ~0 for the
        standing flaps (leaning forward onto the stop / the flap in front), 180 + lean for hanging
        ones. Solved in 2D (YZ): the standing stack from flap 0 backwards, each flap tipped forward
        until it touches what is in front; then the hanging stack from the newest fallen flap, each
        hanging as straight down as the flaps already placed and the hub allow.
        """
        from shapely.geometry import Point, box

        s = self.spec
        obst = [Point(0, 0).buffer(self.hub_r, 64), self._stop_poly(),
                box(-500, -self.axis_z - 50, 500, -self.axis_z)]  # hub, stop, the backbone/floor
        placed: dict[int, Any] = {}
        out: dict[int, tuple[float, float, float]] = {}

        def free(p) -> bool:
            return all(p.distance(o) >= _GAP2D for o in obst) and \
                all(p.distance(q) >= _GAP2D for q in placed.values())

        n, k = s.flaps, 0
        while k < n:  # standing stack: lean forward until contact
            pin = self.pin_position(k)
            if not free(self._flap_poly(pin, 0.0)):
                break
            a = 0.0
            while a < 100.0 and free(self._flap_poly(pin, a + 0.25)):
                a += 0.25
            if a >= 100.0:
                break
            placed[k] = self._flap_poly(pin, a)
            out[k] = (pin[0], pin[1], a)
            k += 1
        for j in range(n - 1, k - 1, -1):  # hanging stack, newest fallen (flap n-1) first
            pin = self.pin_position(j)
            best = None
            for step in range(0, 721):
                for sign in (1, -1):
                    b = 180.0 + sign * step * 0.25
                    if free(self._flap_poly(pin, b)):
                        best = b
                        break
                if best is not None:
                    break
            if best is None:
                log.warning("splitflap: no collision-free rest pose for flap %d", j)
                best = 180.0
            placed[j] = self._flap_poly(pin, best)
            out[j] = (pin[0], pin[1], best)
        return dict(sorted(out.items()))

    @cached_property
    def release_advance(self) -> float | None:
        """Spool advance (°) after which the held flap slips past the stop and falls (None: never)."""
        stop = self._stop_poly()
        steps = int(round(self.pitch_angle * 20))
        for i in range(steps + 1):
            adv = i * 0.05
            pin = self.pin_position(0, adv)
            a = 0.0
            while a < 60.0 and self._flap_poly(pin, a + 0.25).distance(stop) > 0.0:
                a += 0.25
            if a >= 60.0:
                return adv
        return None

    def release_lean(self, advance: float) -> float:
        """Forward lean (°) of the held flap at spool ``advance`` (resting on the stop)."""
        stop = self._stop_poly()
        pin = self.pin_position(0, advance)
        a = 0.0
        while a < 60.0 and self._flap_poly(pin, a + 0.25).distance(stop) > 0.0:
            a += 0.25
        return a if a < 60.0 else 0.0

    # -- parts -------------------------------------------------------------------------------------
    def _pin_holes(self, x0: float, x1: float) -> list[Part]:
        out = []
        for k in range(self.spec.flaps):
            y, z = _polar(self.spec.pitch_radius, self.pin_angle(k))
            out.append(_cyl_x(self.pin_hole_d / 2, x0 - EPS, x1 + EPS, y, z))
        return out

    @property
    def magnet_angle(self) -> float:
        """Angle of the magnet in the spool frame (joint value 0)."""
        return self.spec.hall_angle - self.spec.home_angle

    @cached_property
    def spool(self) -> PartSpec:
        """Drive half of the spool: flange (pin holes, magnet pocket) + hub with the motor D-bore.

        Spool frame = module frame shifted so the axis is the X axis (the node sits at ``axis_z``).
        Printed outer face down (+X → −Z): flange on the bed, hub and D-spigot up; the D-bore and the
        magnet pocket open at the bed and get bridged roofs.
        """
        s = self.spec
        comp = self.printer.hole_compensation
        body = _union([
            _cyl_x(self.drive_r, self.x_drive_in, self.x_drive_out),
            _cyl_x(self.hub_r, self.x_cap_in, self.x_drive_in + EPS),
            _cyl_x(self.spigot_r, self.x_cap_out, self.x_cap_in + EPS)
            - _box(self.x_cap_out - 1, self.x_cap_in + 0.5, -10, 10, -10, -self.spigot_flat),
        ])
        cuts = self._pin_holes(self.x_drive_in, self.x_drive_out)
        # axle bore for the left frame's stub
        cuts.append(_cyl_x(self.axle_bore_d / 2, self.x_cap_out - EPS, self.x_cap_out + self.axle_engage + 1.5))
        # motor D-bore: round lead-in where the shaft is still round, double-D beyond it
        x1 = self.x_drive_out
        round_len = max(self.shaft_engagement - SHAFT_FLAT_LEN, 0.0) + 0.3
        cuts.append(_cyl_x((SHAFT_D + 0.6 + comp) / 2, x1 - round_len, x1 + EPS))
        dcut = _cyl_x((SHAFT_D + 0.2 + comp) / 2, x1 - self.bore_depth, x1 + EPS)
        half_flat = SHAFT_FLATS / 2 + 0.1 + comp / 2
        dcut = _as_part(dcut & _box(x1 - self.bore_depth - 1, x1 + 1, -half_flat, half_flat, -5, 5))
        cuts.append(dcut)
        # magnet pocket in the outer face
        my, mz = _polar(s.magnet_radius, self.magnet_angle)
        depth = s.magnet_h + self.magnet_recess
        cuts.append(_cyl_x(self.magnet_pocket_d / 2, x1 - depth, x1 + EPS, my, mz))
        shape = _as_part(body - _union(cuts))
        return PartSpec("spool", shape, material=s.material, color=s.spool_color, print_rotation=(0, 90, 0),
                        meta={"role": "splitflap spool (drive)", "flaps": s.flaps})

    @cached_property
    def spool_cap(self) -> PartSpec:
        """Second flange: pin holes + a D-hole that presses onto the hub's D-spigot (holes line up)."""
        s = self.spec
        comp = self.printer.hole_compensation
        disc = _cyl_x(self.cap_r, self.x_cap_out, self.x_cap_in)
        r = self.spigot_r + 0.1 + comp / 2
        dhole = _as_part(_cyl_x(r, self.x_cap_out - EPS, self.x_cap_in + EPS)
                         - _box(self.x_cap_out - 1, self.x_cap_in + 1, -10, 10, -10, -(self.spigot_flat + 0.1)))
        shape = _as_part(disc - _union(self._pin_holes(self.x_cap_out, self.x_cap_in) + [dhole]))
        return PartSpec("spool_cap", shape, material=s.material, color=s.spool_color, print_rotation=(0, -90, 0),
                        meta={"role": "splitflap spool (cap)"})

    def _flap_shape(self) -> Part:
        s = self.spec
        w2, t2, pw2 = s.flap_width / 2, s.flap_thickness / 2, s.pin_width / 2
        plate = _box(-w2, w2, -t2, t2, -self.pin_edge, self.flap_length)
        tabs = [_box(w2 - EPS, w2 + s.pin_length, -t2, t2, -pw2, pw2),
                _box(-w2 - s.pin_length, -w2 + EPS, -t2, t2, -pw2, pw2)]
        return _union([plate] + tabs)

    @cached_property
    def flap(self) -> PartSpec:
        """One flap (quantity = flaps): plate + two side pins; local frame: pin axis = X axis,
        thickness along Y (front face at −Y), free edge at +Z. Printed flat, front face down."""
        s = self.spec
        return PartSpec("flap", self._flap_shape(), material=s.material, color=s.flap_color,
                        quantity=s.flaps, print_rotation=(90, 0, 0),
                        meta={"role": "splitflap flap", "size_mm": [s.flap_width, self.flap_height,
                                                                     s.flap_thickness]})

    def _frame_common(self, side: str) -> tuple[Part, list[Part]]:
        """Plate + foot + bezel posts + stop tab of one side frame, and its tap-hole cutters."""
        s = self.spec
        za = self.axis_z
        if side == "left":
            xo, xi, xin, px = self.x_left_out, self.x_left_in, -self.x_inner, self.post_x[0]
            stop_x = (xi - EPS, -(s.flap_width / 2 - s.stop_overlap))
            post_x = (xi - EPS, xin)
        else:
            xo, xi, xin, px = self.x_right_out, self.x_right_in, self.x_inner, self.post_x[1]
            stop_x = (s.flap_width / 2 - s.stop_overlap, xi + EPS)
            post_x = (xin, xi + EPS)
        x0, x1 = min(xo, xi), max(xo, xi)
        parts = [_plate_yz(x0, x1, s.frame_front, s.frame_back, 0.0, s.module_height, 4.0)]
        parts.append(_box(post_x[0], post_x[1], s.frame_front, s.frame_back, 0.0, 10.0))  # foot
        for zc in self.post_z:
            parts.append(_box(post_x[0], post_x[1], s.frame_front, s.frame_front + 10.0, zc - 5.0, zc + 5.0))
        parts.append(_box(stop_x[0], stop_x[1], self.stop_y - self.stop_depth, self.stop_y,
                          za + self.stop_z, za + self.stop_z + self.stop_height))
        cuts = []
        for fy in self.foot_y:  # M3 tapped from below (screws come up through the backbone)
            cuts.append(_cyl_z(self.tap_d / 2, -EPS, 8.0, px, fy))
        for zc in self.post_z:  # M3 tapped from the front (bezel screws)
            cuts.append(_cyl_y(self.tap_d / 2, s.frame_front - EPS, s.frame_front + 7.5, px, zc))
        return _union(parts), cuts

    @cached_property
    def frame_left(self) -> PartSpec:
        """Left side frame: axle stub for the spool cap, stop tab, foot and bezel posts (printed
        outer face down; every feature points up)."""
        s = self.spec
        body, cuts = self._frame_common("left")
        stub = _cyl_x(self.axle_d / 2, self.x_left_in - EPS, self.x_cap_out + self.axle_engage, 0.0, self.axis_z)
        shape = _as_part(_as_part(body + stub) - _union(cuts))
        return PartSpec("frame_left", shape, material=s.material, color=s.frame_color, print_rotation=(0, -90, 0),
                        meta={"role": "splitflap side frame (left)"})

    @cached_property
    def hall_position(self) -> tuple[float, float]:
        """(y, z) of the Hall sensor's centre in the module frame (on the right frame's inner face)."""
        y, z = _polar(self.spec.magnet_radius, self.spec.hall_angle)
        return y, self.axis_z + z

    @cached_property
    def frame_right(self) -> PartSpec:
        """Right side frame: 28BYJ-48 mount (boss hole + 35 mm tab screws), Hall sensor pocket flush
        with the inner face facing the magnet path, lead slot, stop tab, foot and bezel posts."""
        s = self.spec
        comp = self.printer.hole_compensation
        body, cuts = self._frame_common("right")
        za = self.axis_z
        boss = self.motor.component("boss")
        cuts.append(_cyl_x((boss[3][0] + 1.0 + comp) / 2, self.x_right_in - EPS, self.x_right_out + EPS, 0.0, za))
        for hy, hz in self.motor_holes:
            cuts.append(_cyl_x(self.tap_d / 2, self.x_right_in - EPS, self.x_right_out + EPS, hy, hz))
        # Hall pocket (TO-92 UA, branded face flush with the inner face) + lead slot, at hall_angle
        hy, hz = self.hall_position
        bw, bh, bt = HALL_BODY
        xi = self.x_right_in
        pocket = _box(xi - EPS, xi + bt + 0.2, hy - (bw + 0.3) / 2, hy + (bw + 0.3) / 2,
                      hz - (bh + 0.3) / 2, hz + (bh + 0.3) / 2)
        # lead slot through the wall: the three soldered wires (and the 5 V/GND bus pair) leave towards +X
        slot = _box(xi - EPS, self.x_right_out + EPS, hy - HALL_SLOT_HALF, hy + HALL_SLOT_HALF, hz,
                    hz + (bh + 0.3) / 2 + 6.0)
        cuts.append(_rot_x_about(_as_part(pocket + slot), s.hall_angle, hy, hz))
        shape = _as_part(body - _union(cuts))
        return PartSpec("frame_right", shape, material=s.material, color=s.frame_color, print_rotation=(0, 90, 0),
                        meta={"role": "splitflap side frame (motor side)"})

    # -- motor / sensor placement ---------------------------------------------------------------
    @cached_property
    def motor_matrix(self) -> np.ndarray:
        """4×4 placement of the 28BYJ-48 module model: shaft on the spool axis pointing −X, tabs on
        the right frame's outer face, body behind the shaft (+Y)."""
        sx, sy = self.shaft_offset
        m = np.eye(4)
        m[:3, 0] = (0, 0, -1)  # motor X → world −Z (tab line vertical)
        m[:3, 1] = (0, -1, 0)  # motor Y (towards the shaft) → world −Y
        m[:3, 2] = (-1, 0, 0)  # motor Z (shaft direction) → world −X
        m[:3, 3] = (self.x_right_out + self.motor.front_height, sy, self.axis_z + sx)
        return m

    @cached_property
    def motor_holes(self) -> list[tuple[float, float]]:
        """(y, z) of the motor's tab holes on the right frame (35 mm apart)."""
        m = self.motor_matrix
        return [(float((m @ (hx, hy, 0, 1))[1]), float((m @ (hx, hy, 0, 1))[2])) for hx, hy in self.motor.holes]

    @cached_property
    def magnet(self) -> PartSpec:
        """The 6×3 mm disc magnet in its pocket (spool frame)."""
        s = self.spec
        my, mz = _polar(s.magnet_radius, self.magnet_angle)
        x1 = self.x_drive_out - self.magnet_recess
        shape = _cyl_x(s.magnet_d / 2, x1 - s.magnet_h, x1, my, mz)
        return PartSpec("magnet", shape, kind="reference", material="NdFeB", color="#9aa4b2",
                        meta={"size_mm": [s.magnet_d, s.magnet_h]})

    @cached_property
    def hall(self) -> PartSpec:
        """A3144 Hall sensor (TO-92 UA) in its pocket, branded face towards the magnet (−X)."""
        hy, hz = self.hall_position
        bw, bh, bt = HALL_BODY
        xi = self.x_right_in
        body = _box(xi, xi + bt, hy - bw / 2, hy + bw / 2, hz - bh / 2, hz + bh / 2)
        leads = _box(xi + 0.5, xi + 0.5 + _LEAD_T, hy - _LEAD_W / 2, hy + _LEAD_W / 2, hz + bh / 2 - EPS,
                     hz + bh / 2 + _LEAD_L)
        shape = _rot_x_about(_as_part(body + leads), self.spec.hall_angle, hy, hz)
        return PartSpec("hall", shape, kind="reference", material="A3144", color="#202020",
                        meta={"elec_key": "hall_a3144"})

    def hall_anchors(self) -> tuple:
        """Wire anchors (module frame) of the Hall sensor: the soldered lead tips, wires leaving
        through the lead slot towards +X (VCC, GND, OUT = pin 1…3; pin 1 on the +Y side seen from
        the branded face). A second wire on the same lead fans out downwards (−Z)."""
        from piforge.mech.anchors import PinAnchor

        hy, hz = self.hall_position
        bh = HALL_BODY[1]
        xi = self.x_right_in
        a = math.radians(self.spec.hall_angle)
        c, sn = math.cos(a), math.sin(a)

        def rot(y: float, z: float, about: bool = True) -> tuple[float, float]:
            dy, dz = (y - hy, z - hz) if about else (y, z)
            ry, rz = dy * c - dz * sn, dy * sn + dz * c
            return (ry + hy, rz + hz) if about else (ry, rz)

        out = []
        tip = hz + bh / 2 + _LEAD_L - 0.8
        for k, pin in enumerate(("VCC", "GND", "OUT")):
            y, z = rot(hy + (1 - k) * HALL_LEAD_PITCH, tip)
            fy, fz = rot(0.0, -1.0, about=False)
            out.append(PinAnchor(pin=pin, number=str(k + 1), pos=(xi + 0.5 + _LEAD_T / 2, y, z), dir=(1.0, 0.0, 0.0),
                                 kind="solder", fan=(0.0, fy, fz), label=f"A3144 {pin} lead"))
        return tuple(out)

    def hall_distance(self, joint_value: float | None = None) -> float:
        """Distance (mm) from the Hall element to the magnet's face centre at ``joint_value``
        (default: ``home_angle``)."""
        s = self.spec
        j = s.home_angle if joint_value is None else joint_value
        my, mz = _polar(s.magnet_radius, self.magnet_angle + j)
        magnet = np.array([self.x_drive_out - self.magnet_recess, my, self.axis_z + mz])
        hy, hz = self.hall_position
        sensor = np.array([self.x_right_in + HALL_ACTIVE_DEPTH, hy, hz])
        return float(np.linalg.norm(sensor - magnet))

    # -- printed parts of the module -------------------------------------------------------------
    @property
    def parts(self) -> list[PartSpec]:
        """Printed parts of one digit module (the flap with quantity = flaps)."""
        return [self.spool, self.spool_cap, self.flap, self.frame_left, self.frame_right]

    @property
    def outer_size(self) -> tuple[float, float, float]:
        """Module slot (pitch × depth × height) incl. motor, mm."""
        s = self.spec
        return (self.pitch, s.frame_back - s.frame_front, s.module_height)

    # -- comma ------------------------------------------------------------------------------------
    @property
    def comma_y(self) -> tuple[float, float]:
        """(front, back) y of the comma panel: front face in the plane of the shown flaps."""
        pin = self.pin_position(0)
        front = pin[0] - self.spec.flap_thickness / 2
        return front, 12.0

    @property
    def comma_hole_y(self) -> tuple[float, float]:
        f, b = self.comma_y
        return f + 6.0, b - 6.0

    def comma_module(self, font: str = "Arial", bold: bool = True) -> list[PartSpec]:
        """Fixed comma module (``comma_width`` wide, same height): ``[comma, comma_glyph]``.

        ``comma`` is a U-channel whose front plate lies in the plane of the shown flaps, with a foot
        tapped from below like the frames; ``comma_glyph`` is the comma (same font and scale as the
        digit artwork, on the digits' baseline) as a 0.6 mm inlay for a second colour. Frame: x
        centred on the comma, y/z as the digit modules. Printed front face down.
        """
        from piforge.mech.splitflap_art import comma_faces

        s = self.spec
        c2 = s.comma_width / 2 - 0.5
        yf, yb = self.comma_y
        wall = 2.0
        parts = [_box(-c2, c2, yf, yf + wall, 0.0, s.module_height),
                 _box(-c2, -c2 + wall, yf, yb, 0.0, s.module_height),
                 _box(c2 - wall, c2, yf, yb, 0.0, s.module_height),
                 _box(-c2, c2, yf, yb, 0.0, 10.0)]
        body = _union(parts)
        cuts = [_cyl_z(self.tap_d / 2, -EPS, 8.0, 0.0, hy) for hy in self.comma_hole_y]
        depth = 0.6
        faces = comma_faces(self, font=font, bold=bold)

        def glyph(z0: float, z1: float) -> Part:  # (u, w) in XY → module (x = u, y ∈ [yf + z0, yf + z1], z = axis + w)
            solids = [_as_part(bd.extrude(f, z1 - z0).moved(
                Location((0, yf, self.axis_z), (90, 0, 0)) * Location((0, 0, -z1)))) for f in faces]
            return _union(solids)

        inlay = glyph(0.0, depth)
        body = _as_part(body - _union(cuts) - glyph(-EPS, depth))
        return [PartSpec("comma", body, material=s.material, color=s.flap_color, print_rotation=(90, 0, 0),
                         meta={"role": "splitflap comma module"}),
                PartSpec("comma_glyph", inlay, material=s.material, color=s.ink_color, print_rotation=(90, 0, 0),
                         meta={"role": "comma inlay (second colour)"})]

    # -- display layout -----------------------------------------------------------------------------
    def module_x(self, i: int, comma_after: int | None = None) -> float:
        """x of module ``i``'s origin (flap centre) in a row starting with module 0 at x = 0."""
        extra = self.spec.comma_width if comma_after is not None and i > comma_after else 0.0
        return i * self.pitch + extra

    def comma_x(self, comma_after: int) -> float:
        """x of the comma's centre in the same row frame."""
        return self.module_x(comma_after) - self.left_ext + self.pitch + self.spec.comma_width / 2

    def segment_plan(self, n_modules: int, comma_after: int | None = None) -> list[tuple[int, int, int | None]]:
        """Split a row into bed-sized segments: ``[(first_module, count, comma_after_local), …]``.

        ``comma_after_local`` is the local index of the module the comma follows inside that segment
        (−1: the comma opens the segment; None: no comma). A segment never ends with the comma, so
        every segment joint lies between two modules (the backbone joints then sit midway between
        two frames' screws).
        """
        if n_modules < 1:
            raise ValidationError("n_modules must be >= 1")
        if comma_after is not None and not 0 <= comma_after < n_modules - 1:
            raise ValidationError(f"comma_after must be a module index in [0, {n_modules - 2}]")
        bed = max(self.printer.build_x, self.printer.build_y) - 14.0  # 10 mm dovetail tongue + margin
        items = [("m", i) for i in range(n_modules)]
        if comma_after is not None:
            items.insert(comma_after + 1, ("c", -1))
        widths = {"m": self.pitch, "c": self.spec.comma_width}
        plan, pos = [], 0
        while pos < len(items):
            end, width = pos, 0.0
            while end < len(items) and (width + widths[items[end][0]] <= bed or end == pos):
                width += widths[items[end][0]]
                end += 1
            if items[end - 1][0] == "c" and end < len(items) and end - 1 > pos:
                end -= 1  # never end a segment with the comma
            seg = items[pos:end]
            mods = [i for kind, i in seg if kind == "m"]
            if not mods:  # a comma alone: give it the next module (always fits: 2 modules do)
                seg, end = items[pos:end + 1], end + 1
                mods = [i for kind, i in seg if kind == "m"]
            local = None
            for j, (kind, _i) in enumerate(seg):
                if kind == "c":
                    local = j - 1
            plan.append((mods[0], len(mods), local))
            pos = end
        return plan

    def _segment_layout(self, n_modules: int, comma_after: int | None) -> tuple[list[float], float | None, float]:
        """Module origins x, comma centre x and width of a segment (x = 0 at its left edge)."""
        if comma_after is not None and not -1 <= comma_after < n_modules:
            raise ValidationError(f"comma_after must be -1…{n_modules - 1} for a {n_modules}-module segment")
        c = self.spec.comma_width
        lead = c if comma_after == -1 else 0.0
        xs = [lead + self.left_ext + j * self.pitch + (c if comma_after is not None and -1 < comma_after < j else 0.0)
              for j in range(n_modules)]
        cx = None
        if comma_after is not None:
            cx = c / 2 if comma_after == -1 else (comma_after + 1) * self.pitch + c / 2
        width = n_modules * self.pitch + (c if comma_after is not None else 0.0)
        return xs, cx, width

    @property
    def backbone_shift(self) -> float:
        """x offset of the backbone joints from the bezel joints: midway between the right foot screw
        of one module and the left foot screw of the next (keeps the screws away from the joint)."""
        return self.left_ext + (self.post_x[0] + self.post_x[1] - self.pitch) / 2

    def bezel_segment(self, n_modules: int, comma_after: int | None = None, *, name: str | None = None) -> PartSpec:
        """Front panel over ``n_modules`` modules (windows + M3 clearance holes into the frames' posts),
        x from 0 (left edge of the first module slot) to its width, y ∈ [frame_front − t, frame_front].

        ``comma_after`` = local index of the module the comma follows (−1: the comma comes first).
        Printed front face down.
        """
        s = self.spec
        xs, cx, width = self._segment_layout(n_modules, comma_after)
        y1 = s.frame_front
        y0 = y1 - s.bezel_thickness
        plate = _plate_xz(0.0, width, y0, y1, 0.0, s.module_height, 3.0)
        cuts = [_rect_cut_xz(x, self.axis_z, self.window_w, self.window_h, y0 - EPS, y1 + EPS, 2.0) for x in xs]
        if cx is not None:
            cuts.append(_rect_cut_xz(cx, self.axis_z, s.comma_width - 6.0, self.window_h, y0 - EPS, y1 + EPS, 2.0))
        for x in xs:
            for px in self.post_x:
                for pz in self.post_z:
                    cuts.append(_cyl_y(self.clear_d / 2, y0 - EPS, y1 + EPS, x + px, pz))
        shape = _as_part(plate - _union(cuts))
        part = PartSpec(name or f"bezel_{n_modules}{'c' if cx is not None else ''}", shape, material=s.material,
                        color=s.bezel_color, print_rotation=(90, 0, 0),
                        meta={"role": "splitflap bezel segment", "modules": n_modules, "comma_after": comma_after,
                              "width_mm": width})
        return part

    def backbone_segment(self, n_modules: int, comma_after: int | None = None, *, left_joint: bool = True,
                         right_joint: bool = True, name: str | None = None) -> PartSpec:
        """Base rail under ``n_modules`` modules: counterbored M3 clearance holes for the frames' feet
        (and the comma's), a dovetail tongue on the +X end and a matching socket on the −X end so
        segments slide together from above. Same x frame as :meth:`bezel_segment`; the joints are
        shifted by :attr:`backbone_shift` (≈ −10 mm) so they fall between two modules' screws. The
        outer ends (``left_joint``/``right_joint`` False) stop flush with the bezel. z ∈ [−t, 0]
        (modules stand on z = 0). Printed upside down (counterbores up)."""
        s = self.spec
        xs, cx, width = self._segment_layout(n_modules, comma_after)
        t = s.backbone_thickness
        y0, y1 = s.frame_front, s.frame_back
        ym = (y0 + y1) / 2
        sh = self.backbone_shift
        x_lo = sh if left_joint else 0.0
        x_hi = width + sh if right_joint else width
        plate = _box(x_lo, x_hi, y0, y1, -t, 0.0)
        tongue_len, root, tip = 10.0, 14.0, 20.0
        clr = 0.15  # src: PrinterProfile sliding-fit practice (0.1–0.25 per side)

        def dovetail(x0: float, grow: float) -> Part:
            pts = [(x0, ym - root / 2 - grow), (x0 + tongue_len + grow, ym - tip / 2 - grow),
                   (x0 + tongue_len + grow, ym + tip / 2 + grow), (x0, ym + root / 2 + grow)]
            face = bd.Face(bd.Wire.make_polygon([bd.Vector(x, y, -t - EPS) for x, y in pts], close=True))
            return _as_part(bd.extrude(face, t + 2 * EPS))

        if right_joint:
            tongue = _as_part(dovetail(x_hi - EPS, 0.0) & _box(x_hi - 1, x_hi + tongue_len + 1, y0, y1, -t, 0.0))
            plate = _as_part(plate + tongue)
        cuts = []
        if left_joint:
            cuts.append(dovetail(x_lo - EPS, clr))
        head = get_size("M3")
        cb_d = head.head_socket_d + 0.6 + self.printer.hole_compensation
        holes = [(x + px, fy) for x in xs for px in self.post_x for fy in self.foot_y]
        if cx is not None:
            holes += [(cx, hy) for hy in self.comma_hole_y]
        for hx, hy in holes:
            cuts.append(_cyl_z(self.clear_d / 2, -t - EPS, EPS, hx, hy))
            cuts.append(_cyl_z(cb_d / 2, -t - EPS, -t + 3.0, hx, hy))
        shape = _as_part(plate - _union(cuts))
        return PartSpec(name or f"backbone_{n_modules}{'c' if cx is not None else ''}", shape, material=s.material,
                        color="#4a4f57", print_rotation=(180, 0, 0),
                        meta={"role": "splitflap backbone segment", "modules": n_modules,
                              "comma_after": comma_after, "width_mm": width})

    # -- assembly ------------------------------------------------------------------------------------
    def assembly(self, name: str = "splitflap", *, asm: Assembly | None = None, prefix: str = "",
                 origin: tuple[float, float, float] = (0.0, 0.0, 0.0), device: str | None = None,
                 flaps: bool = True, prop: str = "angle") -> Assembly:
        """One module as an :class:`Assembly` (or added to ``asm``), node ids prefixed by ``prefix``.

        Ids: ``frame_left``, ``frame_right``, ``motor``, ``hall``, ``spool`` (revolute joint about
        +X, 0–360°, driven by ``device``.``prop`` when ``device`` is given), ``spool_cap`` and
        ``magnet`` (children of the spool) and ``flap_00`` … ``flap_19`` in their static rest pose
        (not children of the spool: they hang by gravity, they do not turn rigidly with it).
        """
        a = asm if asm is not None else Assembly(name)
        ox, oy, oz = (float(v) for v in origin)
        za = self.axis_z

        def at(x: float, y: float, z: float, rx: float = 0.0) -> tuple:
            return (ox + x, oy + y, oz + z, rx, 0.0, 0.0)

        p = prefix
        a.add(self.frame_left, at(0, 0, 0), id=f"{p}frame_left", explode=(-25, 0, 0))
        a.add(self.frame_right, at(0, 0, 0), id=f"{p}frame_right", explode=(25, 0, 0))
        mm = self.motor_matrix.copy()
        mm[:3, 3] += (ox, oy, oz)
        a.add(self.motor.part("motor"), matrix_to_location(mm), id=f"{p}motor", explode=(55, 0, 0))
        a.add(self.hall, at(0, 0, 0), id=f"{p}hall", explode=(15, 0, 0))
        driven = {"device": device, "prop": prop, "scale": 1.0, "offset": 0.0} if device else None
        a.add(self.spool, at(0, 0, za), id=f"{p}spool",
              joint=Joint("revolute", axis=(1, 0, 0), min=0.0, max=360.0, value=0.0, driven_by=driven))
        a.add(self.spool_cap, (0, 0, 0), id=f"{p}spool_cap", parent=f"{p}spool", explode=(-12, 0, 0))
        a.add(self.magnet, (0, 0, 0), id=f"{p}magnet", parent=f"{p}spool", explode=(8, 0, 0))
        if flaps:
            for k, (py, pz, ang) in self.rest_poses.items():
                a.add(self.flap, at(0, py, za + pz, ang), id=f"{p}flap_{k:02d}", explode=(0, -30, 0))
        return a

    def display_assembly(self, n_modules: int, comma_after: int | None = None, *, name: str = "splitflap_display",
                         devices: list[str] | None = None, bezel: bool = True, backbone: bool = True,
                         flaps: bool = True) -> Assembly:
        """A row of ``n_modules`` modules (ids ``m0_…``), the comma after module ``comma_after``,
        bezel segments (``bezel_<s>``) and backbone segments (``backbone_<s>``) as planned by
        :meth:`segment_plan`. ``devices[i]`` drives module i's spool."""
        a = Assembly(name)
        for i in range(n_modules):
            dev = devices[i] if devices and i < len(devices) else None
            self.assembly(asm=a, prefix=f"m{i}_", origin=(self.module_x(i, comma_after), 0, 0), device=dev,
                          flaps=flaps)
        if comma_after is not None:
            cx = self.comma_x(comma_after)
            body, glyph = self.comma_module()
            a.add(body, (cx, 0, 0), id="comma")
            a.add(glyph, (0, 0, 0), id="comma_glyph", parent="comma")
        plan = self.segment_plan(n_modules, comma_after)
        for si, (first, count, cl) in enumerate(plan):
            x0 = self.module_x(first, comma_after) - self.left_ext - (self.spec.comma_width if cl == -1 else 0.0)
            last = si == len(plan) - 1
            if bezel:
                a.add(self.bezel_segment(count, cl), (x0, 0, 0), id=f"bezel_{si}", explode=(0, -40, 0))
            if backbone:
                a.add(self.backbone_segment(count, cl, left_joint=si > 0, right_joint=not last), (x0, 0, 0),
                      id=f"backbone_{si}", explode=(0, 0, -30))
        return a

    # -- checks ------------------------------------------------------------------------------------
    def geometry_checks(self) -> Report:
        """Kinematics/fit findings that need no CAD booleans: pin pitch, rest pose, flap release,
        window size, Hall-to-magnet distance."""
        s = self.spec
        rep = Report(title="splitflap geometry")
        subj = "splitflap"
        web = self.pin_pitch - self.pin_hole_d
        if web < 2 * self.printer.line_w:
            rep.add("SPLITFLAP.PIN_PITCH", Severity.ERROR,
                    f"Pin holes Ø{self.pin_hole_d:.2f} mm are {self.pin_pitch:.2f} mm apart: only {web:.2f} mm "
                    "web between them.", subj, hint="Increase pitch_radius or use thinner flaps/pins.",
                    web_mm=web)
        elif self.pin_pitch < s.flap_thickness + 1.0:
            rep.add("SPLITFLAP.PIN_PITCH", Severity.ERROR,
                    f"Flap pitch {self.pin_pitch:.2f} mm leaves no room between {s.flap_thickness} mm flaps.", subj)
        else:
            rep.add("SPLITFLAP.PIN_PITCH", Severity.INFO,
                    f"{s.flaps} flaps on a Ø{2 * s.pitch_radius:g} mm pin circle: {self.pin_pitch:.2f} mm pitch, "
                    f"Ø{self.pin_hole_d:.2f} mm holes, {web:.2f} mm web.", subj, web_mm=web,
                    pitch_mm=self.pin_pitch)
        poses = self.rest_poses
        standing = sum(1 for _, _, a in poses.values() if a < 90)
        rep.add("SPLITFLAP.REST_POSE", Severity.INFO,
                f"Rest pose: {standing} flaps standing (flap 0 on the stop), {len(poses) - standing} hanging.",
                subj, standing=standing, hanging=len(poses) - standing)
        adv = self.release_advance
        if adv is None or not (3.0 <= adv <= self.pitch_angle - 3.0):
            rep.add("SPLITFLAP.RELEASE", Severity.ERROR,
                    f"The held flap releases {'never' if adv is None else f'after {adv:.1f}°'} of the "
                    f"{self.pitch_angle:g}° step (needs 3°…{self.pitch_angle - 3:g}°).", subj,
                    hint="Adjust stop_hold (how far the flap edge overlaps the stop).", advance_deg=adv)
        else:
            rep.add("SPLITFLAP.RELEASE", Severity.INFO,
                    f"Flap slips past the stop after {adv:.1f}° of each {self.pitch_angle:g}° step and falls "
                    f"forward to hang in front (held {s.stop_hold:g} mm at rest).", subj, advance_deg=adv)
        top = self.flap_length - s.seam / 2
        if s.digit_height / 2 > top - 1.0 or self.window_w < s.flap_width:
            rep.add("SPLITFLAP.WINDOW", Severity.ERROR, "The digit does not fit the flap/window.", subj)
        else:
            rep.add("SPLITFLAP.WINDOW", Severity.INFO,
                    f"Window {self.window_w:.1f} × {self.window_h:.1f} mm shows a {s.digit_height:g} mm digit split "
                    f"on a {s.seam:g} mm seam; flaps {s.flap_width:g} × {self.flap_height:.1f} × {s.flap_thickness:g} mm.",
                    subj, window_mm=[self.window_w, self.window_h])
        d = self.hall_distance()
        if d > HALL_MAX_DIST:
            rep.add("SPLITFLAP.HALL_FAR", Severity.ERROR,
                    f"Hall element is {d:.2f} mm from the magnet at home (> {HALL_MAX_DIST:g} mm): it may not switch.",
                    subj, hint="Reduce gap_drive or check hall_angle/home_angle.", distance_mm=d)
        else:
            rep.add("SPLITFLAP.HALL_ALIGNED", Severity.INFO,
                    f"Hall element {d:.2f} mm from the magnet face at home angle {s.home_angle:g}°.", subj,
                    distance_mm=d)
        return rep

    def _swing_check(self) -> Report:
        """Sweep the released flap through its fall against the frames (stops) and a bezel."""
        adv = self.release_advance or self.pitch_angle / 2
        lean = self.release_lean(max(adv - 0.1, 0.0))
        tmp = Assembly("swing")
        tmp.add(self.frame_left, id="frame_left")
        tmp.add(self.frame_right, id="frame_right")
        tmp.add(self.bezel_segment(1, name="bezel"), (-self.left_ext, 0, 0), id="bezel")
        py, pz = self.pin_position(0, adv)
        tmp.add(self.flap, (0, py, self.axis_z + pz, lean + 0.5, 0, 0), id="flap",
                joint=Joint("revolute", axis=(1, 0, 0), min=0.0, max=180.0 - lean - 0.5))
        sweep = tmp.sweep_joint("flap", steps=19)
        rep = Report(title="splitflap swing")
        hits = sweep.by_code("ASM.JOINT_COLLISION")
        if hits:
            for f in hits:
                rep.add("SPLITFLAP.SWING_COLLISION", Severity.ERROR,
                        f"The falling flap hits {f.data.get('other')} at {f.data.get('angles')}°.", "splitflap",
                        hint="Enlarge the bezel window or move the stop.", **f.data)
        else:
            rep.add("SPLITFLAP.SWING_CLEAR", Severity.INFO,
                    "The falling flap swings through the bezel window without touching the stops or bezel.",
                    "splitflap")
        rep.extend(f for f in sweep if f.code == "ASM.CHECK_FAILED")
        return rep

    def checks(self, *, printability: bool = True, sweep_steps: int = 21) -> Report:
        """All module checks: geometry, rest-pose interference, spool sweep, flap swing, Hall
        alignment and (``printability=True``) :func:`piforge.fab.analyze_mesh` of every printed part."""
        rep = Report(title="splitflap module")
        rep.extend(self.geometry_checks())
        asm = self.assembly()
        asm.add(self.bezel_segment(1, name="bezel"), (-self.left_ext, 0, 0), id="bezel")
        # the module model's shaft is a plain Ø5 cylinder: the D-bore's flats overlap it by design
        rep.extend(asm.check_interference(ignore=[("motor", "spool")], min_volume=0.5))
        moving = ["spool", "spool_cap", "magnet"]
        ignore = [("motor", m) for m in moving] + [(m, f"flap_{k:02d}") for m in moving for k in range(self.spec.flaps)]
        rep.extend(asm.sweep_joint("spool", steps=sweep_steps, ignore=ignore))
        rep.extend(self._swing_check())
        if printability:
            rep.extend(self.print_checks())
        return rep

    def print_checks(self, parts: list[PartSpec] | None = None) -> Report:
        """Printability (``PRINT.*``) of every printed part in its print orientation: the module's
        parts, the comma, a 2-module bezel and backbone segment (or ``parts``)."""
        from piforge.fab.analyze import analyze_mesh
        from piforge.mech.export import to_trimesh

        if parts is None:
            parts = self.parts + [self.comma_module()[0], self.bezel_segment(2), self.backbone_segment(2)]
        rep = Report(title="splitflap printability")
        for part in parts:
            res = analyze_mesh(to_trimesh(part.shape), self.printer, part.material, name=part.name,
                               rotation=part.print_rotation)
            rep.extend(res.report)
        return rep
