"""Printed parts and layout of the demo climate gauge (imported by ``project.py``).

Frame: the enclosure frame of :mod:`piforge.mech.enclosure` — origin at the centre of the base's
outer bottom face, Z up, X along the Raspberry Pi's long edge (USB/Ethernet towards +X), GPIO
header towards +Y; the front of the gauge is −Y. The box is longer than the Pi on the −X side:
that compartment holds the SG90 servo under the dial, the intake fan and the BME280.

Parts made here (the base and the lid come from :class:`~piforge.mech.Enclosure`):

* ``enclosure_lid`` — the generated lid plus two posts that hold the servo by its tabs, a hole
  for the servo's gear turret and two bosses for the dial screws;
* ``enclosure_base`` — the generated base plus vertical grille bars in the fan opening;
* ``dial`` — 0–40 °C scale plate (two-colour print: white plate, dark marks after a filament
  change at 1.2 mm), screwed onto the lid with two M2 screws;
* ``needle`` — pressed onto the SG90's 21-tooth spline.
"""

from __future__ import annotations

import math
from dataclasses import replace

import build123d as bd

from piforge.mech import (
    EPS, PartSpec, get_module, get_size, hole_compensation, screw, to_location,
)

# -- design choices (mm) — not measurements --------------------------------------------------
SPLINE_XY = (-33.0, 0.0)  # servo output shaft = dial centre, above the −X compartment
POST_DROP = 4.5  # servo tab top sits this far below the lid's inner face (turret clears the lid)
POST_D = 5.5  # servo / dial screw posts
DIAL_R = 28.0  # dial radius
DIAL_T = 1.2  # dial plate thickness (marks add MARK_H on top)
MARK_H = 0.4  # raised scale marks (printed in a second colour)
DIAL_SCREWS = ((-14.0, -12.0), (14.0, -12.0))  # relative to the dial centre
DIAL_SCREW_L = 6  # M2 × 6: dial 1.2 + lid 2 + 2.8 mm into the boss
DIAL_BOSS_L = 5.0  # boss below the lid's inner face
TURRET_CLEAR = 0.4  # radial clearance around the servo's gear turret (lid and dial holes)
NEEDLE_GAP = 0.2  # needle hub above the turret top
NEEDLE_LEN = 24.0  # tip radius
NEEDLE_TAIL = 7.0
HUB_D, HUB_H = 7.0, 3.8
SPLINE_BORE = 4.9  # src: TowerPro SG90 drawing — Ø4.8 output spline; 0.1 mm clearance, glued or pressed
GRILLE_BAR, GRILLE_PITCH = 1.2, 4.0  # vertical bars in the fan opening (print upright, no bridges)

T_MIN, T_MAX = 0.0, 40.0  # dial range, °C
SWEEP = 70.0  # needle ±70° about "up" (+Y) for T_MIN…T_MAX; the firmware uses the same mapping

SG90 = get_module("sg90_servo")
# src: piforge module data for the SG90 (TowerPro drawing): tab underside 15.9, tabs 2.5 thick,
# turret Ø11.8 from 22.7 to 26.7, spline Ø4.8 to 29.7, shaft 5.5 mm from the body centre
_TAB_TOP = SG90.component("tabs")[2][2] + SG90.component("tabs")[3][2] / 2
_TURRET = SG90.component("gear_top")
_SPLINE = SG90.component("spline")
TURRET_D = _TURRET[3][0]
TURRET_TOP = _TURRET[2][2] + _TURRET[3][1] / 2
SPLINE_TOP = _SPLINE[2][2] + _SPLINE[3][1] / 2
SHAFT_DX = _TURRET[2][0]  # shaft offset from the body centre along the servo's X

WHITE, INK, RED = "#f4f1ea", "#22252a", "#d7263d"


def needle_angle(temp_c: float) -> float:
    """Servo/needle angle (degrees, CCW positive seen from above) for a temperature — the same
    mapping as the firmware: T_MIN → +SWEEP (left), T_MAX → −SWEEP (right), 20 °C straight up."""
    t = min(max(temp_c, T_MIN), T_MAX)
    return SWEEP - 2 * SWEEP * (t - T_MIN) / (T_MAX - T_MIN)


