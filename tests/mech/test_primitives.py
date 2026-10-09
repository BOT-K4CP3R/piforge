"""Tests for piforge.mech.primitives — every test needs the OCC kernel (slow)."""

from __future__ import annotations

import math

import pytest

from piforge.core.errors import ValidationError

pytestmark = pytest.mark.slow


def _bounds(shape):
    bb = shape.bounding_box()
    return (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)


def _extents(shape):
    lo, hi = _bounds(shape)
    return tuple(h - l for l, h in zip(lo, hi))


def test_rounded_box_dims():
    from piforge.mech.primitives import rounded_box

    p = rounded_box(40, 30, 12, radius=4)
    lo, hi = _bounds(p)
    assert _extents(p) == pytest.approx((40, 30, 12), abs=0.01)
    assert lo == pytest.approx((-20, -15, 0), abs=0.01)  # centred in XY, bottom on z = 0
    assert p.is_valid
    assert p.volume < 40 * 30 * 12
    # analytic: a rounded rectangle loses (4 - pi) r^2 of area
    assert p.volume == pytest.approx((40 * 30 - (4 - math.pi) * 16) * 12, rel=1e-6)

    sharp = rounded_box(10, 20, 5)
    assert sharp.volume == pytest.approx(1000, rel=1e-9)

    fancy = rounded_box(40, 30, 12, radius=4, top_radius=2, bottom_chamfer=0.5)
    assert fancy.is_valid
    assert _extents(fancy) == pytest.approx((40, 30, 12), abs=0.01)
    # Pappus: an edge treatment of section A with centroid c inside the wall, run along the
    # straight edges (L) and around the four R-corners (one full turn at radius R - c)
    straight = 2 * (40 - 8) + 2 * (30 - 8)
    fillet = (1 - math.pi / 4) * 2**2 * (straight + 2 * math.pi * (4 - 0.22338 * 2))
    cham = 0.5**2 / 2 * (straight + 2 * math.pi * (4 - 0.5 / 3))
    assert fancy.volume == pytest.approx(p.volume - fillet - cham, rel=1e-4)

    from piforge.mech.export import to_trimesh

    full_round = rounded_box(40, 30, 12, radius=4, top_radius=4)  # clamped to 3.99: no degenerate poles
    assert full_round.is_valid and full_round.volume > 0.9 * p.volume
    assert to_trimesh(full_round).is_watertight


def test_rounded_box_rejects_bad_sizes():
    from piforge.mech.primitives import rounded_box

    with pytest.raises(ValidationError):
        rounded_box(10, 10, 5, radius=5)  # radius must be < half the shorter side
    with pytest.raises(ValidationError):
        rounded_box(10, -1, 5)
    with pytest.raises(ValidationError):
        rounded_box(10, 10, 2, top_radius=1.5, bottom_chamfer=1.0)  # leaves no straight wall


def test_hollow_box_volume():
    from piforge.mech.export import to_trimesh
    from piforge.mech.primitives import hollow_box

    p = hollow_box(50, 40, 20, wall=2, floor=3)
    assert p.is_valid
    assert _extents(p) == pytest.approx((50, 40, 20), abs=0.01)
    assert p.volume == pytest.approx(50 * 40 * 20 - 46 * 36 * 17, rel=1e-6)

    closed = hollow_box(50, 40, 20, wall=2, floor=3, open_top=False)
    assert closed.volume == pytest.approx(50 * 40 * 20 - 46 * 36 * (20 - 3 - 3), rel=1e-6)

    rounded = hollow_box(50, 40, 20, wall=2, floor=3, radius=5)
    outer = 50 * 40 - (4 - math.pi) * 5**2
    inner = 46 * 36 - (4 - math.pi) * 3**2  # inner radius = radius - wall
    assert rounded.is_valid
    assert rounded.volume == pytest.approx(outer * 20 - inner * 17, rel=1e-6)
    assert to_trimesh(p).is_watertight
    assert to_trimesh(rounded).is_watertight

    with pytest.raises(ValidationError):
        hollow_box(10, 10, 10, wall=5, floor=1)


