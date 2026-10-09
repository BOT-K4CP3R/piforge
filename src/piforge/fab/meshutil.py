"""Small mesh helpers shared by fabrication analysis and CAD export (trimesh only)."""

from __future__ import annotations

import numpy as np
import trimesh


def rotation_matrix(rx: float = 0.0, ry: float = 0.0, rz: float = 0.0) -> np.ndarray:
    """3x3 rotation from angles in degrees, applied about the fixed X, then Y, then Z axes.

    Equivalent to ``Rz @ Ry @ Rx``; e.g. ``rotation_matrix(90, 0, 0)`` maps +Y onto +Z.
    """
    ax, ay, az = np.radians([rx, ry, rz])
    cx, sx = np.cos(ax), np.sin(ax)
    cy, sy = np.cos(ay), np.sin(ay)
    cz, sz = np.cos(az), np.sin(az)
    r_x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    r_y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    r_z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    r = r_z @ r_y @ r_x
    r[np.abs(r) < 1e-12] = 0.0  # clean -0.0/1e-17 noise so 90° rotations are exact
    return r


def to_homogeneous(rotation: np.ndarray) -> np.ndarray:
    """Embed a 3x3 rotation in a 4x4 transform."""
    m = np.eye(4)
    m[:3, :3] = np.asarray(rotation, dtype=float)
    return m


def place_on_bed(mesh: trimesh.Trimesh, rotation: np.ndarray | None = None) -> trimesh.Trimesh:
    """Return a rotated copy whose lowest point sits on z = 0 and whose XY bbox is centred at 0."""
    out = mesh.copy()
    if rotation is not None:
        out.apply_transform(to_homogeneous(rotation))
    lo, hi = out.bounds
    shift = np.array([-(lo[0] + hi[0]) / 2.0, -(lo[1] + hi[1]) / 2.0, -lo[2]])
    out.apply_translation(shift)
    return out
