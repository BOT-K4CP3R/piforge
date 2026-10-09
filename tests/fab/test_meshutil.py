import numpy as np
import trimesh

from piforge.fab.meshutil import place_on_bed, rotation_matrix, to_homogeneous


def test_rotation_matrix_axes():
    assert np.allclose(rotation_matrix(90, 0, 0) @ [0, 1, 0], [0, 0, 1])
    assert np.allclose(rotation_matrix(0, 90, 0) @ [0, 0, 1], [1, 0, 0])
    assert np.allclose(rotation_matrix(0, 0, 90) @ [1, 0, 0], [0, 1, 0])
    r = rotation_matrix(30, -45, 120)
    assert np.allclose(r @ r.T, np.eye(3)) and np.isclose(np.linalg.det(r), 1.0)


def test_rotation_order_is_x_then_y_then_z():
    r = rotation_matrix(90, 90, 0)
    # X first: +Y -> +Z, then Y: +Z -> +X
    assert np.allclose(r @ [0, 1, 0], [1, 0, 0])


def test_place_on_bed_copy_and_position():
    box = trimesh.creation.box(extents=(10, 20, 30))
    box.apply_translation((100, 50, 7))
    placed = place_on_bed(box, rotation_matrix(90, 0, 0))
    lo, hi = placed.bounds
    assert np.isclose(lo[2], 0.0)
    assert np.allclose((lo[:2] + hi[:2]) / 2, 0.0)
    assert np.allclose(placed.extents, (10, 30, 20))
    assert np.allclose(box.bounds[0], (95, 40, -8))  # original untouched
    assert to_homogeneous(np.eye(3)).shape == (4, 4)
