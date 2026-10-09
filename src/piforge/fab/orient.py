"""Automatic print orientation (spec §6.1).

Candidates are the six axis-aligned "this side down" rotations plus rotations that lay the
largest convex-hull faces flat on the bed. They are ranked in tiers — (0) fits, needs no supports
(overhang below the ``PRINT.OVERHANG`` warning threshold, bridges excluded) and stands on a real
footprint; (1) fits otherwise; (2) does not fit — and within a tier by a score in which support
area dominates, height (print time, wobble) breaks ties and bed contact (adhesion) is a bonus.
Only the up-direction is chosen: rotations about Z (e.g. placing a long part diagonally) are left
to the slicer, apart from the quarter turn that bed-fit checks allow.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh

from piforge.core.errors import ValidationError
from piforge.fab.analyze import (
    OVERHANG_WARN_MM2,
    SMALL_CONTACT_FRACTION,
    _placement_metrics,
    _small_contact,
    _welded,
)
from piforge.fab.estimate import _require_mesh
from piforge.fab.meshutil import rotation_matrix
from piforge.fab.profiles import PrinterProfile, get_printer

logger = logging.getLogger(__name__)

# Score weights — heuristics, not physics (lower score = better). Tiers decide first; within a
# tier support area dominates (1 mm of height ≈ 0.02·s mm² of overhang), height breaks ties.
_W_OVERHANG = 10.0  # per mm² of overhang that needs support
_W_HEIGHT = 0.2  # per mm of height, × s = the edge of a cube with the part's surface area
_W_CONTACT = 0.1  # reward per mm² of bed contact
_SMALL_CONTACT_PENALTY = 0.25  # × total surface area when PRINT.SMALL_CONTACT would fire
_TIER_STEP = 1000.0  # added per tier; the in-tier part of the score stays far below it
_SAME_DIRECTION_COS = math.cos(math.radians(0.5))  # candidates closer than 0.5° are duplicates

_AXIS_DOWN: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("as modelled", (0.0, 0.0, -1.0)),
    ("upside down", (0.0, 0.0, 1.0)),
    ("-X down", (-1.0, 0.0, 0.0)),
    ("+X down", (1.0, 0.0, 0.0)),
    ("-Y down", (0.0, -1.0, 0.0)),
    ("+Y down", (0.0, 1.0, 0.0)),
)


@dataclass
class OrientationCandidate:
    """One way to put the part on the bed. ``score`` is a penalty: lower is better.

    ``overhang_area_mm2`` counts only overhang that needs support (bridges excluded).

    ``rotation`` (3x3) maps the model frame to the print frame; pass it to
    :func:`piforge.fab.analyze.analyze_mesh` or :func:`piforge.fab.meshutil.place_on_bed`.
    """

    rotation: np.ndarray
    label: str
    score: float
    overhang_area_mm2: float
    height_mm: float
    contact_area_mm2: float
    fits_bed: bool


def _rotation_to_bed(direction: np.ndarray) -> np.ndarray:
    """Smallest rotation that turns model-frame ``direction`` to face straight down (−Z)."""
    d = np.asarray(direction, dtype=float)
    d = d / np.linalg.norm(d)
    target = np.array([0.0, 0.0, -1.0])
    cos_a = float(np.clip(d @ target, -1.0, 1.0))
    if cos_a > 1.0 - 1e-12:
        return np.eye(3)
    if cos_a < -1.0 + 1e-12:  # pointing straight up: flip about X
        return rotation_matrix(180.0, 0.0, 0.0)
    axis = np.cross(d, target)
    sin_a = float(np.linalg.norm(axis))
    axis /= sin_a
    k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    r = np.eye(3) + sin_a * k + (1.0 - cos_a) * (k @ k)  # Rodrigues
    r[np.abs(r) < 1e-12] = 0.0  # exact 0/±1 entries for axis-aligned cases
    return r


def _hull_faces(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    """(outward normals (k, 3), areas (k,)) of the convex hull's flat faces, largest first."""
    try:
        hull = mesh.convex_hull
        facets = hull.facets
        normals = np.asarray(hull.face_normals, dtype=float)
        areas = np.asarray(hull.area_faces, dtype=float)
        facet_areas = np.asarray(hull.facets_area, dtype=float).reshape(-1)
        facet_normals = np.asarray(hull.facets_normal, dtype=float).reshape(-1, 3)
    except Exception as exc:  # qhull rejects flat/degenerate input; then only axis candidates
        logger.debug("convex hull failed (%s); using axis-aligned candidates only", exc)
        return np.zeros((0, 3)), np.zeros(0)
    in_facet = np.zeros(len(normals), dtype=bool)
    if len(facets):
        in_facet[np.concatenate(facets)] = True
    # coplanar facets plus the triangles not merged into any facet
    all_normals = np.vstack([facet_normals, normals[~in_facet]])
    all_areas = np.concatenate([facet_areas, areas[~in_facet]])
    order = np.argsort(-all_areas, kind="stable")
    return all_normals[order], all_areas[order]


