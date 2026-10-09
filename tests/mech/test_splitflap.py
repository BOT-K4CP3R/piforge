"""Split-flap digit module (piforge.mech.splitflap / splitflap_art)."""

from __future__ import annotations

import itertools
import json
import math

import numpy as np
import pytest

from piforge.core.errors import ValidationError


@pytest.fixture(scope="module")
def mod():
    from piforge.mech.splitflap import SplitFlapModule

    return SplitFlapModule()


# -- kernel-free kinematics / dimensions ------------------------------------------------------------
def test_lazy_exports():
    import piforge.mech as mech

    assert mech.SplitFlapModule.__name__ == "SplitFlapModule"
    assert callable(mech.digit_artwork) and callable(mech.flap_with_inlay)


def test_spec_validation():
    from piforge.mech.splitflap import SplitFlapModule, SplitFlapSpec

    with pytest.raises(ValidationError):
        SplitFlapSpec(flaps=15)
    with pytest.raises(ValidationError):
        SplitFlapSpec(digit_height=-1)
    with pytest.raises(ValidationError):
        SplitFlapModule(pin_length=0.8)  # would not reach into the flanges
    with pytest.raises(ValidationError):
        SplitFlapModule(pitch_radius=5.0)  # flap could not close the seam


def test_dimensions(mod):
    s = mod.spec
    assert s.flaps == 20 and mod.pitch_angle == pytest.approx(18.0)
    # flap carries half a digit + margin; plate closes the seam at the split
    assert mod.flap_length == pytest.approx(s.digit_height / 2 + s.flap_margin)
    assert mod.pin_edge == pytest.approx(s.pitch_radius * math.sin(math.radians(9)) - s.seam / 2)
    # 20 pins fit the circle with a printable web; flaps stack
    assert mod.pin_pitch == pytest.approx(2 * math.pi * s.pitch_radius / 20, rel=0.01)
    assert mod.pin_pitch - mod.pin_hole_d >= 2 * mod.printer.line_w
    assert mod.pin_hole_d > math.hypot(s.flap_thickness, s.pin_width)  # pin turns in its hole
    # pins reach into both flanges but do not poke out
    assert s.flap_gap < s.pin_length < s.flap_gap + s.cap_flange
    # window shows the whole digit and lets the flap swing through
    assert mod.window_w > s.flap_width and mod.window_h > 2 * mod.flap_length
    # motor shaft engages the hub
    assert mod.shaft_engagement > 5.0 and mod.bore_depth > mod.shaft_engagement
    # 28BYJ-48 tab holes 35 mm apart on the right frame
    (y1, z1), (y2, z2) = mod.motor_holes
    assert math.hypot(y1 - y2, z1 - z2) == pytest.approx(35.0)
    assert 60 < mod.pitch < 90


def test_flap_sequence(mod):
    assert [mod.flap_digit(k) for k in range(20)] == list(range(10)) * 2
    # flap 0 shown at joint 0: its pin 9° above the axis at the front; flap 19 just fell (9° below)
    y0, z0 = mod.pin_position(0)
    y19, z19 = mod.pin_position(19)
    assert y0 == pytest.approx(y19) and z0 == pytest.approx(-z19) and z0 > 0 and y0 < 0
    # one 18° step brings flap 1 into flap 0's place
    assert mod.pin_position(1, 18.0) == pytest.approx(mod.pin_position(0))


def test_rest_pose_no_overlap(mod):
    poses = mod.rest_poses
    assert sorted(poses) == list(range(20))
    polys = {k: mod._flap_poly((y, z), a) for k, (y, z, a) in poses.items()}
    for a, b in itertools.combinations(polys, 2):
        assert polys[a].distance(polys[b]) > 0.0, (a, b)
    assert poses[0][2] < 3.0  # shown flap nearly upright on the stop
    assert poses[19][2] == pytest.approx(180.0)  # fallen flap hangs straight down
    from shapely.geometry import Point

    hub = Point(0, 0).buffer(mod.hub_r)
    assert all(p.distance(hub) > 0 for p in polys.values())


def test_release_within_step(mod):
    adv = mod.release_advance
    assert adv is not None and 3.0 <= adv <= 15.0
    assert mod.release_lean(0.0) < 3.0


def test_hall_alignment(mod):
    assert mod.hall_distance() <= 3.0
    assert mod.hall_distance(mod.spec.home_angle + 18.0) > 5.0  # only one flap position triggers
    from piforge.mech.splitflap import SplitFlapModule

    m2 = SplitFlapModule(hall_angle=-30.0, home_angle=40.0)
    assert m2.hall_distance() <= 3.0


