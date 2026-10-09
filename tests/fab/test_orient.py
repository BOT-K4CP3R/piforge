import math

import numpy as np
import pytest
import trimesh

from piforge.core import ValidationError
from piforge.fab.analyze import analyze_mesh
from piforge.fab.meshutil import rotation_matrix, to_homogeneous
from piforge.fab.orient import OrientationCandidate, best_orientation, candidate_rotations


def _down_directions(cands):
    """Model-frame direction that ends up facing the bed for each candidate rotation."""
    return np.array([r.T @ np.array([0.0, 0.0, -1.0]) for _, r in cands])


def test_candidate_rotations_are_proper_and_distinct(mushroom):
    cands = candidate_rotations(mushroom)
    assert 6 < len(cands) <= 12  # 6 axis-aligned + the largest convex-hull faces
    labels = [label for label, _ in cands]
    assert len(set(labels)) == len(labels)
    assert np.allclose(cands[0][1], np.eye(3))  # "as modelled" comes first
    for _, r in cands:
        assert r.shape == (3, 3)
        assert np.allclose(r @ r.T, np.eye(3), atol=1e-9) and np.isclose(np.linalg.det(r), 1.0)
    downs = _down_directions(cands)
    gram = downs @ downs.T
    np.fill_diagonal(gram, 0.0)
    assert gram.max() < 0.9999  # no two candidates put the same side down


def test_axis_aligned_candidates_put_each_side_down(cube20):
    cands = candidate_rotations(cube20)
    assert len(cands) == 6  # the cube's hull faces coincide with the six axis directions
    downs = _down_directions(cands)
    expected = [(0, 0, -1), (0, 0, 1), (-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0)]
    assert np.allclose(downs, expected, atol=1e-12)


def test_candidate_rotations_respects_max(mushroom):
    assert len(candidate_rotations(mushroom, max_candidates=3)) == 3
    with pytest.raises(ValidationError):
        candidate_rotations(mushroom, max_candidates=0)


def test_orientation_prefers_flat(gamma_bracket):
    # it really is lying wrong: as modelled, the arm's underside hangs in the air
    assert analyze_mesh(gamma_bracket).overhang_area_mm2 == pytest.approx(36 * 20)
    cands = best_orientation(gamma_bracket)
    assert all(isinstance(c, OrientationCandidate) for c in cands)
    best = cands[0]
    assert best.overhang_area_mm2 == pytest.approx(0.0, abs=1e-9)
    assert best.fits_bed
    scores = [c.score for c in cands]
    assert scores == sorted(scores)  # best first
    assert analyze_mesh(gamma_bracket, rotation=best.rotation).overhang_area_mm2 == pytest.approx(0.0)


def _union(*bounds) -> trimesh.Trimesh:
    return trimesh.boolean.union([trimesh.creation.box(bounds=b) for b in bounds], engine="manifold")


def test_taller_support_free_orientation_beats_lower_one_needing_supports():
    # 40×30×20 block with a 2 mm lip along the middle of its +X face: lying flat (20 mm tall) the lip
    # needs 60 mm² of support whichever way up; on its side (30 mm tall) it prints support-free
    part = _union(((0, 0, 0), (40, 30, 20)), ((39, 0, 8), (42, 30, 12)))
    cands = best_orientation(part)
    modelled = next(c for c in cands if c.label == "as modelled")
    assert modelled.overhang_area_mm2 == pytest.approx(60.0) and modelled.height_mm == pytest.approx(20)
    best = cands[0]
    assert best.overhang_area_mm2 < 25.0  # below the PRINT.OVERHANG warning threshold
    assert best.height_mm == pytest.approx(30.0) and best.fits_bed


def test_support_free_tier_wins_however_low_the_alternative():
    # 100×30×4 plate with a small tab on one end: flat it is only 4 mm tall but the tab's 30 mm²
    # underside needs support; on its long edge (30 mm) only 6 mm² hangs — support-free wins
    part = _union(((0, 0, 0), (100, 30, 4)), ((99, 10, 1), (103, 20, 3)))
    cands = best_orientation(part)
    modelled = next(c for c in cands if c.label == "as modelled")
    assert modelled.overhang_area_mm2 == pytest.approx(30.0) and modelled.height_mm == pytest.approx(4)
    best = cands[0]
    assert best.overhang_area_mm2 < 25.0 and best.height_mm == pytest.approx(30.0)


def test_support_area_dominates_height_within_a_tier():
    # 200×150×100 block + 10 mm lip: fits no orientation on a prusa_mini (180 mm cube), so all
    # candidates share the last tier — there 1,500 mm² of support must still outweigh 50 mm of height
    part = _union(((0, 0, 0), (200, 150, 100)), ((195, 0, 40), (210, 150, 60)))
    cands = best_orientation(part, "prusa_mini")
    assert not any(c.fits_bed for c in cands)
    modelled = next(c for c in cands if c.label == "as modelled")
    assert modelled.overhang_area_mm2 == pytest.approx(1500.0) and modelled.height_mm == 100.0
    assert cands[0].overhang_area_mm2 == 0.0 and cands[0].height_mm == pytest.approx(150.0)