def _cyl(d: float, z0: float, z1: float, x: float = 0.0, y: float = 0.0) -> bd.Part:
    return bd.Cylinder(d / 2, z1 - z0, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)).moved(
        bd.Location((x, y, z0)))


def lid_z(enc) -> float:
    """World z of the lid's inner face."""
    return enc.spec.floor + enc.inner_size[2]


def servo_location(enc) -> bd.Location:
    """SG90 module frame → enclosure frame: shaft at SPLINE_XY, tabs POST_DROP below the lid."""
    x, y = SPLINE_XY
    return to_location((x - SHAFT_DX, y, lid_z(enc) - POST_DROP - _TAB_TOP))


def servo_tab_holes(enc) -> list[tuple[float, float]]:
    x0 = SPLINE_XY[0] - SHAFT_DX
    return [(x0 + hx, SPLINE_XY[1] + hy) for hx, hy in SG90.holes]


def dial_screw_xy() -> list[tuple[float, float]]:
    return [(SPLINE_XY[0] + dx, SPLINE_XY[1] + dy) for dx, dy in DIAL_SCREWS]


def dial_z(enc) -> float:
    """Bottom of the dial = outer face of the lid."""
    return lid_z(enc) + enc.spec.lid_thickness


def needle_z(enc) -> float:
    """Bottom of the needle: just above the servo turret."""
    return lid_z(enc) - POST_DROP - _TAB_TOP + TURRET_TOP + NEEDLE_GAP


# ---------------------------------------------------------------------------------- lid / base
def gauge_lid(enc, printer: str) -> PartSpec:
    """The enclosure lid + servo posts, turret hole and dial-screw bosses (printed outside down)."""
    zl, t = lid_z(enc), enc.spec.lid_thickness
    comp = hole_compensation(printer)
    m2 = get_size("M2")
    pilot = m2.tap_plastic + comp
    adds, cuts = [], []
    for x, y in servo_tab_holes(enc):  # tabs are screwed up into the posts (SG90 self-tappers)
        adds.append(_cyl(POST_D, zl - POST_DROP, zl + EPS, x, y))
        cuts.append(_cyl(pilot, zl - POST_DROP - EPS, zl + t - 0.8, x, y))  # 0.8 mm skin outside
    for x, y in dial_screw_xy():  # dial screws come from the top, through the lid into the bosses
        adds.append(_cyl(POST_D, zl - DIAL_BOSS_L, zl + EPS, x, y))
        cuts.append(_cyl(pilot, zl - DIAL_BOSS_L + 0.6, zl + t + EPS, x, y))
    cuts.append(_cyl(TURRET_D + 2 * TURRET_CLEAR + comp, zl - EPS, zl + t + EPS, *SPLINE_XY))
    shape = enc.lid.shape.fuse(*adds).clean().cut(*cuts).clean()
    return replace(enc.lid, shape=shape)