def test_flush_cut_stays_watertight():
    """Review focus #2: a cutter placed flush with a face must not leave coplanar slivers."""
    from piforge.fab.profiles import get_printer
    from piforge.mech.export import to_trimesh
    from piforge.mech.fasteners import clearance_hole
    from piforge.mech.primitives import EPS, rounded_box

    assert EPS == pytest.approx(0.01)
    box = rounded_box(20, 20, 10)
    hole = clearance_hole("M3", 10)  # nominal z ∈ [0, 10] = flush with bottom and top faces
    lo, hi = _bounds(hole)
    assert lo[2] == pytest.approx(-EPS, abs=1e-6) and hi[2] == pytest.approx(10 + EPS, abs=1e-6)

    cut = box - hole
    assert cut.is_valid
    mesh = to_trimesh(cut)
    assert mesh.is_watertight
    r = (3.4 + get_printer("generic").hole_compensation) / 2
    assert mesh.volume == pytest.approx(4000 - math.pi * r * r * 10, rel=0.01)


def test_slot_is_a_stadium_prism():
    from piforge.mech.primitives import slot

    s = slot(20, 6, 3)
    assert s.is_valid
    assert _extents(s) == pytest.approx((20, 6, 3), abs=0.01)
    lo, _ = _bounds(s)
    assert lo == pytest.approx((-10, -3, 0), abs=0.01)
    assert s.volume == pytest.approx(((20 - 6) * 6 + math.pi * 9) * 3, rel=1e-6)
    with pytest.raises(ValidationError):
        slot(4, 6, 3)


def test_vent_slots_fill_area_and_count():
    from piforge.mech.primitives import vent_slots

    v = vent_slots(30, 20, slot_w=2.0, pitch=4.0, depth=10.0)
    lo, hi = _bounds(v)
    assert lo[2] == pytest.approx(-5, abs=1e-6) and hi[2] == pytest.approx(5, abs=1e-6)
    assert hi[0] <= 15 + 1e-6 and lo[0] >= -15 - 1e-6
    assert hi[1] <= 10 + 1e-6 and lo[1] >= -10 - 1e-6
    assert len(v.solids()) == 5  # floor((20 - 2) / 4) + 1
    assert (hi[0] + lo[0]) == pytest.approx(0, abs=1e-6)
    assert (hi[1] + lo[1]) == pytest.approx(0, abs=1e-6)

    across = vent_slots(30, 20, slot_axis="y", r_ends=False)
    assert len(across.solids()) == 8  # slots run along Y, repeated along X: floor(28 / 4) + 1
    assert across.volume == pytest.approx(8 * 2.0 * 20 * 10, rel=1e-6)

    with pytest.raises(ValidationError):
        vent_slots(30, 1.0, slot_w=2.0)
    with pytest.raises(ValidationError):
        vent_slots(30, 20, slot_axis="z")


def test_hex_vents_stay_inside_area():
    from piforge.mech.primitives import hex_vents

    h = hex_vents(40, 30, cell=5.0, web=1.2, depth=4.0)
    lo, hi = _bounds(h)
    assert lo[0] >= -20 - 1e-6 and hi[0] <= 20 + 1e-6
    assert lo[1] >= -15 - 1e-6 and hi[1] <= 15 + 1e-6
    assert lo[2] == pytest.approx(-2, abs=1e-6) and hi[2] == pytest.approx(2, abs=1e-6)
    n = len(h.solids())
    assert n >= 20
    cell_area = math.sqrt(3) / 2 * 5.0**2  # regular hexagon, across flats 5
    assert h.volume == pytest.approx(n * cell_area * 4.0, rel=1e-6)


