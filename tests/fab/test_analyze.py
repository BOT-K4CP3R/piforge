import json
import math
import subprocess
import sys

import numpy as np
import pytest
import trimesh

from piforge.core import Severity, ValidationError
from piforge.fab import analyze as analyze_mod
from piforge.fab import get_printer
from piforge.fab.analyze import (
    PrintAnalysis,
    analyze_mesh,
    bridge_face_mask,
    overhang_mask,
    wall_thickness_samples,
)
from piforge.fab.estimate import estimate_print
from piforge.fab.meshutil import place_on_bed, rotation_matrix, to_homogeneous
from piforge.fab.orient import best_orientation


def _box(lo, hi) -> trimesh.Trimesh:
    return trimesh.creation.box(bounds=(lo, hi))


def _soup(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """The same triangles sharing no vertices (STL read with process=False, GLB split at edges)."""
    return trimesh.Trimesh(mesh.vertices[mesh.faces].reshape(-1, 3),
                           np.arange(3 * len(mesh.faces)).reshape(-1, 3), process=False)


def _cut(base: trimesh.Trimesh, *cutters: trimesh.Trimesh) -> trimesh.Trimesh:
    return trimesh.boolean.difference([base, *cutters], engine="manifold")


def _join(*parts: trimesh.Trimesh) -> trimesh.Trimesh:
    return trimesh.boolean.union(list(parts), engine="manifold")


def _port_wall() -> trimesh.Trimesh:
    """A 2 mm wall standing on the bed with a 9 × 4 mm port cut through it (roof at z = 10)."""
    return _cut(_box((0, 0, 0), (40, 2, 20)), _box((15.5, -1, 6), (24.5, 3, 10)))


def test_cube_is_clean(cube20):
    a = analyze_mesh(cube20)
    assert isinstance(a, PrintAnalysis)
    assert a.watertight and a.winding_consistent and a.body_count == 1
    assert a.overhang_area_mm2 == 0.0 and not a.overhang_face_mask.any()
    assert a.bed_contact_area_mm2 == pytest.approx(400.0, abs=1.0)
    assert a.fits_bed and not a.needs_supports
    assert a.size_mm == pytest.approx((20.0, 20.0, 20.0))
    assert a.volume_mm3 == pytest.approx(8000.0) and a.area_mm2 == pytest.approx(2400.0)
    assert a.min_wall_mm == pytest.approx(20.0, abs=1e-3)
    assert not a.report.errors and not a.report.warnings
    assert a.report.has("PRINT.ESTIMATE", Severity.INFO)
    est = a.report.by_code("PRINT.ESTIMATE")[0]
    assert est.data["mass_g"] == pytest.approx(a.estimate.mass_g)


def test_mushroom_overhang(mushroom):
    a = analyze_mesh(mushroom)
    # the cap's underside around the stem: π(15² − 5²) = 628.3 mm²
    assert a.overhang_area_mm2 == pytest.approx(math.pi * (15**2 - 5**2), rel=0.03)
    assert a.needs_supports
    f = a.report.by_code("PRINT.OVERHANG")
    assert len(f) == 1 and f[0].severity == Severity.WARNING
    assert f[0].data["area_mm2"] == pytest.approx(a.overhang_area_mm2)
    assert f[0].data["limit_deg"] == pytest.approx(46.0)  # max_overhang 45° + 1° tolerance
    # held up by the stem from the inside only: a cantilever all round, not a bridge
    assert not a.report.has("PRINT.BRIDGE") and not a.bridge_face_mask.any()
    # the stem's foot is bed contact, never overhang
    assert a.bed_contact_area_mm2 == pytest.approx(math.pi * 5**2, rel=0.03)
    assert not a.report.has("PRINT.SMALL_CONTACT")


def test_bridge_free_cube_rotated_45(cube20):
    """Ruling: faces exactly at max_overhang (45°) are printable — only > 45° + 1° is overhang.

    Rotated 45° about X, the cube's two lower faces sit exactly on the threshold, so there is no
    overhang; the cube then touches the bed only along an edge → PRINT.SMALL_CONTACT warning.
    """
    a = analyze_mesh(cube20, rotation=rotation_matrix(45, 0, 0))
    assert a.overhang_area_mm2 == 0.0 and not a.overhang_face_mask.any()
    assert a.bed_contact_area_mm2 == pytest.approx(0.0, abs=1e-9)
    assert a.size_mm[2] == pytest.approx(20 * math.sqrt(2))
    assert a.report.has("PRINT.SMALL_CONTACT", Severity.WARNING)
    assert not a.report.has("PRINT.OVERHANG")


@pytest.mark.parametrize(
    ("tilt_deg", "max_overhang_deg", "expected_mm2"),
    [(0, 45, 0.0), (45, 45, 0.0), (47, 45, 400.0), (50, 45, 400.0), (50, 55, 0.0), (60, 55, 400.0)],
)
def test_overhang_threshold_is_measured_from_vertical(cube20, tilt_deg, max_overhang_deg, expected_mm2):
    # tilting the cube by θ about X turns its −Y face into a ceiling θ° from vertical
    tilted = place_on_bed(cube20, rotation_matrix(tilt_deg, 0, 0))
    mask = overhang_mask(tilted, max_overhang_deg)
    assert mask.shape == (len(tilted.faces),) and mask.dtype == bool
    assert tilted.area_faces[mask].sum() == pytest.approx(expected_mm2)


@pytest.mark.parametrize(("lift", "expected_mm2"), [(0.04, 0.0), (1.0, 100.0)])
def test_faces_near_the_bed_are_contact_not_overhang(lift, expected_mm2):
    on_bed = trimesh.creation.box(bounds=((0, 0, 0), (10, 10, 10)))
    floating = trimesh.creation.box(bounds=((20, 0, lift), (30, 10, 10 + lift)))
    mesh = trimesh.util.concatenate([on_bed, floating])
    mask = overhang_mask(mesh)
    assert mesh.area_faces[mask].sum() == pytest.approx(expected_mm2)


def test_overhang_mask_uses_lowest_point_as_bed(cube20):
    raised = cube20.copy()
    raised.apply_translation((0, 0, 50))  # not on z=0: the bed is wherever the part's bottom is
    assert not overhang_mask(raised).any()


def test_port_cutout_roof_is_a_bridge():
    wall = _port_wall()
    a = analyze_mesh(wall)
    assert a.overhang_area_mm2 == 0.0 and not a.needs_supports
    assert not a.report.has("PRINT.OVERHANG")
    f = a.report.by_code("PRINT.BRIDGE")
    assert len(f) == 1 and f[0].severity == Severity.INFO
    assert f[0].data["count"] == 1
    assert f[0].data["max_span_mm"] == pytest.approx(9.0, abs=0.2)
    assert f[0].data["area_mm2"] == pytest.approx(9 * 2)
    assert a.bridge_face_mask.shape == (len(wall.faces),)
    assert wall.area_faces[a.bridge_face_mask].sum() == pytest.approx(18.0)
    assert not (a.bridge_face_mask & a.overhang_face_mask).any()


def test_round_hole_roof_is_a_bridge():
    hole = trimesh.creation.cylinder(radius=1.5, height=30, sections=32)
    hole.apply_transform(to_homogeneous(rotation_matrix(90, 0, 0)))  # axis along Y
    hole.apply_translation((15, 10, 7.5))
    a = analyze_mesh(_cut(_box((0, 0, 0), (30, 20, 15)), hole))  # Ø3 mm through a 20 mm wall
    assert not a.report.has("PRINT.OVERHANG") and not a.needs_supports
    f = a.report.by_code("PRINT.BRIDGE")
    assert len(f) == 1 and f[0].data["count"] == 1
    assert 0.5 < f[0].data["max_span_mm"] < 3.0  # the drooping arc at the top of the hole


@pytest.mark.parametrize(("max_bridge_mm", "bridged"), [(10.0, False), (30.0, True)])
def test_slot_roof_bridges_only_up_to_max_bridge(max_bridge_mm, bridged):
    slot = _cut(_box((0, 0, 0), (60, 2, 20)), _box((17.5, -1, 6), (42.5, 3, 10)))  # 25 mm wide
    a = analyze_mesh(slot, get_printer("generic").with_(max_bridge_mm=max_bridge_mm))
    assert a.report.has("PRINT.BRIDGE") is bridged
    if bridged:
        assert a.overhang_area_mm2 == 0.0
    else:
        assert a.report.has("PRINT.OVERHANG", Severity.WARNING) and a.needs_supports
        assert a.overhang_area_mm2 == pytest.approx(25 * 2)


@pytest.mark.parametrize(("awning", "area"), [
    # 10 mm out and wider than the port: its far edge is ≥ 10 mm from any support (span ≥ 20 mm)
    (((10, -10, 10), (30, 0.5, 12)), 9 * 2 + 20 * 10),
    # flush with the port, 6 mm out: the tip's middle is 7.5 mm from a support (span 15 mm)
    (((15.5, -6, 10), (24.5, 0.5, 12)), 9 * 2 + 9 * 6),
])
def test_roof_running_into_a_cantilever_is_not_a_bridge(awning, area):
    # the port roof (sides 9 mm apart, a fine bridge alone) continues flush into an awning outside
    # the wall: the region is held at both sides but reaches too far from them — no bridge
    wall = _join(_port_wall(), _box(*awning))
    a = analyze_mesh(wall, get_printer("generic").with_(max_bridge_mm=10.0))  # pin the span limit
    assert not a.report.has("PRINT.BRIDGE")
    assert a.overhang_area_mm2 == pytest.approx(area)
    assert a.report.has("PRINT.OVERHANG", Severity.WARNING)


def test_cantilever_ledge_stays_an_overhang():
    part = _join(_box((0, 0, 0), (20, 20, 20)), _box((19, 5, 16), (25, 15, 20)))  # sticks out 5 mm
    a = analyze_mesh(part)
    assert a.report.has("PRINT.OVERHANG", Severity.WARNING) and a.needs_supports
    assert a.overhang_area_mm2 == pytest.approx(5 * 10)
    assert not a.report.has("PRINT.BRIDGE") and not a.bridge_face_mask.any()


def test_cap_around_a_post_is_not_a_bridge():
    # an 8 mm cap on a 5 mm post: the 3 mm ring is held from the inside only — a cantilever all
    # round, even though its "span" (6 mm) would fit a 10 mm bridge
    post = trimesh.creation.cylinder(radius=5.0, height=10.5)
    post.apply_translation((0, 0, 5.25))
    cap = trimesh.creation.cylinder(radius=8.0, height=3.0)
    cap.apply_translation((0, 0, 11.5))
    a = analyze_mesh(_join(post, cap))
    assert a.report.has("PRINT.OVERHANG", Severity.WARNING)
    assert a.overhang_area_mm2 == pytest.approx(math.pi * (8**2 - 5**2), rel=0.03)
    assert not a.report.has("PRINT.BRIDGE")


def test_ring_resting_on_the_bed_is_not_a_bridge():
    # a torus lying flat: inside its contact ring the underside curls up and inward — the "facing"
    # supports are 60 mm apart across the central hole, far beyond any bridge
    a = analyze_mesh(trimesh.creation.torus(major_radius=30, minor_radius=5, major_sections=128,
                                            minor_sections=48))
    assert not a.report.has("PRINT.BRIDGE") and not a.bridge_face_mask.any()
    assert a.report.has("PRINT.OVERHANG", Severity.WARNING)


def test_enclosure_with_port_cutouts_needs_no_supports():
    tray = _cut(_box((0, 0, 0), (90, 62, 26)), _box((2, 2, 2), (88, 60, 27)),
                _box((10, -1, 5), (19.5, 3, 9)),  # USB-C 9.5 × 4 mm
                _box((25, -1, 5), (32.5, 3, 9)),  # micro-HDMI 7.5 × 4 mm
                _box((38, -1, 5), (45.5, 3, 9)))  # micro-HDMI
    assert overhang_mask(tray).any()  # geometrically the three roofs do hang over air…
    a = analyze_mesh(tray, "prusa_mk4", "PETG")
    assert not a.needs_supports and not a.report.has("PRINT.OVERHANG")  # …but they bridge
    assert a.report.by_code("PRINT.BRIDGE")[0].data["count"] == 3


def test_overhang_mask_can_leave_out_bridges():
    wall = _port_wall()
    assert wall.area_faces[overhang_mask(wall)].sum() == pytest.approx(18.0)
    assert not overhang_mask(wall, max_bridge_mm=10.0).any()
    assert not overhang_mask(_soup(wall), max_bridge_mm=10.0).any()  # welded internally


def test_bridge_face_mask_matches_the_analysis_even_on_unwelded_meshes():
    wall = _port_wall()
    expected = analyze_mesh(wall).bridge_face_mask
    assert wall.area_faces[expected].sum() == pytest.approx(18.0)
    for mesh in (wall, _soup(wall)):
        mask = bridge_face_mask(mesh)  # max_bridge_mm defaults to the generic printer's limit (20 mm)
        assert mask.shape == (len(wall.faces),) and mask.dtype == bool
        assert np.array_equal(mask, expected)
    assert np.array_equal(bridge_face_mask(wall, max_bridge_mm=None), expected)
    assert not bridge_face_mask(wall, max_bridge_mm=5.0).any()  # the 9 mm span is too long
    assert not bridge_face_mask(wall, max_overhang_deg=89.0).any()  # nothing hangs → no bridge


def test_to_dict_counts_bridges():
    d = analyze_mesh(_port_wall()).to_dict()
    json.dumps(d)
    assert d["bridge_count"] == 1 and d["bridge_area_mm2"] == pytest.approx(18.0)
    assert "bridge_face_mask" not in d
    assert analyze_mesh(trimesh.creation.box(extents=(10, 10, 10))).to_dict()["bridge_count"] == 0


def test_small_overhang_is_info_only():
    part = _join(_box((0, 0, 0), (20, 20, 20)), _box((19, 8, 18), (25.5, 12, 20)))  # 5.5 × 4 mm lip
    a = analyze_mesh(part)
    f = a.report.by_code("PRINT.OVERHANG")
    assert len(f) == 1 and f[0].severity == Severity.INFO
    assert a.overhang_area_mm2 == pytest.approx(22.0) and not a.needs_supports


@pytest.mark.parametrize(("height", "warned"), [(100.0, True), (70.0, False)])
def test_small_contact_flags_slender_parts(height, warned):
    # 100 mm² of contact is > 1 % of the surface, but taller than 8·√100 = 80 mm is too slender
    a = analyze_mesh(trimesh.creation.box(extents=(10, 10, height)))
    assert a.report.has("PRINT.SMALL_CONTACT", Severity.WARNING) is warned


def test_thin_plate_warning():
    plate = trimesh.creation.box(extents=(40, 40, 0.5))
    a = analyze_mesh(plate)
    assert a.min_wall_mm == pytest.approx(0.5, abs=1e-3)
    # 0.4 mm nozzle ≤ 0.5 mm < 0.8 mm min wall → WARNING, not ERROR
    f = a.report.by_code("PRINT.THIN_WALL")
    assert len(f) == 1 and f[0].severity == Severity.WARNING
    assert f[0].data["min_wall_mm"] == pytest.approx(0.5, abs=1e-3)
    assert f[0].data["area_mm2"] == pytest.approx(3200.0, rel=0.1)  # top + bottom
    big = np.abs(plate.face_normals[:, 2]) > 0.99
    assert a.thin_face_mask[big].all()
    assert not a.thin_face_mask[~big].any()  # across the 0.5 mm edges the part is 40 mm wide


def test_thin_mask_marks_both_sides_of_a_wall():
    plate = trimesh.creation.box(extents=(40, 40, 0.5))
    a = analyze_mesh(plate, wall_samples=1)  # one ray, from one side of the plate…
    nz = plate.face_normals[a.thin_face_mask, 2]
    assert (nz > 0.99).sum() == 1 and (nz < -0.99).sum() == 1  # …flags the face it exits through too


def test_thin_plate_error_below_nozzle():
    a = analyze_mesh(trimesh.creation.box(extents=(40, 40, 0.3)))
    assert a.report.has("PRINT.THIN_WALL", Severity.ERROR)
    assert not a.report.ok


def test_too_big():
    bar = trimesh.creation.box(extents=(300, 10, 10))
    a = analyze_mesh(bar, "prusa_mk4")
    assert not a.fits_bed
    f = a.report.by_code("PRINT.TOO_BIG")
    assert len(f) == 1 and f[0].severity == Severity.ERROR
    assert f[0].data["size_mm"] == pytest.approx([300, 10, 10])
    assert f[0].data["build_volume_mm"] == pytest.approx([250, 210, 220])
    # standing it up does not help either: 300 mm > 220 mm build height
    assert not analyze_mesh(bar, "prusa_mk4", rotation=(0, 90, 0)).fits_bed


def test_quarter_turn_on_the_bed_counts_as_fitting():
    # 230 mm exceeds prusa_mk4's 210 mm Y travel, but turned 90° on the bed it fits the 250 mm X
    a = analyze_mesh(trimesh.creation.box(extents=(200, 230, 10)), "prusa_mk4")
    assert a.fits_bed and not a.report.has("PRINT.TOO_BIG")


def test_masks_align_with_faces(mushroom):
    # model the mushroom lying on its side, then analyse it with the rotation that stands it up
    lying = mushroom.copy()
    lying.apply_transform(to_homogeneous(rotation_matrix(-90, 0, 0)))
    before = lying.vertices.copy()
    r = rotation_matrix(90, 0, 0)
    a = analyze_mesh(lying, rotation=r)
    assert np.allclose(a.rotation, r)
    n = len(lying.faces)
    assert a.overhang_face_mask.shape == (n,) and a.overhang_face_mask.dtype == bool
    assert a.thin_face_mask.shape == (n,) and a.thin_face_mask.dtype == bool
    # flagged faces (ORIGINAL order) are exactly the cap underside: facing straight down once rotated
    down_z = (lying.face_normals @ r.T)[:, 2]
    assert a.overhang_face_mask.any() and np.all(down_z[a.overhang_face_mask] < -0.99)
    assert lying.area_faces[a.overhang_face_mask].sum() == pytest.approx(math.pi * 200, rel=0.03)
    assert np.array_equal(lying.vertices, before)  # input mesh untouched


def test_rotation_accepts_degrees_and_auto(mushroom):
    flipped = analyze_mesh(mushroom, rotation=(180, 0, 0))
    assert np.allclose(flipped.rotation, rotation_matrix(180, 0, 0))
    assert flipped.overhang_area_mm2 == 0.0  # cap flat on the bed
    auto = analyze_mesh(mushroom, rotation="auto")
    assert auto.overhang_area_mm2 == 0.0
    assert auto.bed_contact_area_mm2 == pytest.approx(math.pi * 15**2, rel=0.03)


@pytest.mark.parametrize("bad", [np.diag([1.0, 1.0, -1.0]), np.ones((3, 3)), np.ones(4), "sideways"])
def test_invalid_rotation_rejected(cube20, bad):
    with pytest.raises(ValidationError):
        analyze_mesh(cube20, rotation=bad)


def test_open_mesh_is_not_watertight(cube20):
    open_box = trimesh.Trimesh(cube20.vertices, cube20.faces[1:])
    a = analyze_mesh(open_box)
    assert not a.watertight
    assert a.report.has("PRINT.NOT_WATERTIGHT", Severity.ERROR)
    assert a.overhang_face_mask.shape == (11,) and a.thin_face_mask.shape == (11,)


def test_multiple_bodies_info():
    a = trimesh.creation.box(extents=(10, 10, 10))
    b = trimesh.creation.box(extents=(10, 10, 10))
    b.apply_translation((30, 0, 0))
    res = analyze_mesh(trimesh.util.concatenate([a, b]))
    assert res.body_count == 2 and res.watertight
    assert res.report.has("PRINT.MULTIPLE_BODIES", Severity.INFO)


def test_inconsistent_winding_warns_and_analysis_uses_repaired_normals(cube20):
    faces = cube20.faces.copy()
    top = int(np.argmax(cube20.face_normals[:, 2]))  # one triangle of the top face
    faces[top] = faces[top][::-1]  # now "faces down" at z = 20: a fake 200 mm² overhang
    bad = trimesh.Trimesh(cube20.vertices, faces, process=False)
    a = analyze_mesh(bad)
    assert not a.winding_consistent
    assert a.report.has("PRINT.INCONSISTENT_WINDING", Severity.WARNING)
    assert a.overhang_area_mm2 == 0.0


def test_inside_out_mesh_is_analysed_outward(cube20):
    inverted = cube20.copy()
    inverted.invert()
    a = analyze_mesh(inverted)
    assert a.report.has("PRINT.INCONSISTENT_WINDING", Severity.WARNING)
    assert a.volume_mm3 == pytest.approx(8000.0)
    assert a.overhang_area_mm2 == 0.0
    assert a.bed_contact_area_mm2 == pytest.approx(400.0, abs=1.0)


def test_wall_thickness_samples_measures_plate():
    plate = trimesh.creation.box(extents=(40, 40, 2))
    fi, t = wall_thickness_samples(plate, n=400, seed=1)
    assert fi.shape == t.shape == (400,)
    assert fi.min() >= 0 and fi.max() < len(plate.faces)
    skin = np.abs(plate.face_normals[fi, 2]) > 0.99
    assert skin.any() and (~skin).any()
    assert np.allclose(t[skin], 2.0, atol=1e-3)
    assert np.allclose(t[~skin], 40.0, atol=1e-3)
    fi2, t2 = wall_thickness_samples(plate, n=400, seed=1)
    assert np.array_equal(fi, fi2) and np.array_equal(t, t2)


def test_wall_thickness_fallback_matches_embree(mushroom, monkeypatch):
    fi, t = wall_thickness_samples(mushroom, n=300, seed=3)
    monkeypatch.setattr(analyze_mod, "PREFER_EMBREE", False)
    fi2, t2 = wall_thickness_samples(mushroom, n=300, seed=3)
    assert np.array_equal(fi, fi2)
    assert np.isfinite(t).mean() > 0.99
    assert np.allclose(t, t2, atol=1e-3, equal_nan=True)


def test_wall_thickness_open_surface_gives_nan():
    tri = trimesh.Trimesh([[0, 0, 0], [10, 0, 0], [0, 10, 0]], [[0, 1, 2]])
    fi, t = wall_thickness_samples(tri, n=10)
    assert fi.shape == (10,) and np.isnan(t).all()


def test_to_dict_is_json_safe(mushroom):
    a = analyze_mesh(mushroom, "prusa_mk4", "PETG", name="cap")
    d = a.to_dict()
    json.dumps(d)
    assert "overhang_face_mask" not in d and "thin_face_mask" not in d
    assert (d["name"], d["printer"], d["material"]) == ("cap", "prusa_mk4", "PETG")
    assert d["overhang_area_mm2"] == pytest.approx(a.overhang_area_mm2)
    assert d["estimate"]["mass_g"] == pytest.approx(a.estimate.mass_g)
    assert d["report"]["counts"]["warning"] >= 1
    assert np.allclose(d["rotation"], np.eye(3))


def test_wall_samples_zero_skips_thickness_check():
    a = analyze_mesh(trimesh.creation.box(extents=(40, 40, 0.3)), wall_samples=0)
    assert a.min_wall_mm is None and not a.thin_face_mask.any()
    assert not a.report.has("PRINT.THIN_WALL")


@pytest.mark.parametrize("kwargs", [{"wall_samples": -5}, {"wall_samples": 2.5}, {"infill": 2.0}])
def test_invalid_options_rejected(cube20, kwargs):
    with pytest.raises(ValidationError):
        analyze_mesh(cube20, **kwargs)


@pytest.mark.parametrize("bad", [trimesh.Trimesh(), "part.stl"])
def test_invalid_mesh_rejected(bad):
    with pytest.raises(ValidationError):
        analyze_mesh(bad)


@pytest.mark.parametrize("fn", [analyze_mesh, overhang_mask, bridge_face_mask, wall_thickness_samples,
                                estimate_print, best_orientation])
def test_non_finite_vertices_rejected(cube20, fn):
    vertices = cube20.vertices.copy()
    vertices[0, 0] = np.nan
    with pytest.raises(ValidationError, match="finite"):
        fn(trimesh.Trimesh(vertices, cube20.faces, process=False))


def test_package_reexports_are_lazy():
    code = (
        "import sys\n"
        "import piforge.fab as fab\n"
        "assert 'trimesh' not in sys.modules, 'importing piforge.fab must stay light'\n"
        "from piforge.fab import (PrintAnalysis, analyze_mesh, overhang_mask, wall_thickness_samples,\n"
        "    bridge_face_mask,\n"
        "    OrientationCandidate, best_orientation, candidate_rotations, PrintEstimate, estimate_print,\n"
        "    SliceResult, SlicerInfo, SlicerNotFoundError, find_slicer, parse_gcode_stats, slice_stl,\n"
        "    place_on_bed, rotation_matrix, get_printer, MATERIALS)\n"
        "assert fab.analyze_mesh.__module__ == 'piforge.fab.analyze'\n"
        "assert set(fab.__all__) <= set(dir(fab))\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-2000:]


# -- bed fillets: small rounded/chamfered bottom edges are not overhang --------------------------
def _hull(points) -> trimesh.Trimesh:
    return trimesh.convex.convex_hull(np.asarray(points, dtype=float))


def _rounded_lid(r: float, *, bottom: bool = True, top: bool = True, size=(40.0, 30.0, 3.0)):
    """Box whose bottom and/or top edges are rounded with radius ``r`` (hull of corner spheres)."""
    sx, sy, sz = size
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=r).vertices
    pts = []
    for x in (r, sx - r):
        for y in (r, sy - r):
            for zc, rounded, up in ((r, bottom, False), (sz - r, top, True)):
                if rounded:
                    pts.append(sphere[(sphere[:, 2] >= 0) == up] + (x, y, zc))
                else:
                    pts.append(np.array([[x - r, y - r], [x + r, y - r], [x - r, y + r],
                                         [x + r, y + r]]).repeat(1, axis=0)
                               @ np.eye(2, 3) + (0, 0, 0 if not up else sz))
    return _hull(np.vstack(pts))


@pytest.mark.parametrize("radius", [0.5, 1.0])
def test_rounded_bottom_lid_has_a_bed_fillet_not_overhang(radius):
    lid = _rounded_lid(radius)
    a = analyze_mesh(lid, wall_samples=0)
    assert a.overhang_area_mm2 == 0.0 and not a.needs_supports
    assert not a.report.has("PRINT.OVERHANG")
    info = a.report.by_code("PRINT.BED_FILLET")
    assert len(info) == 1 and info[0].severity == Severity.INFO
    assert info[0].data["count"] == 1 and info[0].data["area_mm2"] > 5.0
    assert info[0].data["max_height_mm"] <= info[0].data["limit_mm"] == pytest.approx(1.0)
    assert a.fillet_count == 1 and a.fillet_area_mm2 == pytest.approx(info[0].data["area_mm2"])
    assert a.to_dict()["fillet_count"] == 1
    assert not a.overhang_face_mask.any()
    # the geometric mask still sees the band; with a bridge/fillet-aware call it is dropped
    assert overhang_mask(lid).any() and not overhang_mask(lid, max_bridge_mm=20.0).any()
    assert not overhang_mask(_soup(lid), max_bridge_mm=20.0).any()


def test_best_orientation_sees_no_overhang_in_a_rounded_lid_lying_flat():
    lid = _rounded_lid(0.5)
    best = best_orientation(lid)[0]
    assert best.overhang_area_mm2 == 0.0 and best.height_mm == pytest.approx(3.0, abs=1e-6)


def test_small_chamfer_lip_at_the_bed_is_not_overhang():
    # 1 mm wide, 0.6 mm tall chamfer (59° from vertical: steeper than 45° → geometric overhang)
    body = _hull([(x, y, z) for x, y in ((0, 0), (20, 0), (0, 20), (20, 20)) for z in (5.0,)]
                 + [(x + dx, y + dy, 0.6) for x, y, dx, dy in
                    ((0, 0, 0, 0), (20, 0, 0, 0), (0, 20, 0, 0), (20, 20, 0, 0))]
                 + [(1, 1, 0), (19, 1, 0), (1, 19, 0), (19, 19, 0)])
    assert overhang_mask(body).any()
    a = analyze_mesh(body, wall_samples=0)
    assert a.overhang_area_mm2 == 0.0 and a.report.has("PRINT.BED_FILLET")


def test_one_by_one_chamfer_at_the_bed_is_not_overhang():
    body = _hull([(0, 0, 1), (20, 0, 1), (0, 20, 1), (20, 20, 1), (0, 0, 5), (20, 0, 5),
                  (0, 20, 5), (20, 20, 5), (1, 1, 0), (19, 1, 0), (1, 19, 0), (19, 19, 0)])
    a = analyze_mesh(body, wall_samples=0)
    assert a.overhang_area_mm2 == 0.0 and not a.report.has("PRINT.OVERHANG")


def test_wide_ledge_just_above_the_bed_is_still_overhang():
    # 3 mm ledge at 0.5 mm height: wider than tall → not a fillet
    ledge = trimesh.boolean.union([_box((0, 0, 0), (20, 20, 6)), _box((-3, 0, 0.5), (0.5, 20, 1.5))],
                                  engine="manifold")
    a = analyze_mesh(ledge, wall_samples=0)
    assert a.report.has("PRINT.OVERHANG", Severity.WARNING) and a.needs_supports
    assert a.overhang_area_mm2 == pytest.approx(60.0, rel=0.05)
    assert not a.report.has("PRINT.BED_FILLET")


def test_narrow_ledge_that_is_a_fillet_sized_lip_is_not_overhang():
    lip = trimesh.boolean.union([_box((0, 0, 0), (20, 20, 6)), _box((-0.8, 0, 0.5), (0.5, 20, 1.5))],
                                engine="manifold")
    a = analyze_mesh(lip, wall_samples=0)
    assert a.overhang_area_mm2 == 0.0 and a.report.has("PRINT.BED_FILLET")


def test_tall_lip_is_not_a_bed_fillet_even_if_narrow():
    lip = trimesh.boolean.union([_box((0, 0, 0), (40, 40, 30)), _box((-0.8, 0, 12), (0.5, 40, 13))],
                                engine="manifold")  # 0.8 mm lip 12 mm above the bed
    a = analyze_mesh(lip, wall_samples=0)
    assert a.report.has("PRINT.OVERHANG", Severity.WARNING)
    assert not a.report.has("PRINT.BED_FILLET")


def test_fillet_limit_grows_with_layer_height():
    lid = _rounded_lid(2.0)  # band is ~1.7 mm wide: too big for the 1 mm limit at 0.2 mm layers…
    assert analyze_mesh(lid, wall_samples=0).overhang_area_mm2 > 0.0
    thick = get_printer("generic").with_(layer_h=0.6)  # …but 4 × 0.6 = 2.4 mm covers it
    assert analyze_mesh(lid, thick, wall_samples=0).overhang_area_mm2 == 0.0


def test_rounded_top_edge_on_a_tall_part_is_classified_as_before():
    part = _rounded_lid(3.0, bottom=False, top=True, size=(40.0, 30.0, 12.0))
    a = analyze_mesh(part, wall_samples=0)  # top fillets face up: never overhang, never a fillet
    assert a.overhang_area_mm2 == 0.0 and not a.report.has("PRINT.BED_FILLET")
    assert not a.report.has("PRINT.OVERHANG")
    # the same part upside down hangs its r=3 fillets in the air well above the bed… only the
    # lowest 1 mm band may count as a fillet, so most of it is overhang
    flipped = analyze_mesh(part, rotation=(180.0, 0.0, 0.0), wall_samples=0)
    assert flipped.report.has("PRINT.OVERHANG") or flipped.report.has("PRINT.BED_FILLET")


@pytest.mark.slow
def test_embossed_text_is_fine_detail_not_thin_wall():
    import build123d as bd

    from piforge.mech.export import to_trimesh
    from piforge.mech.primitives import emboss

    plate = bd.Box(60, 20, 2.0, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    part = emboss(plate, "28.5 C", size=6.0, height=0.6)
    a = analyze_mesh(to_trimesh(part), "prusa_mk4", "PETG", wall_samples=3000)
    assert not a.report.has("PRINT.THIN_WALL"), a.report.to_markdown()
    assert a.report.has("PRINT.FINE_DETAIL", Severity.INFO)


def test_tall_thin_wall_still_warns():
    wall = _join(_box((0, 0, 0), (40, 20, 2)), _box((10, 9.7, 1.9), (30, 10.3, 22)))  # 0.6 × 20 mm tall fin
    a = analyze_mesh(wall, "prusa_mk4", "PETG", wall_samples=3000)
    f = a.report.by_code("PRINT.THIN_WALL")
    assert f and f[0].severity == Severity.WARNING
    assert f[0].data["min_wall_mm"] == pytest.approx(0.6, abs=0.02)
    assert not a.report.has("PRINT.FINE_DETAIL")


def test_low_thin_rib_is_fine_detail():
    rib = _join(_box((0, 0, 0), (40, 20, 2)), _box((10, 9.7, 1.9), (30, 10.3, 3.0)))  # 0.6 wide, 1 mm tall
    a = analyze_mesh(rib, "prusa_mk4", "PETG", wall_samples=3000)
    assert not a.report.has("PRINT.THIN_WALL")
    assert a.report.has("PRINT.FINE_DETAIL", Severity.INFO)