def test_height_breaks_ties_between_support_free_orientations():
    # L-profile (40 × 30 mm legs, 15 mm deep): standing as modelled and lying on its side, it touches
    # the bed with the same 600 mm² — but lying it is 15 mm tall instead of 30 mm
    part = _union(((0, 0, 0), (40, 15, 10)), ((0, 0, 0), (10, 15, 30)))
    cands = best_orientation(part)
    modelled = next(c for c in cands if c.label == "as modelled")
    assert modelled.contact_area_mm2 == pytest.approx(600.0) and modelled.overhang_area_mm2 == 0.0
    best = cands[0]
    assert best.overhang_area_mm2 == pytest.approx(0.0, abs=1e-9)
    assert best.contact_area_mm2 == pytest.approx(600.0)
    assert best.height_mm == pytest.approx(15.0)


def test_bed_contact_breaks_ties_of_height_and_overhang():
    # 20 mm cube with an 8×8×2 mm pocket in its bottom: every side down is 20 mm tall and needs no
    # support (the pocket's roof bridges) — but as modelled only 336 mm² touch the bed, not 400
    part = trimesh.boolean.difference([trimesh.creation.box(bounds=((0, 0, 0), (20, 20, 20))),
                                       trimesh.creation.box(bounds=((6, 6, -1), (14, 14, 2)))],
                                      engine="manifold")
    cands = best_orientation(part)
    modelled = next(c for c in cands if c.label == "as modelled")
    assert modelled.overhang_area_mm2 == 0.0 and modelled.contact_area_mm2 == pytest.approx(336.0)
    assert all(c.overhang_area_mm2 == 0.0 and c.height_mm == pytest.approx(20.0) for c in cands)
    assert cands[0].contact_area_mm2 == pytest.approx(400.0)


def test_orientation_balanced_on_an_edge_is_not_in_the_support_free_tier(cube20):
    # modelled standing on an edge: nothing overhangs (both lower faces sit exactly at 45°), but
    # with no footprint it would topple — it ranks in tier 1 (score ≥ 1000), never with tier 0
    tilted = cube20.copy()
    tilted.apply_transform(to_homogeneous(rotation_matrix(45, 0, 0)))
    cands = best_orientation(tilted)
    modelled = next(c for c in cands if c.label == "as modelled")
    assert modelled.overhang_area_mm2 == 0.0 and modelled.contact_area_mm2 < 1.0
    assert modelled.fits_bed and modelled.score >= 1000.0
    assert cands[0].contact_area_mm2 == pytest.approx(400.0) and cands[0].score < 1000.0


@pytest.mark.parametrize("welded", [True, False])
def test_bridged_hole_does_not_force_a_taller_orientation(welded):
    # flat (20 mm tall) the Ø3 mm hole's roof (~60 mm² facing down) bridges, so there is no reason to
    # stand the block on its side (30 mm) just to make the hole vertical
    hole = trimesh.creation.cylinder(radius=1.5, height=40, sections=32)
    hole.apply_transform(to_homogeneous(rotation_matrix(90, 0, 0)))  # axis along Y
    hole.apply_translation((20, 15, 10))
    block = trimesh.creation.box(bounds=((0, 0, 0), (40, 30, 20)))
    part = trimesh.boolean.difference([block, hole], engine="manifold")
    if not welded:  # a triangle soup (STL read with process=False) must rank the same as analyze_mesh
        part = trimesh.Trimesh(part.vertices[part.faces].reshape(-1, 3),
                               np.arange(3 * len(part.faces)).reshape(-1, 3), process=False)
    best = best_orientation(part)[0]
    assert best.label == "as modelled" and best.overhang_area_mm2 == 0.0


def test_mushroom_is_flipped_onto_its_cap(mushroom):
    best = best_orientation(mushroom)[0]
    assert best.overhang_area_mm2 == pytest.approx(0.0, abs=1e-9)
    assert best.height_mm == pytest.approx(13.0)
    assert best.contact_area_mm2 == pytest.approx(math.pi * 15**2, rel=0.03)
    assert (best.rotation @ [0, 0, 1])[2] == pytest.approx(-1.0)  # cap top now faces the bed


def test_tilted_box_is_laid_flat_via_hull_faces():
    box = trimesh.creation.box(extents=(40, 30, 10))
    box.apply_transform(to_homogeneous(rotation_matrix(30, 20, 10)))
    best = best_orientation(box)[0]
    assert best.overhang_area_mm2 == pytest.approx(0.0, abs=1e-6)
    assert best.height_mm == pytest.approx(10.0, abs=1e-6)
    assert best.contact_area_mm2 == pytest.approx(40 * 30, rel=1e-6)


def test_long_flat_part_lies_down():
    bar = trimesh.creation.box(extents=(10, 10, 100))  # modelled standing up
    best = best_orientation(bar)[0]
    assert best.height_mm == pytest.approx(10.0)
    assert best.contact_area_mm2 == pytest.approx(1000.0)


def test_best_orientation_finds_fit_when_one_exists():
    bar = trimesh.creation.box(extents=(260, 10, 10))  # too long for the 250×220 bed, fits standing (Z 270)
    cands = best_orientation(bar, "prusa_core_one")
    assert cands[0].fits_bed
    assert cands[0].height_mm == pytest.approx(260.0)
    assert not any(c.fits_bed for c in cands if c.height_mm < 100)


def test_no_orientation_fits_too_long_bar():
    bar = trimesh.creation.box(extents=(300, 10, 10))
    cands = best_orientation(bar, "prusa_mk4")  # 250×210×220: diagonal placement is not considered
    assert cands and not any(c.fits_bed for c in cands)