def fan_grille(enc, fan_center: tuple[float, float, float], window_d: float) -> bd.Part:
    """Vertical bars filling the round fan opening in the −Y wall (vertical bars print without
    support; the teardrop tip above the circle stays open)."""
    x0, _y, z0 = fan_center
    iw, wall = enc.inner_size[1], enc.spec.wall
    y_in, y_out = -iw / 2, -iw / 2 - wall
    n = int(window_d / 2 // GRILLE_PITCH)
    bars = [bd.Box(GRILLE_BAR, wall, window_d + 4).moved(bd.Location((x0 + k * GRILLE_PITCH, (y_in + y_out) / 2, z0)))
            for k in range(-n, n + 1)]
    disc = bd.Cylinder(window_d / 2 + 0.5, wall, rotation=(90, 0, 0)).moved(bd.Location((x0, (y_in + y_out) / 2, z0)))
    return bd.Part(bd.Compound(bars).wrapped) & disc


def gauge_base(enc, fan_center: tuple[float, float, float], window_d: float) -> PartSpec:
    """The enclosure base + vertical grille bars in the fan's round window.

    The enclosure cuts the window (a teardrop whose tip is flattened below the rim of this 34 mm
    wall); the bars every 4 mm guard the blades and carry the arc (≤ 2.8 mm spans).
    """
    shape = enc.base.shape.fuse(fan_grille(enc, fan_center, window_d)).clean()
    return replace(enc.base, shape=shape)


# ---------------------------------------------------------------------------------------- dial
def _polar(r: float, deg: float) -> tuple[float, float]:
    return r * math.cos(math.radians(deg)), r * math.sin(math.radians(deg))


def _scale_angle(temp_c: float) -> float:
    """Polar angle (degrees from +X) of a temperature on the dial."""
    return 90.0 + needle_angle(temp_c)


def _tick(r0: float, r1: float, width: float, deg: float, h: float) -> bd.Part:
    box = bd.Box(r1 - r0, width, h, align=(bd.Align.MIN, bd.Align.CENTER, bd.Align.MIN))
    return box.moved(bd.Location((0, 0, 0), (0, 0, deg)) * bd.Location((r0, 0, 0)))


def _arc_band(r0: float, r1: float, a0: float, a1: float, h: float) -> bd.Part:
    pts = [_polar(r1, a0 + (a1 - a0) * i / 32) for i in range(33)]
    pts += [_polar(r0, a1 - (a1 - a0) * i / 32) for i in range(33)]
    face = bd.Face(bd.Wire.make_polygon([bd.Vector(x, y, 0) for x, y in pts], close=True))
    return bd.extrude(face, h)


_SEGMENTS = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc"}
SEG_H, SEG_W, SEG_T = 4.4, 2.6, 0.9  # seven-segment numerals: every stroke ≥ 0.8 mm (two perimeters)


def _digit(ch: str, x0: float, y0: float, h: float) -> list:
    """Seven-segment digit with its lower-left corner at (x0, y0) — printable raised strokes (font
    text has sub-0.8 mm strokes and serifs that slicers drop)."""
    w, t = SEG_W, SEG_T
    half = SEG_H / 2
    boxes = {"a": (x0, y0 + SEG_H - t, w, t), "d": (x0, y0, w, t), "g": (x0, y0 + half - t / 2, w, t),
             "f": (x0, y0 + half - t / 2, t, half + t / 2), "b": (x0 + w - t, y0 + half - t / 2, t, half + t / 2),
             "e": (x0, y0, t, half + t / 2), "c": (x0 + w - t, y0, t, half + t / 2)}
    out = []
    for seg in _SEGMENTS[ch]:
        x, y, bw, bh = boxes[seg]
        out.append(bd.Box(bw, bh, h, align=(bd.Align.MIN, bd.Align.MIN, bd.Align.MIN)).moved(bd.Location((x, y, 0))))
    return out


def _number(text: str, cx: float, cy: float, h: float) -> list:
    gap = 0.8
    width = len(text) * SEG_W + (len(text) - 1) * gap
    out = []
    for i, ch in enumerate(text):
        out += _digit(ch, cx - width / 2 + i * (SEG_W + gap), cy - SEG_H / 2, h)
    return out


def dial_marks(z0: float = 0.0, h: float = MARK_H) -> bd.Part:
    """Scale marks (ticks every 2 °C, numerals every 10 °C, fan zone ≥ 28 °C, °C), z ∈ [z0, z0 + h]."""
    marks: list = []
    for t in range(int(T_MIN), int(T_MAX) + 1, 2):
        major = t % 10 == 0
        marks.append(_tick(18.5 if major else 21.0, 24.0, 1.4 if major else 0.9, _scale_angle(t), h))
        if major:
            marks += _number(str(t), *_polar(14.3, _scale_angle(t)), h)
    marks.append(_arc_band(24.0, 24.9, _scale_angle(T_MAX), _scale_angle(T_MIN), h))
    marks.append(_arc_band(25.4, 26.8, _scale_angle(T_MAX), _scale_angle(28.0), h))  # fan runs here
    # "°C" below the hub: a ring and an open arc, strokes 0.9 mm
    ring = bd.extrude(bd.Circle(1.3) - bd.Circle(0.4), h).moved(bd.Location((-3.2, -9.0, 0)))
    marks += [ring, _arc_band(1.6, 2.5, 50.0, 310.0, h).moved(bd.Location((1.2, -10.3, 0)))]
    out = marks[0].fuse(*marks[1:]).clean()
    return out.moved(bd.Location((0, 0, z0)))


def dial(printer: str) -> tuple[PartSpec, PartSpec]:
    """(printed dial: plate + marks as one solid, coloured scene model: white plate, dark marks).

    Dial frame: centre on the servo shaft, bottom on z = 0. Print face up; change filament at
    z = DIAL_T so the marks come out dark.
    """
    comp = hole_compensation(printer)
    clear = get_size("M2").clearance_normal + comp
    plate = bd.Part(bd.Cylinder(DIAL_R, DIAL_T, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)).wrapped)
    plate = plate.chamfer(0.4, None, plate.edges().group_by(bd.Axis.Z)[0])  # elephant's foot
    holes = [_cyl(TURRET_D + 2 * TURRET_CLEAR + comp, -EPS, DIAL_T + EPS)]
    holes += [_cyl(clear, -EPS, DIAL_T + EPS, dx, dy) for dx, dy in DIAL_SCREWS]
    plate = plate.cut(*holes).clean()
    marks = dial_marks(DIAL_T - EPS, MARK_H + EPS)
    printed = PartSpec("dial", plate.fuse(marks).clean(), kind="printed", material="PETG", color=WHITE,
                       print_rotation=(0, 0, 0), meta={"filament_change_mm": DIAL_T, "colors": [WHITE, INK]})
    plate.label, plate.color = "plate", bd.Color(WHITE)
    vis = dial_marks(DIAL_T, MARK_H)
    vis.label, vis.color = "marks", bd.Color(INK)
    scene = PartSpec("dial", bd.Compound(children=[plate, vis], label="dial"), kind="printed", material="PETG",
                     color=WHITE, print_rotation=(0, 0, 0), meta=dict(printed.meta))
    return printed, scene


