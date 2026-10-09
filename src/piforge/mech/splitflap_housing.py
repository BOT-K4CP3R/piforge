"""Closed product housing for a row of split-flap modules (:mod:`piforge.mech.splitflap`).

One matte-black box around the whole display: a front panel whose windows show **only the flap
face** (a uniform border overlaps the flaps' edges; a black light-tunnel ("shroud") behind every
window hides the spools, frames and motors), the comma inlaid in the front panel, a floor the
modules screw onto from below, a top, two side walls and a removable back cover. Behind the
modules is the electronics compartment: one ULN2003 board per module on the floor, the Pi and
the 74HCT595 perfboard hanging under the top, the DC jack in the back cover, vents in the back
cover and the floor, cable-tie anchors, rubber-foot recesses and wall-mount keyholes.

Frame (binding, mm, Z up — the same as the modules'): the display faces −Y; the row of modules is
centred on x = 0, module ``i`` (0 = leftmost) has its flap centre at :meth:`SplitFlapHousing.module_x`
and stands on the floor's top face z = 0; the spool axis is at ``z = axis_z``.

Why the front panel sits so far forward: a released flap swings forward about its pin through
the horizontal, its free edge reaching ``flap_length`` in front of the pin (≈ 45 mm in front of the
axis for the default module). The panel's back face is ``swing_clear`` in front of that arc, so the
flaps never touch it and the window can be smaller than the flap face. The shroud walls run from
the panel back to just in front of the module frames (``frame_front``).

Printing (220 × 220 × 250 bed by default): every part is cut into segments of at most
``bed − seg_margin`` at hidden places — front joints between two windows (two joint ribs, a
``splice`` plate screwed from behind, two Ø3 dowels), floor/top/back joints between two modules
(a pair of ``pillar`` posts bolted together, Ø3 dowels), and the front joints are staggered
against the floor/top joints so the box is rigid once screwed together. Print orientations: front
face down (windows are plain holes, shrouds and ribs grow upwards), floor bottom down, top outer
face down, back cover outer face down, side walls outer face down, pillars lying — no supports.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

import build123d as bd
import numpy as np
from build123d import Location, Part

from piforge.core.errors import ValidationError
from piforge.core.report import Report, Severity
from piforge.mech.assembly import Assembly, Joint
from piforge.mech.fasteners import get_size
from piforge.mech.part import PartSpec
from piforge.mech.primitives import EPS, _as_part
from piforge.mech.splitflap import (
    SplitFlapModule, SplitFlapSpec, _box, _cyl_x, _cyl_y, _cyl_z, _plate_xz, _union,
)

__all__ = ["HousingElectronics", "HousingSpec", "SplitFlapHousing", "DARK"]

DARK = "#16171a"  # matte black (housing, and the module colours a housing defaults to)
_ALLOWED_SEAM = ("spool", "spool_cap")  # visible through the 0.4 mm split between the flaps


# ---------------------------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class HousingElectronics:
    """What goes into the electronics compartment (``None`` leaves a part out).

    ``driver`` is one module-database board per digit (on floor bosses behind its module);
    ``board`` (a :func:`~piforge.mech.get_board` key) and ``perfboard`` hang under the top;
    ``dc_jack`` is a panel module in the back cover; ``shift_registers`` DIP-16 packages are drawn
    on the perfboard.
    """

    board: str | None = "rpizero2w"
    perfboard: str | None = "perfboard_50x70"
    driver: str | None = "uln2003_board"
    dc_jack: str | None = "dc_jack_panel_55x21"
    shift_registers: int = 4


@dataclass(frozen=True)
class HousingSpec:
    """Housing parameters (mm). Defaults: 3 mm walls, 5 mm floor, 54 mm electronics compartment.

    * ``window_border`` 2.0: the window is the visible flap face (both halves) shrunk by this on
      every side — more than the 1.5 mm the stop tabs reach over the flap edges, so neither the
      stops nor a flap edge ever show.
    * ``swing_clear`` 1.5: front panel back face to the falling flap's arc.
    * ``shroud_gap`` 0.6 / ``shroud_wall`` 1.2: light tunnel behind each window, ``shroud_gap``
      beside the flap edges and above/below the flap faces.
    * ``compartment`` 54: frame back → back cover inner face (the 70 × 50 perfboard + 2 mm).
    """

    wall: float = 3.0
    floor: float = 5.0
    side_clear: float = 1.0
    top_clear: float = 1.5
    swing_clear: float = 1.5
    window_border: float = 2.0
    window_radius: float = 1.0
    shroud_gap: float = 0.6
    shroud_wall: float = 1.2
    compartment: float = 54.0
    rib_depth: float = 8.0
    rib_height: float = 6.0
    joint_rib: float = 7.0
    joint_rib_depth: float = 10.0
    splice_t: float = 4.0
    pillar: tuple[float, float] = (10.0, 12.0)  # x × y
    boss_d: float = 6.5
    boss_h: float = 6.0
    comma_depth: float = 1.2
    seg_margin: float = 2.0
    stagger: float = 30.0
    feet: bool = True
    keyholes: bool = True
    vents: bool = True
    color: str = DARK
    comma_color: str = "#f2f2f2"
    face_color: str = "#0b0b0c"

    def __post_init__(self) -> None:
        for name in ("wall", "floor", "swing_clear", "window_border", "shroud_gap", "shroud_wall",
                     "compartment", "rib_depth", "rib_height", "joint_rib", "splice_t", "boss_d", "boss_h",
                     "comma_depth"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                raise ValidationError(f"HousingSpec.{name} must be a positive number, got {v!r}")
        if self.comma_depth >= self.wall - 0.8:
            raise ValidationError("HousingSpec.comma_depth must leave >= 0.8 mm of front panel")


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _rrect_xz(cx: float, cz: float, w: float, h: float, y0: float, y1: float, r: float) -> Part:
    return _plate_xz(cx - w / 2, cx + w / 2, y0, y1, cz - h / 2, cz + h / 2, r)


def _hex_y(af: float, y0: float, y1: float, x: float, z: float) -> Part:
    """Hex prism (nut pocket) along +Y, flats ``af`` apart."""
    sk = bd.RegularPolygon(af / math.sqrt(3), 6, major_radius=True)
    plane = bd.Plane(origin=(x, y0, z), x_dir=(1, 0, 0), z_dir=(0, 1, 0))
    return _as_part(bd.extrude(sk.moved(plane.location), y1 - y0))


def _hex_x(af: float, x0: float, x1: float, y: float, z: float) -> Part:
    """Hex prism along +X with a vertex up (a 60° roof: prints without support lying on its side)."""
    sk = bd.RegularPolygon(af / math.sqrt(3), 6, major_radius=True)
    plane = bd.Plane(origin=(x0, y, z), x_dir=(0, 1, 0), z_dir=(1, 0, 0))
    return _as_part(bd.extrude(sk.moved(plane.location), x1 - x0))


def _even(lo: float, hi: float, pitch: float, *, min_n: int = 2) -> list[float]:
    """Evenly spaced positions in [lo, hi] at most ``pitch`` apart (ends included)."""
    if hi - lo < 1e-6:
        return [(lo + hi) / 2]
    n = max(min_n, int(math.ceil((hi - lo) / pitch)) + 1)
    return [lo + (hi - lo) * k / (n - 1) for k in range(n)]


def _far(x: float, avoid: list[tuple[float, float]]) -> bool:
    return all(not (a <= x <= b) for a, b in avoid)


# ---------------------------------------------------------------------------------------------
# housing
# ---------------------------------------------------------------------------------------------
class SplitFlapHousing:
    """A closed housing around ``n_digits`` split-flap modules + a comma (see the module docstring).

    ``SplitFlapHousing()`` = 8 digits, comma after the 6th (``000000,00``), default module with
    black spools/frames, Pi Zero 2 W + perfboard + 8 ULN2003 boards + DC jack.
    ``spec`` may be a :class:`SplitFlapSpec` or a ready :class:`SplitFlapModule`;
    ``comma_after`` counts digits (6 → the comma sits between digit 6 and 7; None = no comma).
    Housing parameters: ``housing=HousingSpec(...)`` or keyword overrides.
    """

    def __init__(self, spec: SplitFlapSpec | SplitFlapModule | None = None, *, n_digits: int = 8,
                 comma_after: int | None = 6, electronics: HousingElectronics | None = HousingElectronics(),
                 housing: HousingSpec | None = None, **overrides: Any):
        if isinstance(spec, SplitFlapModule):
            self.module = spec
        else:
            self.module = SplitFlapModule(spec or SplitFlapSpec(spool_color=DARK, frame_color=DARK))
        if not isinstance(n_digits, int) or n_digits < 1:
            raise ValidationError(f"n_digits must be a positive integer, got {n_digits!r}")
        if comma_after is not None and not (isinstance(comma_after, int) and 1 <= comma_after < n_digits):
            raise ValidationError(f"comma_after must be 1…{n_digits - 1} digits (or None), got {comma_after!r}")
        hs = housing or HousingSpec()
        self.spec = replace(hs, **overrides) if overrides else hs
        self.n = n_digits
        self.comma_after = comma_after
        self.comma_idx = None if comma_after is None else comma_after - 1  # module index the comma follows
        self.electronics = electronics
        m, s, h = self.module, self.module.spec, self.spec
        self.printer = m.printer
        comp = self.printer.hole_compensation
        self.axis_z = m.axis_z
        # -- row (x) ---------------------------------------------------------------------------------
        self.row_width = self.n * m.pitch + (s.comma_width if self.comma_idx is not None else 0.0)
        self._x0 = -self.row_width / 2 + m.left_ext
        self.x_in = self.row_width / 2 + h.side_clear
        self.x_out = self.x_in + h.wall
        # -- visible flap face (relative to the axis): flap 0 standing, flap n-1 hanging ----------
        poses = m.rest_poses
        y0, z0, a0 = poses[0]
        yb, zb, ab = poses[s.flaps - 1]
        self.face_top = z0 + m.flap_length * math.cos(math.radians(a0))
        self.face_bottom = zb + m.flap_length * math.cos(math.radians(ab))
        self.face_y = y0 - s.flap_thickness / 2  # front face of the shown flaps (≈ −14.4)
        self.window_w = s.flap_width - 2 * h.window_border
        self.window_z = (self.face_bottom + h.window_border, self.face_top - h.window_border)
        self.window_h = self.window_z[1] - self.window_z[0]
        self.window_cz = (self.window_z[0] + self.window_z[1]) / 2
        # -- depth (y) -------------------------------------------------------------------------------
        adv0 = m.release_advance or m.pitch_angle / 2
        fall = [m.pin_position(0, a)[0] for a in np.linspace(adv0, m.pitch_angle, 31)]
        self.swing_y = min(fall) - m.flap_length  # the falling flap's free edge, most forward
        self.y_panel = self.swing_y - h.swing_clear  # front panel back face
        self.y_front = self.y_panel - h.wall  # front face (window plane)
        self.y_shroud_end = s.frame_front - 0.3
        self.y_back_in = s.frame_back + h.compartment
        self.y_back = self.y_back_in + h.wall
        # -- height (z) ------------------------------------------------------------------------------
        self.z_top_in = s.module_height + h.top_clear
        self.z_min = -h.floor
        self.z_max = self.z_top_in + h.wall
        # shroud (light tunnel) inner/outer extents, relative to a window centre / the axis
        self.shroud_x_in = s.flap_width / 2 + h.shroud_gap
        self.shroud_x_out = self.shroud_x_in + h.shroud_wall
        self.shroud_z_in = (self.face_bottom - h.shroud_gap, self.face_top + h.shroud_gap)
        self.shroud_z_out = (self.shroud_z_in[0] - h.shroud_wall, self.shroud_z_in[1] + h.shroud_wall)
        # fasteners
        m3 = get_size("M3")
        self.tap3 = m3.tap_plastic + comp
        self.clear3 = m3.clearance("normal") + comp
        self.cb3 = m3.head_socket_d + 0.6 + comp
        self.cb3_depth = m3.head_socket_h + 0.2
        self.nut3 = m3.nut_af + 0.3 + comp
        self.dowel_d = 3.0 + 0.1 + comp  # src: Ø3 × 8 steel dowel pin (ISO 8734) or 3 mm filament, slip fit
        self.dowel_depth = 4.5  # Ø3 × 8 dowel, 4 mm in each part
        # bed
        self.bed = (self.printer.build_x, self.printer.build_y, self.printer.build_z)
        self.seg_limit = max(self.printer.build_x, self.printer.build_y) - h.seg_margin
        if self.y_shroud_end - self.y_panel < 2.0:
            raise ValidationError("the module's frame_front is too close to the front panel for a shroud")
        if self.window_w <= 10 or self.window_h <= s.digit_height:
            raise ValidationError(f"window {self.window_w:.1f} × {self.window_h:.1f} mm cannot show a "
                                  f"{s.digit_height:g} mm digit: reduce window_border")

    # -- layout ------------------------------------------------------------------------------------
    def module_x(self, i: int) -> float:
        """x of module ``i``'s flap centre (= its window centre)."""
        if not 0 <= i < self.n:
            raise ValidationError(f"module index must be 0…{self.n - 1}, got {i!r}")
        return self._x0 + self.module.module_x(i, self.comma_idx)

    @property
    def comma_x(self) -> float | None:
        """x of the comma glyph centre: midway between the two windows it separates."""
        if self.comma_idx is None:
            return None
        return (self.module_x(self.comma_idx) + self.module_x(self.comma_idx + 1)) / 2

    def window_rect(self, i: int) -> tuple[float, float, float, float]:
        """(x0, x1, z0, z1) of window ``i`` in the front plane."""
        cx = self.module_x(i)
        return (cx - self.window_w / 2, cx + self.window_w / 2,
                self.axis_z + self.window_z[0], self.axis_z + self.window_z[1])

    @property
    def outer_size(self) -> tuple[float, float, float]:
        return (2 * self.x_out, self.y_back - self.y_front, self.z_max - self.z_min)

    @cached_property
    def _comma_halfwidth(self) -> float:
        if self.comma_idx is None:
            return 0.0
        from piforge.mech.splitflap_art import comma_faces

        xs = [v.X for f in comma_faces(self.module) for v in f.vertices()]
        return max(abs(min(xs)), abs(max(xs)))

    def _front_intervals(self) -> list[tuple[int, float, float]]:
        """Allowed x intervals for a front joint in each gap between two windows."""
        h = self.spec
        reach = self.shroud_x_out + h.joint_rib + 2.5  # joint rib + splice overhang (1.5) beside the shroud
        out = []
        for j in range(self.n - 1):
            a, b = self.module_x(j) + reach, self.module_x(j + 1) - reach
            if self.comma_idx == j:
                c, cw = self.comma_x, self._comma_halfwidth + 6.0
                out += [(j, a, min(b, c - cw)), (j, max(a, c + cw), b)]
            else:
                out.append((j, a, b))
        return [(j, a, b) for j, a, b in out if b > a]

    def _floor_intervals(self) -> list[tuple[int, float, float]]:
        """Allowed x intervals for a floor/top/back joint: between two modules' foot screws."""
        m = self.module
        cb = m.post_x[1] + self.cb3 / 2 + 1.5
        cb_l = -m.post_x[0] + self.cb3 / 2 + 1.5
        px = self.spec.pillar[0]
        out = []
        for j in range(self.n - 1):
            a = self.module_x(j) + max(cb, self._driver_halfwidth() + px + 1.0)
            b = self.module_x(j + 1) - max(cb_l, self._driver_halfwidth() + px + 1.0)
            if b > a:
                out.append((j, a, b))
        return out

    def _driver_halfwidth(self) -> float:
        if not self.electronics or not self.electronics.driver:
            return 0.0
        from piforge.mech.modules import get_module

        d = get_module(self.electronics.driver)
        return max(d.pcb[0] / 2, max(abs(x) for x, _ in d.holes) + self.spec.boss_d / 2)

    @staticmethod
    def _plan(lo: float, hi: float, intervals: list[tuple[int, float, float]], limit: float,
              avoid: list[float] = (), stagger: float = 0.0, extra_end: float = 0.0) -> list[float]:
        """Joint x positions splitting [lo, hi] into segments <= ``limit`` (ends grown by
        ``extra_end``): fewest segments, then the most even; joints kept ``stagger`` away from
        ``avoid`` when possible."""
        cands: dict[int, list[float]] = {}
        for j, a, b in intervals:
            cands.setdefault(j, []).extend([a, (a + b) / 2, b])
        gaps = sorted(cands)
        total = hi - lo + 2 * extra_end
        k_min = max(0, int(math.ceil(total / limit)) - 1)
        for strict in (True, False):
            for k in range(k_min, len(gaps) + 1):
                best = None
                for sub in itertools.combinations(gaps, k):
                    for pos in itertools.product(*(cands[g] for g in sub)):
                        if strict and any(abs(p - a) < stagger for p in pos for a in avoid):
                            continue
                        edges = [lo - extra_end, *sorted(pos), hi + extra_end]
                        widths = np.diff(edges)
                        if widths.max() > limit or widths.min() < 20.0:
                            continue
                        score = (float(widths.max()), -float(widths.min()))
                        if best is None or score < best[0]:
                            best = (score, sorted(pos))
                if best is not None:
                    return best[1]
        raise ValidationError(f"cannot split {total:.0f} mm into printable segments of <= {limit:.0f} mm")

    @cached_property
    def front_joints(self) -> list[float]:
        """x of the front-panel joints (between two windows, beside the comma)."""
        return self._plan(-self.x_in, self.x_in, self._front_intervals(), self.seg_limit,
                          extra_end=self.spec.wall)

    @cached_property
    def floor_joints(self) -> list[float]:
        """x of the floor/top/back joints (between two modules, staggered against the front)."""
        return self._plan(-self.x_in, self.x_in, self._floor_intervals(), self.seg_limit,
                          avoid=self.front_joints, stagger=self.spec.stagger, extra_end=self.spec.wall)

    def _segments(self, joints: list[float], lo: float, hi: float) -> list[tuple[float, float]]:
        edges = [lo, *joints, hi]
        return list(zip(edges[:-1], edges[1:]))

    # -- electronics layout ---------------------------------------------------------------------------
    @cached_property
    def electronics_layout(self) -> dict[str, Any]:
        """Positions of the electronics (housing frame): drivers per module, the board and the
        perfboard under the top, the DC jack in the back cover."""
        from piforge.mech.boards import get_board
        from piforge.mech.modules import get_module

        e = self.electronics
        s, h = self.module.spec, self.spec
        out: dict[str, Any] = {"drivers": [], "board": None, "perfboard": None, "dc_jack": None}
        if not e:
            return out
        if e.driver:
            d = get_module(e.driver)
            yc = s.frame_back + 4.0 + d.pcb[1] / 2
            out["drivers"] = [(self.module_x(i), yc) for i in range(self.n)]
        segs = self._segments(self.floor_joints, -self.x_in, self.x_in)
        k = max(0, len(segs) // 2 - 1)
        if e.perfboard:
            pb = get_module(e.perfboard)
            a, b = segs[k]
            x = (a + b) / 2
            out["perfboard"] = (x, s.frame_back + 1.5 + pb.pcb[1] / 2, pb.pcb[0], pb.pcb[1])
        if e.board:
            bd_ = get_board(e.board)
            a, b = segs[min(k + 1, len(segs) - 1)] if e.perfboard else segs[k]
            x = (a + b) / 2
            out["board"] = (x, s.frame_back + 2.0 + bd_.width / 2, bd_.length, bd_.width)
        if e.dc_jack:
            x = out["perfboard"][0] + 20.0 if out["perfboard"] else 0.0
            out["dc_jack"] = (x, self.axis_z - 15.0)
        return out

    def _boss_points_top(self) -> list[tuple[float, float, str]]:
        """(x, y, size) of the bosses hanging under the top (board + perfboard holes)."""
        from piforge.mech.boards import get_board
        from piforge.mech.modules import get_module

        lay, e = self.electronics_layout, self.electronics
        pts: list[tuple[float, float, str]] = []
        if lay["perfboard"]:
            x, y, _, _ = lay["perfboard"]
            pts += [(x + hx, y + hy, "M2") for hx, hy in get_module(e.perfboard).holes]
        if lay["board"]:
            b = get_board(e.board)
            x, y, L, W = lay["board"]
            # hung upside down (rotated 180° about X): board (u, v) → (x - L/2 + u, y + W/2 - v)
            pts += [(x - L / 2 + u, y + W / 2 - v, "M2.5") for u, v in b.holes]
        return pts

    def _electronics_x_ranges(self) -> list[tuple[float, float]]:
        lay = self.electronics_layout
        out = []
        for key in ("perfboard", "board"):
            if lay[key]:
                x, _y, L, _W = lay[key]
                out.append((x - L / 2 - 8.0, x + L / 2 + 8.0))
        return out

    # -- fastener positions -------------------------------------------------------------------------
    @cached_property
    def _front_rib_screws(self) -> list[float]:
        """x of the vertical screws floor → front bottom rib and top → front top rib."""
        h = self.spec
        out = []
        avoid = [(j - 8, j + 8) for j in self.front_joints + self.floor_joints]
        for a, b in self._segments(self.front_joints, -self.x_in, self.x_in):
            for x in _even(a + 10.0, b - 10.0, 60.0):
                if _far(x, avoid):
                    out.append(x)
        return out

    def _side_rib_y(self) -> list[float]:
        """y of the vertical screws into the side walls' bottom/top ribs (front + compartment)."""
        h, s = self.spec, self.module.spec
        front = (self.y_panel + h.rib_depth + 5.0 + self.y_shroud_end) / 2
        return [front, s.frame_back + 8.0, self.y_back_in - h.joint_rib_depth - 10.0]

    @cached_property
    def _lip_x(self) -> dict[str, list[float]]:
        """x of the back-cover screw lips on the floor and under the top."""
        h = self.spec
        px = h.pillar[0]
        avoid = [(j - px - 8.0, j + px + 8.0) for j in self.floor_joints]
        avoid += [(-self.x_in - 1, -self.x_in + 16.0), (self.x_in - 16.0, self.x_in + 1)]
        floor, top = [], []
        for a, b in self._segments(self.floor_joints, -self.x_in, self.x_in):
            for x in _even(a + 25.0, b - 25.0, 75.0):
                if _far(x, avoid):
                    floor.append(x)
                    if _far(x, self._electronics_x_ranges()):
                        top.append(x)
        return {"floor": floor, "top": top}

    @property
    def _pillar_y(self) -> tuple[float, float]:
        return self.y_back_in - self.spec.pillar[1], self.y_back_in

    def _cover_screws(self) -> list[tuple[float, float]]:
        """(x, z) of every back-cover screw."""
        h = self.spec
        px = h.pillar[0]
        zt = self.z_top_in
        pts = []
        for j in self.floor_joints:
            for x in (j - px / 2, j + px / 2):
                pts += [(x, 0.3 * zt), (x, 0.7 * zt)]
        for sx in (-1, 1):
            x = sx * (self.x_in - h.rib_depth / 2)
            pts += [(x, 0.3 * zt), (x, 0.7 * zt)]
        pts += [(x, 5.0) for x in self._lip_x["floor"]]
        pts += [(x, zt - 5.0) for x in self._lip_x["top"]]
        return pts

    # -- printed parts ---------------------------------------------------------------------------------
    def _windows_in(self, a: float, b: float) -> list[int]:
        return [i for i in range(self.n) if a < self.module_x(i) < b]

    def _front_body(self, a: float, b: float, *, left_end: bool, right_end: bool,
                    ribs: bool = True) -> Part:
        """Front panel between x = a and b: plate, windows, shrouds, ribs, joint ribs, comma pocket."""
        h, s = self.spec, self.module.spec
        za = self.axis_z
        yp, yf = self.y_panel, self.y_front
        plate = _box(a, b, yf, yp, self.z_min, self.z_max)
        adds: list[Part] = []
        cuts: list[Part] = []
        for i in self._windows_in(a, b):
            cx = self.module_x(i)
            cuts.append(_rrect_xz(cx, za + self.window_cz, self.window_w, self.window_h, yf - EPS, yp + EPS,
                                  h.window_radius))
            # light tunnel: four walls from the panel back to just before the module frames
            xo, xi = self.shroud_x_out, self.shroud_x_in
            zi0, zi1 = self.shroud_z_in
            zo0, zo1 = self.shroud_z_out
            y0, y1 = yp - EPS, self.y_shroud_end
            tunnel = _as_part(_box(cx - xo, cx + xo, y0, y1, za + zo0, za + zo1)
                              - _box(cx - xi, cx + xi, y0 - 1, y1 + 1, za + zi0, za + zi1))
            adds.append(tunnel)
        if ribs:
            xa, xb = max(a, -self.x_in), min(b, self.x_in)
            adds.append(_box(xa, xb, yp - EPS, yp + h.rib_depth, 0.0, h.rib_height))
            adds.append(_box(xa, xb, yp - EPS, yp + h.rib_depth, self.z_top_in - h.rib_height, self.z_top_in))
            for x in self._front_rib_screws:
                if xa + 3 < x < xb - 3:
                    y = yp + h.rib_depth / 2
                    cuts.append(_cyl_z(self.tap3 / 2, -EPS, h.rib_height - 1.0, x, y))
                    cuts.append(_cyl_z(self.tap3 / 2, self.z_top_in - h.rib_height + 1.0, self.z_top_in + EPS, x, y))
            jr, jd = h.joint_rib, h.joint_rib_depth
            for x_edge, sign in ((a, 1), (b, -1)):
                if (sign == 1 and left_end) or (sign == -1 and right_end):
                    continue
                x0, x1 = sorted((x_edge, x_edge + sign * jr))
                adds.append(_box(x0, x1, yp - EPS, yp + jd, 0.0, self.z_top_in))
                xc = (x0 + x1) / 2
                for z in self._splice_z():  # splice screws from behind (tapped)
                    cuts.append(_cyl_y(self.tap3 / 2, yp + 1.5, yp + jd + EPS, xc, z))
                for z in (za - 18.0, za + 18.0):  # Ø3 dowels across the joint
                    xa0, xa1 = sorted((x_edge, x_edge + sign * self.dowel_depth))
                    cuts.append(_cyl_x(self.dowel_d / 2, xa0 - EPS, xa1 + EPS, yp + jd / 2, z))
        cx = self.comma_x
        if cx is not None and a < cx < b:
            cuts.append(self._comma_solid(-EPS, self.spec.comma_depth))
        return _as_part(_as_part(plate + _union(adds)) - _union(cuts))

    def _splice_z(self) -> tuple[float, float]:
        return (self.spec.rib_height + 5.0, self.z_top_in - self.spec.rib_height - 5.0)

    def _comma_solid(self, d0: float, d1: float) -> Part:
        """The comma glyph between y = y_front + d0 and y_front + d1 at the comma position."""
        from piforge.mech.splitflap_art import comma_faces

        faces = comma_faces(self.module)
        solids = [_as_part(bd.extrude(f, d1 - d0).moved(
            Location((self.comma_x, self.y_front, self.axis_z), (90, 0, 0)) * Location((0, 0, -d1))))
            for f in faces]
        return _union(solids)

    @cached_property
    def front_parts(self) -> list[PartSpec]:
        """Front panel segments ``front_0…`` (printed face down)."""
        h = self.spec
        segs = self._segments(self.front_joints, -self.x_out, self.x_out)
        out = []
        for k, (a, b) in enumerate(segs):
            body = self._front_body(a, b, left_end=k == 0, right_end=k == len(segs) - 1)
            out.append(PartSpec(f"front_{k}", body, material=self.module.spec.material, color=h.color,
                                print_rotation=(90, 0, 0),
                                meta={"role": "split-flap housing front panel", "x_range": [a, b],
                                      "windows": self._windows_in(a, b)}))
        return out

    @cached_property
    def comma_inlay(self) -> PartSpec | None:
        """The comma, inlaid ``comma_depth`` into the front panel (second colour / glued in)."""
        if self.comma_x is None:
            return None
        return PartSpec("comma_inlay", self._comma_solid(0.0, self.spec.comma_depth),
                        material=self.module.spec.material, color=self.spec.comma_color, print_rotation=(90, 0, 0),
                        meta={"role": "comma inlay in the front panel (second colour)"})

    @cached_property
    def splice(self) -> PartSpec | None:
        """Plate bridging two front joint ribs from behind (4 × M3 into the ribs)."""
        if not self.front_joints:
            return None
        h = self.spec
        jr = h.joint_rib
        y0 = self.y_panel + h.joint_rib_depth
        y1 = y0 + h.splice_t
        z0, z1 = h.rib_height + 1.0, self.z_top_in - h.rib_height - 1.0
        plate = _box(-jr - 1.5, jr + 1.5, y0, y1, z0, z1)
        cuts = []
        for x in (-jr / 2, jr / 2):
            for z in self._splice_z():
                cuts.append(_cyl_y(self.clear3 / 2, y0 - EPS, y1 + EPS, x, z))
        shape = _as_part(plate - _union(cuts))
        return PartSpec("splice", shape, material=self.module.spec.material, color=h.color,
                        quantity=len(self.front_joints), print_rotation=(-90, 0, 0),
                        meta={"role": "front joint splice (local x = 0 at the joint)"})

    @cached_property
    def side_parts(self) -> list[PartSpec]:
        """``side_left`` / ``side_right``: end walls with bottom/top/back ribs (printed outer face down)."""
        h, s = self.spec, self.module.spec
        out = []
        for name, sx in (("side_left", -1), ("side_right", 1)):
            xo, xi = sx * self.x_out, sx * self.x_in
            xr = xi - sx * h.rib_depth  # rib inner face
            wall = _box(min(xo, xi), max(xo, xi), self.y_panel + EPS, self.y_back_in, self.z_min, self.z_max)
            x0r, x1r = min(xi, xr), max(xi, xr)
            ribs = []
            for y0, y1 in ((self.y_panel + h.rib_depth + 0.5, self.y_shroud_end - 0.5),
                           (s.frame_back + 1.0, self.y_back_in)):
                for z0, z1 in ((0.0, h.rib_height), (self.z_top_in - h.rib_height, self.z_top_in)):
                    ribs.append(_box(x0r, x1r, y0, y1, z0, z1))
            ribs.append(_box(x0r, x1r, self.y_back_in - h.joint_rib_depth, self.y_back_in, 0.0, self.z_top_in))
            body = _as_part(wall + _union(ribs))
            cuts = []
            xc = (xi + xr) / 2
            for y in self._side_rib_y():
                cuts.append(_cyl_z(self.tap3 / 2, -EPS, h.rib_height - 1.0, xc, y))
                cuts.append(_cyl_z(self.tap3 / 2, self.z_top_in - h.rib_height + 1.0, self.z_top_in + EPS, xc, y))
            for x, z in self._cover_screws():
                if abs(x - xc) < 0.5:
                    cuts.append(_cyl_y(self.tap3 / 2, self.y_back_in - 8.0, self.y_back_in + EPS, xc, z))
            shape = _as_part(body - _union(cuts))
            out.append(PartSpec(name, shape, material=s.material, color=h.color,
                                print_rotation=(0, 90 * sx, 0),
                                meta={"role": "split-flap housing side wall"}))
        return out

    @cached_property
    def floor_parts(self) -> list[PartSpec]:
        """Floor segments ``floor_0…``: module foot screws, driver bosses, vents, lips, anchors, feet."""
        from piforge.mech.modules import get_module

        h, m, s = self.spec, self.module, self.module.spec
        segs = self._segments(self.floor_joints, -self.x_in, self.x_in)
        lay = self.electronics_layout
        drv = get_module(self.electronics.driver) if lay["drivers"] else None
        out = []
        for k, (a, b) in enumerate(segs):
            plate = _box(a, b, self.y_panel, self.y_back_in, self.z_min, 0.0)
            adds, cuts = [], []

            def screw_up(x: float, y: float) -> None:  # counterbored from below, through the floor
                cuts.append(_cyl_z(self.clear3 / 2, self.z_min - EPS, EPS, x, y))
                cuts.append(_cyl_z(self.cb3 / 2, self.z_min - EPS, self.z_min + self.cb3_depth, x, y))

            for i in range(self.n):
                cx = self.module_x(i)
                if not a < cx < b:
                    continue
                for px in m.post_x:
                    for fy in m.foot_y:
                        screw_up(cx + px, fy)
                if drv is not None:
                    dx, dy = lay["drivers"][i]
                    for hx, hy in drv.holes:
                        adds.append(_cyl_z(h.boss_d / 2, -EPS, h.boss_h, dx + hx, dy + hy))
                        cuts.append(_cyl_z(self.tap3 / 2, 1.0 - h.floor, h.boss_h + EPS, dx + hx, dy + hy))
                    if h.vents:
                        for u in (-8.0, -4.0, 0.0, 4.0, 8.0):
                            cuts.append(_box(dx + u - 1.25, dx + u + 1.25, dy - 9.0, dy + 9.0,
                                             self.z_min - EPS, EPS))
                    # cable-tie anchor behind the driver: a 4 × 2 mm tunnel along Y
                    ay = dy + drv.pcb[1] / 2 + 4.0
                    adds.append(_box(dx - 5.0, dx + 5.0, ay - 3.0, ay + 3.0, -EPS, 6.0))
                    cuts.append(_box(dx - 2.0, dx + 2.0, ay - 3.0 - EPS, ay + 3.0 + EPS, 1.8, 3.8))
            for x in self._front_rib_screws:
                if a + 4 < x < b - 4:
                    screw_up(x, self.y_panel + h.rib_depth / 2)
            for sx in (-1, 1):
                xc = sx * (self.x_in - h.rib_depth / 2)
                if a < xc < b:
                    for y in self._side_rib_y():
                        screw_up(xc, y)
            px, py = h.pillar
            yp0, yp1 = self._pillar_y
            for j in self.floor_joints:
                for xc in (j - px / 2, j + px / 2):
                    if a < xc < b:
                        screw_up(xc, (yp0 + yp1) / 2)
            for x in self._lip_x["floor"]:
                if a < x < b:
                    adds.append(_box(x - 6.0, x + 6.0, self.y_back_in - 8.0, self.y_back_in, -EPS, 10.0))
                    cuts.append(_cyl_y(self.tap3 / 2, self.y_back_in - 8.0 - EPS, self.y_back_in + EPS, x, 5.0))
            if h.feet:  # Ø12 × 1 recesses for stick-on rubber feet
                xm = (a + b) / 2
                for y in (self.y_panel + 14.0, self.y_back_in - 16.0):
                    cuts.append(_cyl_z(6.2, self.z_min - EPS, self.z_min + 1.0, xm, y))
            body = _as_part(plate + _union(adds)) if adds else plate
            shape = _as_part(body - _union(cuts))
            out.append(PartSpec(f"floor_{k}", shape, material=s.material, color=h.color, print_rotation=(0, 0, 0),
                                meta={"role": "split-flap housing floor", "x_range": [a, b]}))
        return out

    @cached_property
    def pillar(self) -> PartSpec | None:
        """Back corner post at a floor/top joint (local x ∈ [0, px] with the joint at x = 0, y = the
        housing's): two bolt together across the joint (nut in the hex pocket on the outer face)."""
        if not self.floor_joints:
            return None
        h = self.spec
        px, py = h.pillar
        y0, y1 = self._pillar_y
        yc = (y0 + y1) / 2
        zt = self.z_top_in
        body = _box(0.0, px, y0, y1, 0.0, zt)
        cuts = [_cyl_z(self.tap3 / 2, -EPS, 9.0, px / 2, yc), _cyl_z(self.tap3 / 2, zt - 9.0, zt + EPS, px / 2, yc)]
        for z in (0.3 * zt, 0.7 * zt):  # back cover screws (tapped from the back)
            cuts.append(_cyl_y(self.tap3 / 2, y1 - 8.0, y1 + EPS, px / 2, z))
        for z in (0.5 * zt - 12.0, 0.5 * zt + 12.0):  # M3 × 25 across the joint, nut on the outer face
            cuts.append(_cyl_x(self.clear3 / 2, -EPS, px + EPS, yc, z))
            cuts.append(_hex_x(self.nut3, px - 3.0, px + EPS, yc, z))
        for z in (0.15 * zt, 0.85 * zt):  # dowels
            cuts.append(_cyl_x(self.dowel_d / 2, -EPS, self.dowel_depth, yc, z))
        shape = _as_part(body - _union(cuts))
        return PartSpec("pillar", shape, material=self.module.spec.material, color=h.color,
                        quantity=2 * len(self.floor_joints), print_rotation=(0, 90, 0),
                        meta={"role": "back pillar (joint at local x = 0)"})

    @cached_property
    def top_parts(self) -> list[PartSpec]:
        """Top segments ``top_0…`` (printed outer face down): screws into the front/side ribs and
        pillars, lips for the back cover, bosses for the board and perfboard hanging underneath."""
        h, s = self.spec, self.module.spec
        segs = self._segments(self.floor_joints, -self.x_in, self.x_in)
        zt = self.z_top_in
        bosses = self._boss_points_top()
        out = []
        for k, (a, b) in enumerate(segs):
            plate = _box(a, b, self.y_panel, self.y_back_in, zt, self.z_max)
            adds, cuts = [], []

            def screw_down(x: float, y: float) -> None:
                cuts.append(_cyl_z(self.clear3 / 2, zt - EPS, self.z_max + EPS, x, y))
                cuts.append(_cyl_z(self.cb3 / 2, self.z_max - self.cb3_depth + 1.0, self.z_max + EPS, x, y))

            for x in self._front_rib_screws:
                if a + 4 < x < b - 4:
                    screw_down(x, self.y_panel + h.rib_depth / 2)
            for sx in (-1, 1):
                xc = sx * (self.x_in - h.rib_depth / 2)
                if a < xc < b:
                    for y in self._side_rib_y():
                        screw_down(xc, y)
            px, _py = h.pillar
            yp0, yp1 = self._pillar_y
            for j in self.floor_joints:
                for xc in (j - px / 2, j + px / 2):
                    if a < xc < b:
                        screw_down(xc, (yp0 + yp1) / 2)
            for x in self._lip_x["top"]:
                if a < x < b:
                    adds.append(_box(x - 6.0, x + 6.0, self.y_back_in - 8.0, self.y_back_in, zt - 10.0, zt + EPS))
                    cuts.append(_cyl_y(self.tap3 / 2, self.y_back_in - 8.0 - EPS, self.y_back_in + EPS, x, zt - 5.0))
            for x, y, size in bosses:
                if a + 4 < x < b - 4:
                    tap = get_size(size).tap_plastic + self.printer.hole_compensation
                    adds.append(_cyl_z(h.boss_d / 2 - 0.5, zt - h.boss_h, zt + EPS, x, y))
                    cuts.append(_cyl_z(tap / 2, zt - h.boss_h - EPS, zt + 1.5, x, y))
            body = _as_part(plate + _union(adds)) if adds else plate
            shape = _as_part(body - _union(cuts)) if cuts else body
            out.append(PartSpec(f"top_{k}", shape, material=s.material, color=h.color, print_rotation=(180, 0, 0),
                                meta={"role": "split-flap housing top", "x_range": [a, b]}))
        return out

    @cached_property
    def back_parts(self) -> list[PartSpec]:
        """Removable back cover segments ``back_0…`` (outer face down): counterbored screws, vents,
        the DC jack hole and wall-mount keyholes."""
        h, s = self.spec, self.module.spec
        segs = self._segments(self.floor_joints, -self.x_out, self.x_out)
        lay = self.electronics_layout
        y0, y1 = self.y_back_in, self.y_back
        screws = self._cover_screws()
        keys = []
        if h.keyholes:
            span = self.x_in * 0.55
            keys = [(-span, self.axis_z), (span, self.axis_z)]
        out = []
        for k, (a, b) in enumerate(segs):
            plate = _box(a, b, y0, y1, self.z_min, self.z_max)
            cuts = []
            for x, z in screws:
                if a + 2 < x < b - 2:
                    cuts.append(_cyl_y(self.clear3 / 2, y0 - EPS, y1 + EPS, x, z))
                    cuts.append(_cyl_y(self.cb3 / 2, y1 - 1.6, y1 + EPS, x, z))
            if h.vents:
                xa, xb = max(a, -self.x_in) + 14.0, min(b, self.x_in) - 14.0
                for zc in (16.0, self.z_top_in - 16.0):
                    for x in np.arange(xa, xb - 3.0, 6.0):
                        cuts.append(_box(float(x), float(x) + 3.0, y0 - EPS, y1 + EPS, zc - 7.0, zc + 7.0))
            if lay["dc_jack"]:
                jx, jz = lay["dc_jack"]
                if a + 8 < jx < b - 8:
                    cuts.append(_cyl_y((8.0 + 0.3 + self.printer.hole_compensation) / 2, y0 - EPS, y1 + EPS, jx, jz))
            for kx, kz in keys:  # keyhole: Ø8.5 head hole, 4.2 mm slot 10 mm up
                if a + 10 < kx < b - 10:
                    cuts.append(_cyl_y(4.25, y0 - EPS, y1 + EPS, kx, kz))
                    cuts.append(_box(kx - 2.1, kx + 2.1, y0 - EPS, y1 + EPS, kz, kz + 10.0))
                    cuts.append(_cyl_y(2.1, y0 - EPS, y1 + EPS, kx, kz + 10.0))
            shape = _as_part(plate - _union(cuts)) if cuts else plate
            out.append(PartSpec(f"back_{k}", shape, material=s.material, color=h.color, print_rotation=(-90, 0, 0),
                                meta={"role": "split-flap housing back cover (removable)", "x_range": [a, b]}))
        return out

    @property
    def parts(self) -> list[PartSpec]:
        """Every printed housing part (segments individually, splice/pillar with quantities)."""
        out = [*self.front_parts, *self.side_parts, *self.floor_parts, *self.top_parts, *self.back_parts]
        out += [p for p in (self.splice, self.pillar, self.comma_inlay) if p is not None]
        return out

    # -- display faces (GUI) ----------------------------------------------------------------------------
    @cached_property
    def face_part(self) -> PartSpec:
        """Thin black reference plate filling one window (local origin = window centre, y from 0 to
        +0.5): the GUI paints the live split-flap digits on it."""
        w, hgt = self.window_w - 0.1, self.window_h - 0.1
        shape = _rrect_xz(0.0, 0.0, w, hgt, 0.0, 0.5, max(self.spec.window_radius - 0.05, 0.0))
        return PartSpec("display_face", shape, kind="reference", material="none", color=self.spec.face_color,
                        meta={"role": "split-flap display face (GUI)", "size_mm": [w, hgt]})

    def face_location(self, i: int) -> tuple[float, float, float]:
        """Where :attr:`face_part` goes for window ``i``: just behind the window plane."""
        return (self.module_x(i), self.y_front + 0.3, self.axis_z + self.window_cz)

    # -- assembly -----------------------------------------------------------------------------------------
    def assembly(self, name: str = "splitflap_housing", *, asm: Assembly | None = None,
                 devices: list[str] | None = None, modules: bool = True, flaps: bool = True,
                 electronics: bool = True, faces: bool = False, housing: bool = True) -> Assembly:
        """Housing + modules (``m{i}_…``, spools driven by ``devices[i]``.angle) + electronics.

        Housing node ids: ``front_k``, ``side_left|right``, ``floor_k``, ``top_k``, ``back_k``,
        ``splice_k``, ``pillar_k``, ``comma_inlay``; electronics ``m{i}_driver``, ``board``,
        ``perfboard``, ``sr_{k}``, ``dc_jack``; ``faces=True`` adds ``face_{i}`` (with
        ``display_from`` when ``devices`` is given).
        """
        a = asm if asm is not None else Assembly(name)
        if housing:
            for p in self.front_parts:
                a.add(p, id=p.name, explode=(0, -90, 0))
            for p, ex in zip(self.side_parts, ((-60, 0, 0), (60, 0, 0))):
                a.add(p, id=p.name, explode=ex)
            for p in self.floor_parts:
                a.add(p, id=p.name, explode=(0, 0, -50))
            for p in self.top_parts:
                a.add(p, id=p.name, explode=(0, 0, 70))
            for p in self.back_parts:
                a.add(p, id=p.name, explode=(0, 110, 0))
            if self.splice is not None:
                for k, j in enumerate(self.front_joints):
                    a.add(self.splice, (j, 0, 0), id=f"splice_{k}", explode=(0, -60, 0))
            if self.pillar is not None:
                k = 0
                for j in self.floor_joints:
                    a.add(self.pillar, (j, 0, 0), id=f"pillar_{k}", explode=(0, 50, 0))
                    # mirrored: rotate 180° about Z through the pillar's y centre
                    yc = sum(self._pillar_y)
                    a.add(self.pillar, (j, yc, 0, 0, 0, 180), id=f"pillar_{k + 1}", explode=(0, 50, 0))
                    k += 2
            if self.comma_inlay is not None:
                a.add(self.comma_inlay, id="comma_inlay", explode=(0, -100, 0))
        if modules:
            for i in range(self.n):
                dev = devices[i] if devices and i < len(devices) else None
                self.module.assembly(asm=a, prefix=f"m{i}_", origin=(self.module_x(i), 0, 0), device=dev, flaps=flaps)
        if electronics:
            self._add_electronics(a)
        if faces:
            for i in range(self.n):
                disp = {"device": devices[i], "kind": "splitflap"} if devices and i < len(devices) else None
                a.add(self.face_part, self.face_location(i), id=f"face_{i}", display_from=disp)
        return a

    def _add_electronics(self, a: Assembly) -> None:
        from piforge.mech.boards import get_board
        from piforge.mech.modules import get_module

        e, lay, h = self.electronics, self.electronics_layout, self.spec
        if not e:
            return
        if lay["drivers"]:
            part = get_module(e.driver).part(f"{e.driver} driver board")
            for i, (x, y) in enumerate(lay["drivers"]):
                a.add(part, (x, y, h.boss_h), id=f"m{i}_driver", explode=(0, 30, 30))
        zt = self.z_top_in
        if lay["perfboard"]:
            x, y, _L, _W = lay["perfboard"]
            pb = get_module(e.perfboard).part("perfboard (74HCT595 chain)")
            z = zt - h.boss_h
            a.add(pb, (x, y, z, 180, 0, 0), id="perfboard", explode=(0, 40, -30))
            l, w, ht = 19.3, 6.35, 4.0  # src: JEDEC MS-001 DIP-16 body
            dip = PartSpec("74HCT595 (DIP-16)", _box(-l / 2, l / 2, -w / 2, w / 2, -ht, 0.0), kind="reference",
                           material="module", color="#1b1b1f")
            for k in range(e.shift_registers):
                a.add(dip, (x - 22.0 + 14.7 * k, y - 6.0, z - 1.6, 0, 0, 90), id=f"sr_{k}", explode=(0, 40, -30))
        if lay["board"]:
            x, y, L, W = lay["board"]
            b = get_board(e.board)
            # board frame: corner origin, PCB z ∈ [0, t]; hung under the top (180° about X)
            a.add(b.part(), (x - L / 2, y + W / 2, zt - h.boss_h, 180, 0, 0), id="board", explode=(0, 40, -30))
        if lay["dc_jack"]:
            jx, jz = lay["dc_jack"]
            jack = get_module(e.dc_jack).part("DC jack 5.5 × 2.1")
            a.add(jack, (jx, self.y_back, jz, -90, 0, 0), id="dc_jack", explode=(0, 110, 0))

    # -- checks -------------------------------------------------------------------------------------------
    def window_visibility(self, asm: Assembly, *, step: float = 1.0, inset: float = 0.3,
                          direction: tuple[float, float] = (0.0, 0.0),
                          exclude: tuple[str, ...] = ()) -> dict[int, Counter]:
        """Cast rays into every window and count which node each one hits first.

        Rays start in front of the panel on a ``step`` grid inside the window (``inset`` from its
        edges) and travel along +Y tilted by ``direction`` = (horizontal°, vertical°). Returns
        ``{window: Counter({node_id: rays})}`` (``None`` = ray hit nothing)."""
        import trimesh

        from piforge.mech.export import to_trimesh

        dx = math.tan(math.radians(direction[0]))
        dz = math.tan(math.radians(direction[1]))
        d = np.array([dx, 1.0, dz])
        d /= np.linalg.norm(d)
        x_lo = min(self.window_rect(i)[0] for i in range(self.n)) - 60
        x_hi = max(self.window_rect(i)[1] for i in range(self.n)) + 60
        meshes, names = [], []
        for node in asm.nodes:
            if (exclude and node.id.startswith(exclude)) or node.part.kind == "wire":
                continue  # harness wires run behind the modules; they never show in a window
            shp = asm.world_shape(node.id)
            bb = shp.bounding_box()
            if bb.max.Y < self.y_front - 1 or bb.min.Y > 40.0 or bb.max.X < x_lo or bb.min.X > x_hi:
                continue
            mesh = to_trimesh(shp)
            if len(mesh.faces):
                meshes.append(mesh)
                names.append(node.id)
        allm = trimesh.util.concatenate(meshes)
        face_owner = np.concatenate([np.full(len(mm.faces), k) for k, mm in enumerate(meshes)])
        try:
            from trimesh.ray.ray_triangle import RayMeshIntersector
            inter = RayMeshIntersector(allm)
        except Exception:  # noqa: BLE001
            inter = allm.ray
        out: dict[int, Counter] = {}
        y_start = self.y_front - 20.0
        for i in range(self.n):
            x0, x1, z0, z1 = self.window_rect(i)
            xs = np.arange(x0 + inset, x1 - inset + 1e-9, step)
            zs = np.arange(z0 + inset, z1 - inset + 1e-9, step)
            gx, gz = np.meshgrid(xs, zs)
            # start points on the window plane, shifted back along the ray to y_start
            t = (self.y_front - y_start) / d[1]
            org = np.column_stack([gx.ravel() - d[0] * t, np.full(gx.size, y_start), gz.ravel() - d[2] * t])
            dirs = np.tile(d, (len(org), 1))
            tri = inter.intersects_first(org, dirs)
            cnt: Counter = Counter()
            for k, (f, z) in enumerate(zip(tri, gz.ravel())):
                if f < 0:
                    cnt[None] += 1
                    continue
                nid = names[face_owner[f]]
                if any(nid.endswith(sfx) for sfx in _ALLOWED_SEAM) and abs(z - self.axis_z) <= self.module.spec.seam + 0.6:
                    nid = "<seam>"
                cnt[nid] += 1
            out[i] = cnt
        return out

    def visibility_check(self, asm: Assembly, *, step: float = 1.0) -> Report:
        """``HOUSING.WINDOW_CLEAN`` (INFO) when the straight front view of every window shows only
        flaps (and the hub through the 0.4 mm split) — the GUI display faces ``face_*`` are ignored;
        ``HOUSING.WINDOW_LEAK`` (ERROR) naming what else is visible."""
        rep = Report(title="housing windows")
        vis = self.window_visibility(asm, step=step, exclude=("face_",))
        bad = {}
        for i, cnt in vis.items():
            other = {k: v for k, v in cnt.items() if not (k == "<seam>" or (k and "flap_" in k))}
            if other:
                bad[i] = other
        if bad:
            for i, other in bad.items():
                rep.add("HOUSING.WINDOW_LEAK", Severity.ERROR,
                        f"Window {i} shows more than the flap face: {dict(other)} (rays).", f"window:{i}",
                        hint="Enlarge window_border or the shroud.", window=i, hits={str(k): v for k, v in other.items()})
        else:
            total = sum(sum(c.values()) for c in vis.values())
            rep.add("HOUSING.WINDOW_CLEAN", Severity.INFO,
                    f"Front view: {total} rays through {self.n} windows hit only the flap faces "
                    f"(window {self.window_w:.1f} × {self.window_h:.1f} mm, border {self.spec.window_border:g} mm).",
                    "housing", rays=total)
        return rep

    def swing_check(self) -> Report:
        """Sweep a released flap through its fall against one window cell of the front panel (with
        its shroud) and the module frames: ``HOUSING.SWING_CLEAR`` or ``HOUSING.SWING_COLLISION``."""
        m = self.module
        tmp = Assembly("housing_swing")
        cx = self.module_x(0)
        tmp.add(m.frame_left, (cx, 0, 0), id="frame_left")
        tmp.add(m.frame_right, (cx, 0, 0), id="frame_right")
        cell = self._front_body(cx - m.pitch / 2, cx + m.pitch / 2, left_end=True, right_end=True, ribs=False)
        tmp.add(PartSpec("cell", cell, material=m.spec.material), id="front")
        adv = m.release_advance or m.pitch_angle / 2
        lean = m.release_lean(max(adv - 0.1, 0.0))
        py, pz = m.pin_position(0, adv)
        tmp.add(m.flap, (cx, py, self.axis_z + pz, lean + 0.5, 0, 0), id="flap",
                joint=Joint("revolute", axis=(1, 0, 0), min=0.0, max=180.0 - lean - 0.5))
        sweep = tmp.sweep_joint("flap", steps=25)
        rep = Report(title="housing swing")
        hits = sweep.by_code("ASM.JOINT_COLLISION")
        for f in hits:
            rep.add("HOUSING.SWING_COLLISION", Severity.ERROR,
                    f"The falling flap hits {f.data.get('other')} at {f.data.get('angles')}°.", "housing",
                    hint="Increase swing_clear.", **f.data)
        if not hits:
            rep.add("HOUSING.SWING_CLEAR", Severity.INFO,
                    f"The falling flap clears the front panel by {self.swing_y - self.y_panel:.1f} mm and the "
                    "shroud over its whole fall.", "housing", clearance_mm=self.swing_y - self.y_panel)
        rep.extend(f for f in sweep if f.code == "ASM.CHECK_FAILED")
        return rep

    def bed_check(self, parts: list[PartSpec] | None = None) -> Report:
        """``HOUSING.BED_FIT`` (INFO) / ``HOUSING.TOO_BIG`` (ERROR): every printed part, in its print
        orientation, fits the printer's build volume."""
        from piforge.mech.export import to_trimesh

        rep = Report(title="housing bed fit")
        big = []
        worst = (0.0, 0.0, 0.0)
        for p in parts or self.parts:
            mesh = to_trimesh(p.shape)
            rx, ry, rz = p.print_rotation or (0, 0, 0)
            from scipy.spatial.transform import Rotation

            R = Rotation.from_euler("xyz", [rx, ry, rz], degrees=True).as_matrix()
            v = mesh.vertices @ R.T
            ext = tuple(float(e) for e in v.max(axis=0) - v.min(axis=0))
            if ext[0] > self.bed[0] + 1e-6 or ext[1] > self.bed[1] + 1e-6 or ext[2] > self.bed[2] + 1e-6:
                big.append((p.name, ext))
            if ext[0] * ext[1] > worst[0] * worst[1]:
                worst = ext
        for name, ext in big:
            rep.add("HOUSING.TOO_BIG", Severity.ERROR,
                    f"{name} is {ext[0]:.0f} × {ext[1]:.0f} × {ext[2]:.0f} mm in its print orientation "
                    f"(bed {self.bed[0]:g} × {self.bed[1]:g} × {self.bed[2]:g}).", f"part:{name}")
        if not big:
            rep.add("HOUSING.BED_FIT", Severity.INFO,
                    f"All housing parts fit the {self.bed[0]:g} × {self.bed[1]:g} × {self.bed[2]:g} mm build volume "
                    f"(largest footprint {worst[0]:.0f} × {worst[1]:.0f} mm).", "housing")
        return rep

    def summary(self) -> Report:
        """``HOUSING.SUMMARY`` (INFO): size, windows, segments."""
        rep = Report(title="housing")
        w, d, hgt = self.outer_size
        rep.add("HOUSING.SUMMARY", Severity.INFO,
                f"Closed housing {w:.0f} × {d:.0f} × {hgt:.0f} mm for {self.n} digits (module pitch "
                f"{self.module.pitch:g} mm): windows {self.window_w:.1f} × {self.window_h:.1f} mm, "
                f"{len(self.front_joints) + 1} front / {len(self.floor_joints) + 1} floor, top and back segments.",
                "housing", size_mm=[w, d, hgt], window_mm=[self.window_w, self.window_h],
                front_joints=self.front_joints, floor_joints=self.floor_joints)
        return rep

    def checks(self, asm: Assembly | None = None, *, interference: bool = True, swing: bool = True,
               visibility: bool = True) -> Report:
        """Summary, bed fit, flap swing vs the front panel, window visibility and interference of
        the housing with the modules and electronics."""
        rep = Report(title="split-flap housing")
        rep.extend(self.summary())
        rep.extend(self.bed_check())
        if swing:
            rep.extend(self.swing_check())
        if asm is None and (visibility or interference):
            asm = self.assembly()
        if visibility:
            rep.extend(self.visibility_check(asm))
        if interference:
            ignore = [(f"m{i}_motor", f"m{i}_spool") for i in range(self.n)]
            rep.extend(asm.check_interference(ignore=ignore, min_volume=0.5))
        return rep

    def print_checks(self, parts: list[PartSpec] | None = None) -> Report:
        """:func:`piforge.fab.analyze_mesh` of every printed housing part in its print orientation."""
        from piforge.fab.analyze import analyze_mesh
        from piforge.mech.export import to_trimesh

        rep = Report(title="housing printability")
        for part in parts or self.parts:
            res = analyze_mesh(to_trimesh(part.shape), self.printer, part.material, name=part.name,
                               rotation=part.print_rotation)
            rep.extend(res.report)
        return rep