def test_segment_plan(mod):
    plan = mod.segment_plan(8, comma_after=5)
    assert sum(c for _, c, _ in plan) == 8
    assert [f for f, _, _ in plan] == [0, 2, 4, 6]
    assert plan[-1][2] == -1  # comma opens the segment of modules 6-7, never ends one
    bed = max(mod.printer.build_x, mod.printer.build_y)
    for first, count, cl in plan:
        width = count * mod.pitch + (mod.spec.comma_width if cl is not None else 0)
        assert width + 10 <= bed
    with pytest.raises(ValidationError):
        mod.segment_plan(3, comma_after=2)


# -- CAD -----------------------------------------------------------------------------------------
@pytest.mark.slow
def test_parts_watertight(mod):
    from piforge.mech.export import to_trimesh

    parts = mod.parts + mod.comma_module() + [mod.bezel_segment(2), mod.backbone_segment(2, -1)]
    for p in parts:
        assert p.shape.is_valid, p.name
        assert to_trimesh(p.shape).is_watertight, p.name
    sx, sy, sz = mod.flap.size
    assert sx == pytest.approx(mod.spec.flap_width + 2 * mod.spec.pin_length)
    assert sy == pytest.approx(mod.spec.flap_thickness)
    assert sz == pytest.approx(mod.flap_height)
    assert mod.flap.quantity == 20


@pytest.mark.slow
def test_assembly_checks_clean(mod):
    asm = mod.assembly(device="SF1")
    ids = {n.id for n in asm.nodes}
    assert {"frame_left", "frame_right", "motor", "hall", "spool", "spool_cap", "magnet"} <= ids
    assert {f"flap_{k:02d}" for k in range(20)} <= ids
    j = asm.node("spool").joint
    assert j.axis == (1.0, 0.0, 0.0) and j.driven_by["device"] == "SF1" and j.driven_by["prop"] == "angle"
    rep = mod.checks(printability=False)
    assert not rep.errors, [str(f) for f in rep.errors]
    assert rep.has("ASM.JOINT_OK") and rep.has("SPLITFLAP.SWING_CLEAR") and rep.has("SPLITFLAP.HALL_ALIGNED")


@pytest.mark.slow
def test_display_assembly(mod):
    a = mod.display_assembly(3, comma_after=1, devices=["A", "B", "C"])
    ids = {n.id for n in a.nodes}
    assert {"m0_spool", "m2_flap_19", "comma", "comma_glyph", "bezel_0", "bezel_1", "backbone_1"} <= ids
    assert a.node("m1_spool").joint.driven_by["device"] == "B"


@pytest.mark.slow
def test_printability_clean(mod):
    rep = mod.print_checks()
    bad = [str(f) for f in rep if f.severity >= 1]
    assert not bad, bad


@pytest.mark.slow
def test_digit_artwork(tmp_path, mod):
    from PIL import Image

    from piforge.mech.splitflap_art import digit_artwork

    idx = digit_artwork(tmp_path, mod, dpi=200)
    for k, rec in enumerate(idx["flaps"]):
        assert rec["front_digit"] == k % 10 and rec["back_digit"] == (k + 1) % 10
        for key in ("front_svg", "front_png", "back_svg", "back_png"):
            assert (tmp_path / rec["files"][key]).is_file()
    assert (tmp_path / "sheet_front.svg").is_file() and (tmp_path / "sheet_back.svg").is_file()
    assert json.loads((tmp_path / "index.json").read_text())["size_mm"][0] == mod.spec.flap_width
    # digit "1": its stem crosses the split, so the front's pin edge (bottom rows) and the back's
    # pin edge (top rows, flap 0 carries the bottom of 1) both carry ink; the opposite edges do not
    front = np.asarray(Image.open(tmp_path / "flap_01_front.png").convert("L")) > 128
    back = np.asarray(Image.open(tmp_path / "flap_00_back.png").convert("L")) > 128
    assert front[-3:].any() and not front[:5].any()
    assert back[:3].any() and not back[-5:].any()
    assert 'fill-rule="evenodd"' in (tmp_path / "flap_08_front.svg").read_text()


@pytest.mark.slow
def test_flap_with_inlay(mod):
    from piforge.mech.splitflap_art import flap_with_inlay

    body, ink = flap_with_inlay(3, mod)
    assert body.shape.is_valid and ink.shape.is_valid
    assert body.volume + ink.volume == pytest.approx(mod.flap.volume, rel=0.005)
    t2 = mod.spec.flap_thickness / 2
    (x0, y0, z0), (x1, y1, z1) = ink.bounds()
    assert y0 == pytest.approx(-t2, abs=1e-3) and y1 == pytest.approx(t2, abs=1e-3)  # both faces
    assert abs(x0) <= mod.spec.flap_width / 2 and z0 >= -mod.pin_edge and z1 <= mod.flap_length
    assert ink.meta["front_digit"] == 3 and ink.meta["back_digit"] == 4
    with pytest.raises(ValidationError):
        flap_with_inlay(3, mod, depth=0.5)