# -------------------------------------------------------------------------------------- needle
def needle() -> PartSpec:
    """Needle along +Y (points at 20 °C at joint value 0), hub centred on the origin, bottom z = 0.

    Printed flat (bottom on the bed); the blind Ø4.9 bore is open at the bottom and slides onto
    the servo spline.
    """
    arm_t = 1.2
    w0, w1 = 3.0, 1.0  # ≥ 0.8 mm everywhere: two perimeters even at the tip
    pts = [(-w0 / 2, -NEEDLE_TAIL), (w0 / 2, -NEEDLE_TAIL), (w0 / 2, 0.0), (w1 / 2, NEEDLE_LEN),
           (-w1 / 2, NEEDLE_LEN), (-w0 / 2, 0.0)]
    arm = bd.extrude(bd.Face(bd.Wire.make_polygon([bd.Vector(x, y, 0) for x, y in pts], close=True)), arm_t)
    hub = _cyl(HUB_D, 0.0, HUB_H)
    weight = _cyl(5.0, 0.0, arm_t, 0.0, -NEEDLE_TAIL)  # counterweight disc at the tail
    body = arm.fuse(hub, weight).clean()
    spline_engage = SPLINE_TOP - TURRET_TOP - NEEDLE_GAP  # spline length inside the hub
    body = body.cut(_cyl(SPLINE_BORE, -EPS, min(spline_engage + 0.1, HUB_H - 0.9))).clean()
    return PartSpec("needle", body, kind="printed", material="PETG", color=RED, print_rotation=(0, 0, 0))


# ------------------------------------------------------------------------------------ modules
def bme280_module():
    """GY-BME280 breakout with its 4-pin header soldered on the sensor side (pins point into the
    box when the board sits on wall standoffs) — the stock model has the header underneath."""
    m = get_module("bme280_breakout")
    pcb_t = m.pcb[2]
    comps = tuple(c for c in m.components if c[0] != "header")
    # src: 2.54 mm pin header, 4 pins (10.16 mm), 8.5 mm overall — same as the stock model, flipped up
    header = ("header", "box", (0.0, -4.2, pcb_t + 4.25), (10.16, 2.54, 8.5))
    return replace(m, components=comps + (header,), source=m.source + "; header on the sensor side")


def m2_screw(length: int) -> PartSpec:
    return PartSpec(f"M2x{length} socket screw", screw("M2", length), kind="fastener", material="steel",
                    color="#9aa0a6", meta={"size": "M2", "length": length})