def candidate_rotations(mesh: trimesh.Trimesh,
                        max_candidates: int = 12) -> list[tuple[str, np.ndarray]]:
    """Up to ``max_candidates`` distinct ``(label, rotation)`` pairs, "as modelled" first.

    The six axis-aligned orientations come first, then the largest convex-hull faces laid flat
    (skipping any that put the same side down as an earlier candidate).
    """
    mesh = _require_mesh(mesh)
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, (int, np.integer)) \
            or max_candidates < 1:
        raise ValidationError(f"max_candidates must be an integer ≥ 1, got {max_candidates!r}")
    out: list[tuple[str, np.ndarray]] = []
    downs: list[np.ndarray] = []

    def add(label: str, direction: Any) -> None:
        d = np.asarray(direction, dtype=float)
        d = d / np.linalg.norm(d)
        if any(float(d @ e) > _SAME_DIRECTION_COS for e in downs):
            return
        downs.append(d)
        out.append((label, _rotation_to_bed(d)))

    for label, direction in _AXIS_DOWN:
        if len(out) >= max_candidates:
            break
        add(label, direction)
    hull_normals, _ = _hull_faces(mesh)
    for k, normal in enumerate(hull_normals, start=1):
        if len(out) >= max_candidates:
            break
        if np.linalg.norm(normal) < 0.5:  # degenerate hull triangle without a normal
            continue
        nx, ny, nz = normal
        add(f"hull face {k} down (n = {nx:+.2f}, {ny:+.2f}, {nz:+.2f})", normal)
    return out


def best_orientation(mesh: trimesh.Trimesh, printer: str | PrinterProfile = "generic", *,
                     max_candidates: int = 12) -> list[OrientationCandidate]:
    """Rank :func:`candidate_rotations` for printing on ``printer``; the best comes first.

    score = 1000·tier + (10·overhang mm² + 0.2·s·height − 0.1·contact mm²
    [+ 0.25·A when PRINT.SMALL_CONTACT would fire]) / A, lower is better. A is the surface area,
    s = √(A/6). Tier 0: fits, overhang < 25 mm² (bridges excluded) and bed contact ≥ 1 % of A;
    tier 1: fits otherwise; tier 2: does not fit. A support-free orientation therefore always beats
    one that needs supports, however much lower that one is. Ties keep candidate order.
    Overhang follows :func:`~piforge.fab.analyze.analyze_mesh`: the mesh is welded by position
    first, so a triangle soup's bridges are recognised too.
    """
    mesh = _require_mesh(mesh)
    prof = get_printer(printer)
    total = float(mesh.area)
    if total <= 0.0:
        raise ValidationError("Mesh has zero surface area.")
    welded = _welded(mesh)  # candidates come from the caller's mesh: its cached hull is reused
    cube_edge = math.sqrt(total / 6.0)
    ranked: list[OrientationCandidate] = []
    for label, rot in candidate_rotations(mesh, max_candidates):
        m = _placement_metrics(welded, rot, prof)
        support_free = m["overhang_area"] < OVERHANG_WARN_MM2
        footprint = m["contact_area"] >= SMALL_CONTACT_FRACTION * total
        tier = 2 if not m["fits"] else (0 if support_free and footprint else 1)
        penalty = (_W_OVERHANG * m["overhang_area"] + _W_HEIGHT * cube_edge * m["height"]
                   - _W_CONTACT * m["contact_area"])
        if _small_contact(m["contact_area"], total, m["height"]):
            penalty += _SMALL_CONTACT_PENALTY * total
        score = _TIER_STEP * tier + penalty / total
        ranked.append(OrientationCandidate(
            rotation=rot, label=label, score=round(score, 9),
            overhang_area_mm2=m["overhang_area"], height_mm=m["height"],
            contact_area_mm2=m["contact_area"], fits_bed=m["fits"],
        ))
    ranked.sort(key=lambda c: c.score)  # stable: equal scores keep "as modelled" first
    return ranked