def test_text_solid_centred():
    from piforge.mech.primitives import text_solid

    t = text_solid("PiForge 5", size=6.0, depth=0.6)
    assert t.is_valid and t.volume > 0
    lo, hi = _bounds(t)
    assert lo[2] == pytest.approx(0, abs=1e-6) and hi[2] == pytest.approx(0.6, abs=1e-6)
    assert (lo[0] + hi[0]) / 2 == pytest.approx(0, abs=0.05)
    assert (lo[1] + hi[1]) / 2 == pytest.approx(0, abs=0.05)
    assert 15 < hi[0] - lo[0] < 40  # 9 glyphs at 6 mm
    with pytest.raises(ValidationError):
        text_solid("   ")


def test_chamfer_bottom_removes_elephant_foot():
    from piforge.mech.primitives import chamfer_bottom, hollow_box, rounded_box

    box = rounded_box(20, 20, 10, radius=3)
    c = chamfer_bottom(box, 0.4)
    assert c.is_valid
    assert _extents(c) == pytest.approx((20, 20, 10), abs=0.01)
    removed = 0.4**2 / 2 * (4 * (20 - 6) + 2 * math.pi * (3 - 0.4 / 3))  # Pappus, see above
    assert c.volume == pytest.approx(box.volume - removed, rel=1e-5)
    # the bottom face shrank by ~0.4 mm on every side
    bottom = min(c.faces(), key=lambda f: f.center().Z)
    assert bottom.area < min(box.faces(), key=lambda f: f.center().Z).area

    plain = hollow_box(30, 20, 10, wall=2, floor=2)
    shell = chamfer_bottom(plain, 0.4)
    # sharp outer corners: four edge prisms minus the four mitred corner overlaps (c³/3 each)
    removed = 0.4**2 / 2 * 2 * (30 + 20) - 4 * 0.4**3 / 3
    assert shell.is_valid
    assert shell.volume == pytest.approx(plain.volume - removed, rel=1e-6)


@pytest.mark.parametrize("face", ["top", "bottom", "-y", "+x"])
def test_emboss_and_engrave_overlap_by_eps_and_stay_watertight(face):
    from piforge.mech.export import to_trimesh
    from piforge.mech.primitives import emboss, engrave, rounded_box

    base = rounded_box(40, 20, 10, 2)
    up = emboss(base, "Pi", face=face, size=6, height=0.8)
    down = engrave(base, "Pi", face=face, size=6, depth=0.6)
    for part in (up, down):
        assert part.is_valid
        assert len(part.solids()) == 1  # text fused into / cut out of the body, no loose solids
        assert to_trimesh(part).is_watertight
    assert up.volume > base.volume + 1.0
    assert down.volume < base.volume - 1.0
    lo, hi = _bounds(base)
    ulo, uhi = _bounds(up)
    axis = {"top": (2, 1), "bottom": (2, -1), "-y": (1, -1), "+x": (0, 1)}[face]
    i, sign = axis
    if sign > 0:
        assert uhi[i] == pytest.approx(hi[i] + 0.8, abs=1e-6)  # exact height above the face
    else:
        assert ulo[i] == pytest.approx(lo[i] - 0.8, abs=1e-6)
    assert [*_bounds(down)[0], *_bounds(down)[1]] == pytest.approx([*lo, *hi], abs=1e-6)  # never grows


def test_emboss_on_a_face_object_and_validation():
    from piforge.mech.primitives import emboss, engrave, rounded_box

    base = rounded_box(40, 20, 10)
    top = base.faces().sort_by()[-1]
    up = emboss(base, "A", face=top, size=5, height=1.0, at=(10, 0), rotation=90)
    assert _bounds(up)[1][2] == pytest.approx(11.0, abs=1e-6)
    assert up.is_valid
    with pytest.raises(ValidationError):
        emboss(base, "A", face="sideways")
    with pytest.raises(ValidationError):
        engrave(base, "A", depth=10.0)  # deeper than the part along that axis
    with pytest.raises(ValidationError):
        emboss(base, "A", face=base.faces().filter_by(lambda f: False) or base.edges()[0])
