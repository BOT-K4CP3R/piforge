"""Printability analysis of a triangle mesh in a chosen print orientation (spec §6.1).

:func:`analyze_mesh` places a copy of the mesh on the bed and checks watertightness and winding,
body count, bed fit, overhangs, bed contact, thin walls and a mass/time/cost estimate. Every
problem becomes a ``PRINT.*`` finding in :attr:`PrintAnalysis.report`.

Conventions (binding rulings for PiForge):

* Overhang angles are measured from vertical: a wall is 0°, a flat ceiling 90°. A face is an
  overhang only when it leans *more* than ``printer.max_overhang_deg + 1°`` — 45° chamfers print.
* The bed is the plane through the part's lowest point; faces within 0.05 mm of it rest on the bed
  (bed contact) and are never overhangs.
* An overhang region held up on two opposite sides by walls below it (port-cutout roof, top of a
  horizontal hole, cavity ceiling) whose span is at most ``printer.max_bridge_mm`` is a *bridge*:
  slicers print it without supports, so it is reported as ``PRINT.BRIDGE``, not as overhang.
* A small downward-facing region right above the bed — all of it within ``max(1 mm, 4 × layer_h)``
  of the bed and reaching out horizontally at most that far (a rounded bottom edge, a small
  chamfer: each layer steps out less than a line width, the elephant-foot case) — is a *bed
  fillet*: it prints fine, so it is reported as ``PRINT.BED_FILLET``, not as overhang. A ledge
  wider than it is tall is still overhang.
* A thin spot (thinner than ``printer.min_wall`` but at least one nozzle wide) on a feature that
  stands at most 2 mm proud of a thicker base — raised text, small ribs, the strokes of embossed
  letters — is *fine detail*: slicers print it with single lines, so it is reported as
  ``PRINT.FINE_DETAIL`` (INFO), not as ``PRINT.THIN_WALL``. The feature height is measured by
  walking from the middle of the thin wall across it (perpendicular to the thickness ray, in four
  directions): one way must leave the part within the height, the other must reach material
  that is clearly wider (the base) within the rest of it. Tall thin walls stay ``PRINT.THIN_WALL``.
* Per-face masks follow the face order of the mesh passed in, whatever rotation is applied.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from piforge.core.errors import ValidationError
from piforge.core.report import Report, Severity, jsonable
# package-private validators shared by the fab modules
from piforge.fab.estimate import (
    PrintEstimate,
    _check_count,
    _check_fraction,
    _require_mesh,
    estimate_print,
)
from piforge.fab.meshutil import place_on_bed, rotation_matrix
from piforge.fab.profiles import CURRENCY, Material, PrinterProfile, get_material, get_printer

logger = logging.getLogger(__name__)

#: Use Embree (``embreex``) for ray casts when installed; ``False`` forces trimesh's numpy fallback.
PREFER_EMBREE = True

# src: PiForge task-1 ruling — faces exactly at max_overhang (e.g. 45° chamfers) print without support.
OVERHANG_TOL_DEG = 1.0
# src: PiForge task-1 ruling — within 0.05 mm of the bed a face is squashed into the first layer.
BED_TOL_MM = 0.05
# src: PiForge task-1 brief (heuristic) — below 25 mm² an overhang (hole roof, small lip) usually prints.
OVERHANG_WARN_MM2 = 25.0
# src: PiForge task-1 brief (heuristic) — bed contact < 1 % of the surface risks detaching.
SMALL_CONTACT_FRACTION = 0.01
# src: PiForge task-1 fix-round-2 ruling — near-bed fillets/chamfers up to max(1 mm, 4 layers) tall
# and as far reaching print without support (each layer overhangs the last by < a line width).
BED_FILLET_MIN_MM = 1.0
BED_FILLET_LAYERS = 4.0
# src: PiForge task-1 brief (heuristic) — height / sqrt(contact area) > 8 is a tall, tippy part.
SMALL_CONTACT_ASPECT = 8.0
# src: PiForge bug-fix ruling (demo gauge) — thin features at most 2 mm proud of their base
# (embossed text, ribs) are fine detail, not structural walls; walked in 0.1 mm steps.
FINE_DETAIL_MAX_H_MM = 2.0
_FINE_STEP_MM = 0.1
# src: PiForge bug-fix ruling — a wall-thickness ray must leave through a face within 60° of
# opposite its entry face (cos 60° = 0.5); otherwise it only cuts across a corner.
_OPPOSITE_DOT = 0.5
# A roof edge is supported when the neighbouring face drops at least this far below it (a wall).
_SUPPORT_DROP_MM = 1e-4
# Two supports must face each other across at least this gap to carry a bridge.
_FACING_MIN_MM = 1e-3
# Resolution of bridge-span estimates (support edges are sampled this finely).
_SPAN_TOL_MM = 0.05
# Barycentric sample points per face for bridge spans: corners, edge midpoints, centre, inner points.
_SPAN_SAMPLES = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.5, 0.5, 0], [0, 0.5, 0.5],
                          [0.5, 0, 0.5], [1 / 3, 1 / 3, 1 / 3], [2 / 3, 1 / 6, 1 / 6],
                          [1 / 6, 2 / 3, 1 / 6], [1 / 6, 1 / 6, 2 / 3]])

RotationLike = np.ndarray | Sequence[float] | str | None


@dataclass
class PrintAnalysis:
    """Result of :func:`analyze_mesh`. Lengths in mm, areas in mm², volumes in mm³."""

    name: str
    printer: str
    material: str
    rotation: np.ndarray  # 3x3 rotation applied before placing the part on the bed
    watertight: bool
    winding_consistent: bool
    body_count: int
    volume_mm3: float
    area_mm2: float
    size_mm: tuple[float, float, float]  # bounding-box extents in print orientation
    fits_bed: bool
    overhang_area_mm2: float  # needs support: bridges are not included
    overhang_face_mask: np.ndarray  # bool per face, in the ORIGINAL mesh face order
    bridge_face_mask: np.ndarray  # bool per face (original order): overhangs printed as bridges
    bridge_count: int  # bridged regions (each a port roof, hole top, cavity ceiling…)
    bridge_area_mm2: float  # their total area — not part of overhang_area_mm2
    bed_contact_area_mm2: float
    min_wall_mm: float | None  # thinnest sampled wall; None when no ray found a wall
    thin_face_mask: np.ndarray  # bool per face (original order): faces on a too-thin wall
    needs_supports: bool
    estimate: PrintEstimate
    report: Report
    fillet_count: int = 0  # bed-fillet regions (rounded/chamfered bottom edges), not overhang
    fillet_area_mm2: float = 0.0

    def to_dict(self) -> dict:
        """JSON-safe summary (per-face masks excluded; bridges as a region count and area)."""
        return jsonable({
            "name": self.name,
            "printer": self.printer,
            "material": self.material,
            "rotation": self.rotation,
            "watertight": self.watertight,
            "winding_consistent": self.winding_consistent,
            "body_count": self.body_count,
            "volume_mm3": self.volume_mm3,
            "area_mm2": self.area_mm2,
            "size_mm": self.size_mm,
            "fits_bed": self.fits_bed,
            "overhang_area_mm2": self.overhang_area_mm2,
            "bridge_count": self.bridge_count,
            "bridge_area_mm2": self.bridge_area_mm2,
            "fillet_count": self.fillet_count,
            "fillet_area_mm2": self.fillet_area_mm2,
            "bed_contact_area_mm2": self.bed_contact_area_mm2,
            "min_wall_mm": self.min_wall_mm,
            "needs_supports": self.needs_supports,
            "estimate": asdict(self.estimate),
            "report": self.report.to_dict(),
        })


# -- geometry helpers (shared with piforge.fab.orient) ------------------------------------------
def _welded(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Copy of ``mesh`` with coincident vertices merged by position, as slicers do (face order kept).

    Bridge regions follow shared edges, so a triangle soup — an STL read with ``process=False``, a
    GLB split at sharp edges for shading — must be welded before :func:`_bridge_split`. Only the
    geometry is copied (no visuals or cached normals).
    """
    out = trimesh.Trimesh(vertices=np.array(mesh.vertices, dtype=float),
                          faces=np.array(mesh.faces, dtype=np.int64), process=False)
    out.merge_vertices(merge_tex=True, merge_norm=True)
    return out


def _face_tops(z: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Highest vertex of each face, measured from the lowest point of the mesh (the bed)."""
    zf = z[faces]
    return zf.max(axis=1) - zf.min()


def _overhang_from_arrays(normals: np.ndarray, face_tops: np.ndarray, max_overhang_deg: float,
                          bed_tol: float, angle_tol_deg: float) -> np.ndarray:
    limit_deg = min(float(max_overhang_deg) + float(angle_tol_deg), 90.0)
    # a face leaning α from vertical has a downward normal component of sin(α)
    return (-normals[:, 2] > math.sin(math.radians(limit_deg))) & (face_tops > bed_tol)


def _contact_area(normals: np.ndarray, areas: np.ndarray, face_tops: np.ndarray,
                  bed_tol: float = BED_TOL_MM) -> float:
    """Area of downward faces lying within ``bed_tol`` of the bed, projected onto the bed."""
    down = -normals[:, 2]
    on_bed = (face_tops <= bed_tol) & (down > 0.0)
    return float(np.dot(areas[on_bed], down[on_bed]))


def _fits_bed(size: Sequence[float], printer: PrinterProfile) -> bool:
    """Whether a part of bbox ``size`` fits; a quarter turn about Z (X↔Y) is allowed."""
    sx, sy, sz = (float(v) for v in size)
    bx, by, bz = printer.build_volume
    eps = 1e-6
    if sz > bz + eps:
        return False
    return (sx <= bx + eps and sy <= by + eps) or (sx <= by + eps and sy <= bx + eps)


def _small_contact(contact: float, total_area: float, height: float) -> bool:
    """PRINT.SMALL_CONTACT rule: contact < 1 % of the surface or height/√contact > 8."""
    if total_area <= 0.0:
        return False
    if contact < SMALL_CONTACT_FRACTION * total_area:
        return True
    return height > SMALL_CONTACT_ASPECT * math.sqrt(contact)


def _two_sided(inward: np.ndarray, mid: np.ndarray, max_gap: float) -> bool:
    """Do two support edges face each other across the region, at most ``max_gap`` apart?

    ``inward`` holds unit XY normals pointing from each support edge into the region, ``mid`` the
    edge midpoints. Walls on both sides of a roof, a ring around a ceiling or a three-sided pocket
    qualify; one wall (a cantilever, even if interrupted), a post the region wraps around (a
    mushroom cap) or supports facing each other across a wide gap (a ring lying on the bed) do not.
    """
    if len(inward) < 2:
        return False
    # up to 6 edges per 10° direction bin, spread from the back to the front of the bin
    angle = np.arctan2(inward[:, 1], inward[:, 0])
    bins = np.floor((angle + np.pi) / (2.0 * np.pi) * 36.0).astype(int) % 36
    order = np.lexsort((np.einsum("ij,ij->i", mid, inward), bins))
    sorted_bins = bins[order]
    starts = np.flatnonzero(np.r_[True, sorted_bins[1:] != sorted_bins[:-1]])
    stops = np.r_[starts[1:], len(order)]
    picks = np.unique(np.concatenate([np.linspace(lo, hi - 1, min(hi - lo, 6)).round()
                                      for lo, hi in zip(starts, stops)]).astype(int))
    n, m = inward[order[picks]], mid[order[picks]]
    ahead = np.einsum("ijk,ik->ij", m[None, :, :] - m[:, None, :], n)  # (m_j − m_i) · n_i
    gap_ok = (ahead > _FACING_MIN_MM) & (ahead <= max_gap + _SPAN_TOL_MM)
    return bool((gap_ok & gap_ok.T & (n @ n.T < -0.5)).any())


def _bridge_span(verts_xy: np.ndarray, tri_xy: np.ndarray, a: np.ndarray, b: np.ndarray,
                 limit: float) -> float | None:
    """2 × the largest XY distance from the region to its support segments ``a``–``b``.

    ``verts_xy`` are the region's vertices, ``tri_xy`` its triangles. Returns None as soon as a
    point lies farther than ``limit`` from every support (then it is no bridge).
    """
    seg = b - a
    length = np.linalg.norm(seg, axis=1)
    step = max(_SPAN_TOL_MM, float(length.sum()) / 100_000)  # ≤ ~100k support samples
    count = np.ceil(length / step).astype(np.int64) + 1
    owner = np.repeat(np.arange(len(a)), count)
    local = np.arange(int(count.sum())) - np.repeat(np.cumsum(count) - count, count)
    tree = cKDTree(a[owner] + (local / np.maximum(count - 1, 1)[owner])[:, None] * seg[owner])
    worst = 0.0
    for points in (verts_xy, np.einsum("sv,fvd->fsd", _SPAN_SAMPLES, tri_xy).reshape(-1, 2)):
        dist, _ = tree.query(points, distance_upper_bound=limit + step)  # vertices: quick reject
        if not np.isfinite(dist).all():
            return None
        worst = max(worst, float(dist.max()))
    return 2.0 * worst


def _bridge_split(mesh: trimesh.Trimesh, verts: np.ndarray, over: np.ndarray,
                  max_bridge_mm: float) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Find the overhang regions that print as bridges.

    ``verts`` are the mesh vertices in print orientation; ``over`` is the overhang face mask.
    Overhang faces sharing edges form regions. A region edge is a *support* when the neighbouring
    face drops below it (a wall under the roof). A region is a bridge when two supports face each
    other from opposite sides at most ``max_bridge_mm`` apart (:func:`_two_sided`) and its span —
    twice the largest XY distance from any point of the region to the nearest support — is at
    most ``max_bridge_mm`` (to within ``_SPAN_TOL_MM``).
    Returns (bridge face mask, [(area_mm2, span_mm) per bridge]).
    """
    n = len(mesh.faces)
    bridge = np.zeros(n, dtype=bool)
    adj = mesh.face_adjacency
    if not over.any() or len(adj) == 0 or max_bridge_mm <= 0.0:
        return bridge, []
    o0, o1 = over[adj[:, 0]], over[adj[:, 1]]
    inner = o0 & o1
    graph = coo_matrix((np.ones(int(inner.sum())), (adj[inner, 0], adj[inner, 1])), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    s0, s1 = o0 & ~o1, o1 & ~o0  # region boundary: exactly one face of the pair overhangs
    unshared = mesh.face_adjacency_unshared  # [:, k] = vertex of adj[:, k] off the shared edge
    edges = np.concatenate([mesh.face_adjacency_edges[s0], mesh.face_adjacency_edges[s1]])
    roof = np.concatenate([adj[s0, 0], adj[s1, 1]])
    far = np.concatenate([unshared[s0, 1], unshared[s1, 0]])
    support = verts[far, 2] < verts[edges, 2].mean(axis=1) - _SUPPORT_DROP_MM
    if not support.any():
        return bridge, []
    edges, roof = edges[support], roof[support]
    a, b = verts[edges[:, 0], :2], verts[edges[:, 1], :2]
    mid = (a + b) / 2.0
    length = np.linalg.norm(b - a, axis=1)
    inward = np.column_stack([a[:, 1] - b[:, 1], b[:, 0] - a[:, 0]])
    inward /= np.maximum(length, 1e-12)[:, None]
    toward_roof = verts[mesh.faces[roof], :2].mean(axis=1) - mid
    inward[np.einsum("ij,ij->i", inward, toward_roof) < 0.0] *= -1.0

    over_idx = np.nonzero(over)[0]
    face_order = np.argsort(labels[over_idx], kind="stable")
    face_labels = labels[over_idx][face_order]
    edge_region = labels[roof]
    edge_order = np.argsort(edge_region, kind="stable")
    regions, starts = np.unique(edge_region[edge_order], return_index=True)
    areas = mesh.area_faces
    found: list[tuple[float, float]] = []
    for region, start, stop in zip(regions, starts, [*starts[1:], len(edge_order)]):
        idx = edge_order[start:stop]
        real = idx[length[idx] > 1e-9]  # edges seen end-on in XY have no direction
        if not _two_sided(inward[real], mid[real], max_bridge_mm):
            continue
        lo, hi = np.searchsorted(face_labels, region), np.searchsorted(face_labels, region, "right")
        faces = over_idx[face_order[lo:hi]]
        tri = mesh.faces[faces]
        span = _bridge_span(verts[np.unique(tri), :2], verts[tri, :2], a[idx], b[idx],
                            max_bridge_mm / 2.0)
        if span is not None:
            bridge[faces] = True
            found.append((float(areas[faces].sum()), span))
    return bridge, found


def _fillet_limit(layer_h: float) -> float:
    """Largest height above the bed (and horizontal reach) of a bed fillet: max(1 mm, 4 layers)."""
    return max(BED_FILLET_MIN_MM, BED_FILLET_LAYERS * float(layer_h))


def _fillet_split(mesh: trimesh.Trimesh, verts: np.ndarray, over: np.ndarray,
                  limit_mm: float) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Find the overhang regions that are small fillets/chamfers lying on the bed.

    ``verts`` are the mesh vertices in print orientation, ``over`` the overhang face mask (bridges
    already removed). A region (faces sharing edges) is a bed fillet when every vertex is at most
    ``limit_mm`` above the bed and no point of it is farther than ``limit_mm / 2`` from the region's
    outline in XY (reach = 2 × that distance ≤ ``limit_mm``, so a lip as wide as it is tall at most).
    A wide ledge at the same height fails the reach test. Returns (fillet face mask,
    [(area_mm2, max_height_mm) per fillet]).
    """
    n = len(mesh.faces)
    fillet = np.zeros(n, dtype=bool)
    adj = mesh.face_adjacency
    if not over.any() or len(adj) == 0 or limit_mm <= 0.0:
        return fillet, []
    o0, o1 = over[adj[:, 0]], over[adj[:, 1]]
    inner = o0 & o1
    graph = coo_matrix((np.ones(int(inner.sum())), (adj[inner, 0], adj[inner, 1])), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    z = verts[:, 2] - verts[mesh.faces.ravel(), 2].min()
    face_top = z[mesh.faces].max(axis=1)
    over_idx = np.nonzero(over)[0]
    tops = np.zeros(labels.max() + 1)
    np.maximum.at(tops, labels[over_idx], face_top[over_idx])
    low = np.nonzero(tops <= limit_mm + _SPAN_TOL_MM)[0]
    if not len(low):
        return fillet, []
    edge_mask = o0 ^ o1  # region outline: exactly one face of the pair overhangs
    edges = np.concatenate([mesh.face_adjacency_edges[edge_mask & o0],
                            mesh.face_adjacency_edges[edge_mask & o1]])
    owner = np.concatenate([labels[adj[edge_mask & o0, 0]], labels[adj[edge_mask & o1, 1]]])
    areas = mesh.area_faces
    found: list[tuple[float, float]] = []
    for region in low:
        sel = edges[owner == region]
        faces = over_idx[labels[over_idx] == region]
        if not len(sel):
            continue
        a, b = verts[sel[:, 0], :2], verts[sel[:, 1], :2]
        tri = mesh.faces[faces]
        reach = _bridge_span(verts[np.unique(tri), :2], verts[tri, :2], a, b, limit_mm / 2.0)
        if reach is not None and reach <= limit_mm + _SPAN_TOL_MM:
            fillet[faces] = True
            found.append((float(areas[faces].sum()), float(tops[region])))
    return fillet, found


def _placement_metrics(mesh: trimesh.Trimesh, rotation: np.ndarray,
                       printer: PrinterProfile) -> dict[str, Any]:
    """Overhang (bridges excluded)/contact/height/fit of ``mesh`` rotated by ``rotation``.

    Works on rotated arrays — the mesh is not copied.
    """
    verts = mesh.vertices @ rotation.T
    normals = mesh.face_normals @ rotation.T
    corners = verts[mesh.faces]  # (n, 3, 3): only referenced vertices count
    lo = corners.reshape(-1, 3).min(axis=0)
    hi = corners.reshape(-1, 3).max(axis=0)
    tops = corners[:, :, 2].max(axis=1) - lo[2]
    mask = _overhang_from_arrays(normals, tops, printer.max_overhang_deg, BED_TOL_MM,
                                 OVERHANG_TOL_DEG)
    mask &= ~_bridge_split(mesh, verts, mask, printer.max_bridge_mm)[0]
    mask &= ~_fillet_split(mesh, verts, mask, _fillet_limit(printer.layer_h))[0]
    areas = mesh.area_faces
    size = tuple(float(v) for v in hi - lo)
    return {
        "overhang_area": float(areas[mask].sum()),
        "contact_area": _contact_area(normals, areas, tops),
        "height": size[2],
        "fits": _fits_bed(size, printer),
    }


def overhang_mask(mesh: trimesh.Trimesh, max_overhang_deg: float = 45.0,
                  bed_tol: float = BED_TOL_MM, *, angle_tol_deg: float = OVERHANG_TOL_DEG,
                  max_bridge_mm: float | None = None, layer_h: float = 0.2) -> np.ndarray:
    """Bool per face: faces leaning more than ``max_overhang_deg + angle_tol_deg`` from vertical.

    ``mesh`` must already be in print orientation (Z up); the bed is the plane through its lowest
    point, so it need not sit at z = 0. Faces whose vertices are all within ``bed_tol`` of the
    bed rest on it and are never overhangs. With ``max_bridge_mm`` (e.g. the printer's), regions
    that print as bridges up to that span are left out (see :func:`bridge_face_mask`), and so are
    small bed fillets (``PRINT.BED_FILLET``, sized by ``layer_h``); by default the mask is purely
    geometric.
    """
    mesh = _require_mesh(mesh)
    tops = _face_tops(mesh.vertices[:, 2], mesh.faces)
    mask = _overhang_from_arrays(mesh.face_normals, tops, max_overhang_deg, bed_tol, angle_tol_deg)
    if max_bridge_mm is not None and mask.any():
        welded = _welded(mesh)
        mask &= ~_bridge_split(welded, welded.vertices, mask, float(max_bridge_mm))[0]
        mask &= ~_fillet_split(welded, welded.vertices, mask, _fillet_limit(layer_h))[0]
    return mask


def bridge_face_mask(mesh: trimesh.Trimesh, max_overhang_deg: float = 45.0,
                     bed_tol: float = BED_TOL_MM, *, angle_tol_deg: float = OVERHANG_TOL_DEG,
                     max_bridge_mm: float | None = None) -> np.ndarray:
    """Bool per face: overhang faces that print as bridges (the part :func:`overhang_mask` drops).

    Same rule as :func:`analyze_mesh` (``PRINT.BRIDGE``): an overhang region held up by walls below
    it on two opposite sides, spanning at most ``max_bridge_mm`` — ``None`` means the generic
    printer's limit (20 mm). ``mesh`` must be in print orientation; it need not be welded.
    """
    mesh = _require_mesh(mesh)
    limit = get_printer("generic").max_bridge_mm if max_bridge_mm is None else float(max_bridge_mm)
    tops = _face_tops(mesh.vertices[:, 2], mesh.faces)
    steep = _overhang_from_arrays(mesh.face_normals, tops, max_overhang_deg, bed_tol, angle_tol_deg)
    if not steep.any():
        return steep
    welded = _welded(mesh)
    return _bridge_split(welded, welded.vertices, steep, limit)[0]


# -- wall thickness ------------------------------------------------------------------------------
def _ray_intersector(mesh: trimesh.Trimesh) -> Any:
    from trimesh import ray as tray

    if PREFER_EMBREE and tray.has_embree:
        from trimesh.ray import ray_pyembree

        return ray_pyembree.RayMeshIntersector(mesh)
    from trimesh.ray import ray_triangle

    logger.debug("ray casts use trimesh's numpy fallback (slow on large meshes)")
    return ray_triangle.RayMeshIntersector(mesh)


def _wall_rays(mesh: trimesh.Trimesh, n: int, seed: int,
               rays: dict | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample ``n`` surface points (area-weighted) and cast each one inward along −normal.

    Returns ``(face_index, thickness_mm, hit_face)``; thickness is NaN (and hit_face −1) when the
    ray escapes or first meets a face from outside (open mesh, overlapping shells) or leaves through
    a face more than 60° from opposite (it only cuts across a corner). If ``rays`` is
    a dict it receives the ray ``origins`` and ``directions``.
    """
    areas = mesh.area_faces
    total = float(areas.sum())
    n = int(n)
    if n <= 0 or total <= 0.0:
        if rays is not None:
            rays.update(origins=np.zeros((0, 3)), directions=np.zeros((0, 3)))
        return np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0, dtype=np.int64)
    rng = np.random.default_rng(seed)
    face_idx = rng.choice(len(areas), size=n, p=areas / total).astype(np.int64)
    uv = rng.random((n, 2))
    flip = uv.sum(axis=1) > 1.0
    uv[flip] = 1.0 - uv[flip]  # fold into the triangle → uniform barycentric samples
    tri = mesh.vertices[mesh.faces[face_idx]]
    points = tri[:, 0] + uv[:, :1] * (tri[:, 1] - tri[:, 0]) + uv[:, 1:] * (tri[:, 2] - tri[:, 0])
    directions = -mesh.face_normals[face_idx]
    # start slightly inside so the ray cannot re-hit its own face (Embree works in float32)
    inset = max(1e-4, 1e-5 * float(mesh.scale))
    origins = points + inset * directions
    hit = np.asarray(_ray_intersector(mesh).intersects_first(origins, directions), dtype=np.int64)

    thickness = np.full(n, np.nan)
    ok = np.nonzero(hit >= 0)[0]
    if len(ok):
        hit_normals = mesh.face_normals[hit[ok]]
        denom = np.einsum("ij,ij->i", directions[ok], hit_normals)
        # exits through an outward-facing face roughly opposite the entry face: a ray that starts
        # next to an edge and leaves through the adjacent side (> 60° off) measures the corner,
        # not a wall
        leaving = denom > _OPPOSITE_DOT
        anchor = mesh.vertices[mesh.faces[hit[ok], 0]]
        dist = np.einsum("ij,ij->i", anchor - origins[ok], hit_normals) / np.where(leaving, denom, 1.0)
        thickness[ok] = np.where(leaving & (dist >= 0.0), dist + inset, np.nan)
    hit_face = np.where(np.isfinite(thickness), hit, -1)
    if rays is not None:
        rays.update(origins=origins, directions=directions)
    return face_idx, thickness, hit_face


def _exit_dist(intersector: Any, origins: np.ndarray, dirs: np.ndarray) -> np.ndarray:
    """Distance from each origin (inside the part) to the first face along its ray; inf if none."""
    out = np.full(len(origins), np.inf)
    if not len(origins):
        return out
    _tri, idx, loc = intersector.intersects_id(origins, dirs, multiple_hits=False, return_locations=True)
    if len(idx):
        out[idx] = np.linalg.norm(loc - origins[idx], axis=1)
    return out


def _fine_detail(mesh: trimesh.Trimesh, mid: np.ndarray, d: np.ndarray, t: np.ndarray,
                 min_wall: float) -> np.ndarray:
    """True where the thin wall through ``mid`` (thickness ``t`` along ``d``) belongs to a feature at
    most :data:`FINE_DETAIL_MAX_H_MM` proud of a wider base (see the module notes)."""
    n = len(mid)
    fine = np.zeros(n, dtype=bool)
    if not n:
        return fine
    rt = _ray_intersector(mesh)
    ref = np.where(np.abs(d[:, 2:3]) < 0.9, np.array([[0.0, 0.0, 1.0]]), np.array([[1.0, 0.0, 0.0]]))
    u1 = ref - np.einsum("ij,ij->i", ref, d)[:, None] * d
    u1 /= np.linalg.norm(u1, axis=1, keepdims=True)
    u2 = np.cross(d, u1)
    h = FINE_DETAIL_MAX_H_MM
    wide = np.maximum(2.0 * t, min_wall + t)
    for w in (u1, -u1, u2, -u2):
        todo = np.nonzero(~fine)[0]
        if not len(todo):
            break
        top = _exit_dist(rt, mid[todo], w[todo])  # the free end of the feature this way
        back = _exit_dist(rt, mid[todo], -w[todo])  # the part ends the other way (no base)
        keep = top <= h
        todo, top, back = todo[keep], top[keep], back[keep]
        step = _FINE_STEP_MM
        while len(todo) and step <= h:
            alive = (top + step <= h) & (step < back)
            todo, top, back = todo[alive], top[alive], back[alive]
            if not len(todo):
                break
            q = mid[todo] - step * w[todo]
            width = _exit_dist(rt, q, d[todo]) + _exit_dist(rt, q, -d[todo])
            base = width >= wide[todo]
            fine[todo[base]] = True
            todo, top, back = todo[~base], top[~base], back[~base]
            step += _FINE_STEP_MM
    return fine


def wall_thickness_samples(mesh: trimesh.Trimesh, n: int = 1500,
                           seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Local wall thickness at ``n`` random surface points (area-weighted, reproducible by ``seed``).

    Each point is moved slightly inside the part and a ray is cast along the inward normal; the
    distance to the first face it leaves through is the wall thickness there. Returns
    ``(face_index, thickness_mm)``; thickness is NaN where no wall was found (open meshes).
    Uses Embree when available (``PREFER_EMBREE``), otherwise trimesh's slower numpy ray caster.
    """
    mesh = _require_mesh(mesh)
    face_idx, thickness, _ = _wall_rays(mesh, n, seed)
    return face_idx, thickness


# -- topology ------------------------------------------------------------------------------------
def _count_bodies(mesh: trimesh.Trimesh) -> int:
    """Connected components of faces that share an edge."""
    n = len(mesh.faces)
    adj = mesh.face_adjacency
    graph = coo_matrix((np.ones(len(adj)), (adj[:, 0], adj[:, 1])), shape=(n, n))
    count, _ = connected_components(graph, directed=False)
    return int(count)


def _edge_defects(mesh: trimesh.Trimesh) -> tuple[int, int]:
    """(open edges used by one face, non-manifold edges used by more than two faces)."""
    _, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return int((counts == 1).sum()), int((counts > 2).sum())


def _resolve_rotation(mesh: trimesh.Trimesh, rotation: RotationLike,
                      printer: PrinterProfile) -> np.ndarray:
    """None → identity; (rx, ry, rz) degrees; 3x3 proper rotation; ``"auto"`` → best_orientation."""
    if rotation is None:
        return np.eye(3)
    if isinstance(rotation, str):
        if rotation.strip().lower() == "auto":
            from piforge.fab.orient import best_orientation  # orient imports this module

            return np.array(best_orientation(mesh, printer)[0].rotation, dtype=float)
        raise ValidationError(f"rotation must be None, 'auto', (rx, ry, rz) degrees or a 3x3 "
                              f"matrix — got {rotation!r}")
    try:
        arr = np.asarray(rotation, dtype=float)
    except (TypeError, ValueError):
        raise ValidationError(f"rotation is not numeric: {rotation!r}") from None
    if arr.shape == (3,):
        return rotation_matrix(*arr)
    if arr.shape != (3, 3):
        raise ValidationError(f"rotation must be (rx, ry, rz) degrees or a 3x3 matrix, got shape "
                              f"{arr.shape}")
    if not np.allclose(arr @ arr.T, np.eye(3), atol=1e-6) or np.linalg.det(arr) < 0.0:
        raise ValidationError("rotation must be a proper rotation matrix (orthonormal, det = +1); "
                              "a mirror would print a different part")
    return arr.copy()


# -- main entry point ----------------------------------------------------------------------------
def analyze_mesh(
    mesh: trimesh.Trimesh,
    printer: str | PrinterProfile = "generic",
    material: str | Material = "PLA",
    *,
    name: str = "part",
    rotation: RotationLike = None,
    wall_samples: int = 1500,
    infill: float = 0.15,
) -> PrintAnalysis:
    """Check how printable ``mesh`` is on ``printer`` in ``material``.

    ``rotation`` selects the print orientation: ``None`` prints the mesh as modelled, a 3x3
    matrix or ``(rx, ry, rz)`` degrees (see :func:`~piforge.fab.meshutil.rotation_matrix`) rotates
    it first, ``"auto"`` uses :func:`piforge.fab.orient.best_orientation`. The input mesh is never
    modified; the analysis runs on a copy with merged vertices and, if needed, repaired normals.
    ``wall_samples`` ray casts measure wall thickness (0 skips the check).
    """
    started = time.perf_counter()
    mesh = _require_mesh(mesh)
    prof = get_printer(printer)
    mat = get_material(material)
    infill = _check_fraction("infill", infill)
    wall_samples = _check_count("wall_samples", wall_samples)
    report = Report(title=f"print:{name}")
    subject = f"part:{name}"

    prepared = mesh.copy()
    # weld coincident vertices by position only (as slicers do); keeps face order
    prepared.merge_vertices(merge_tex=True, merge_norm=True)
    watertight = bool(prepared.is_watertight)
    winding = bool(prepared.is_winding_consistent)
    bodies = _count_bodies(prepared)
    if not watertight:
        open_edges, nonmanifold = _edge_defects(prepared)
        report.add("PRINT.NOT_WATERTIGHT", Severity.ERROR,
                   f"Mesh is not watertight ({open_edges} open, {nonmanifold} non-manifold edges); "
                   "volume, wall thickness and slicing are unreliable.", subject,
                   hint="Fix the CAD model (failed boolean, missing or duplicate faces) before "
                        "printing.", open_edges=open_edges, nonmanifold_edges=nonmanifold)
    inverted = watertight and winding and float(prepared.volume) < 0.0
    if not winding or inverted:
        trimesh.repair.fix_normals(prepared, multibody=True)  # flips faces in place, same order
        what = "inconsistent (neighbouring faces disagree)" if not winding else \
            "inverted (normals point into the part)"
        report.add("PRINT.INCONSISTENT_WINDING", Severity.WARNING,
                   f"Face winding is {what}; analysed with repaired normals.", subject,
                   hint="Recompute normals/winding in the exporter; some slicers mis-fill such "
                        "meshes.", inverted=bool(inverted))
    if bodies > 1:
        report.add("PRINT.MULTIPLE_BODIES", Severity.INFO,
                   f"Mesh has {bodies} separate bodies; they will print as one job.", subject,
                   hint="Union touching bodies; keep intentional ones apart on the bed.",
                   bodies=bodies)

    rot = _resolve_rotation(prepared, rotation, prof)
    work = place_on_bed(prepared, rot)
    normals = work.face_normals
    areas = work.area_faces
    total_area = float(areas.sum())
    tops = _face_tops(work.vertices[:, 2], work.faces)
    size = tuple(float(v) for v in work.extents)
    height = size[2]

    fits = _fits_bed(size, prof)
    if not fits:
        bx, by, bz = prof.build_volume
        report.add("PRINT.TOO_BIG", Severity.ERROR,
                   f"Part is {size[0]:.1f} × {size[1]:.1f} × {size[2]:.1f} mm but the {prof.name} "
                   f"build volume is {bx:g} × {by:g} × {bz:g} mm.", subject,
                   hint="Try best_orientation(), split the part, or use a bigger printer.",
                   size_mm=list(size), build_volume_mm=list(prof.build_volume))

    steep = _overhang_from_arrays(normals, tops, prof.max_overhang_deg, BED_TOL_MM,
                                  OVERHANG_TOL_DEG)
    bridge_mask, bridges = _bridge_split(work, work.vertices, steep, prof.max_bridge_mm)
    fillet_mask, fillets = _fillet_split(work, work.vertices, steep & ~bridge_mask,
                                         _fillet_limit(prof.layer_h))
    over_mask = steep & ~bridge_mask & ~fillet_mask
    overhang = float(areas[over_mask].sum())
    needs_supports = overhang >= OVERHANG_WARN_MM2
    limit_deg = min(prof.max_overhang_deg + OVERHANG_TOL_DEG, 90.0)
    if overhang > 0.0:
        tail = "needs supports" if needs_supports else "small — usually prints without supports"
        report.add("PRINT.OVERHANG",
                   Severity.WARNING if needs_supports else Severity.INFO,
                   f"{overhang:.1f} mm² leans more than {limit_deg:g}° from vertical (printer "
                   f"limit {prof.max_overhang_deg:g}° + {OVERHANG_TOL_DEG:g}° tolerance) — {tail}.",
                   subject,
                   hint="Reorient (best_orientation), turn flat ceilings into 45° chamfers, or "
                        "enable supports.",
                   area_mm2=overhang, faces=int(over_mask.sum()),
                   max_overhang_deg=prof.max_overhang_deg, limit_deg=limit_deg)
    bridge_count = len(bridges)
    bridge_area = float(sum(area for area, _ in bridges))
    if bridges:
        span = max(s for _, s in bridges)
        one = bridge_count == 1
        report.add("PRINT.BRIDGE", Severity.INFO,
                   f"{bridge_count} bridge{'' if one else 's'} ({bridge_area:.1f} mm², longest span "
                   f"{span:.1f} mm; {prof.name} bridges up to {prof.max_bridge_mm:g} mm) "
                   f"print{'s' if one else ''} without supports.", subject,
                   hint="Keep bridged roofs flat and short; slicers print them with bridge "
                        "settings.",
                   count=bridge_count, max_span_mm=span, area_mm2=bridge_area,
                   max_bridge_mm=prof.max_bridge_mm)

    fillet_count = len(fillets)
    fillet_area = float(sum(area for area, _ in fillets))
    if fillets:
        limit = _fillet_limit(prof.layer_h)
        one = fillet_count == 1
        report.add("PRINT.BED_FILLET", Severity.INFO,
                   f"{fillet_count} rounded or chamfered bottom edge region{'' if one else 's'} "
                   f"({fillet_area:.1f} mm², up to {max(h for _, h in fillets):.2f} mm above the "
                   f"bed) print{'s' if one else ''} without supports (limit {limit:g} mm).", subject,
                   hint="Small bottom fillets and chamfers print fine; keep them under "
                        f"{limit:g} mm or use a 45° chamfer.",
                   count=fillet_count, area_mm2=fillet_area,
                   max_height_mm=max(h for _, h in fillets), limit_mm=limit)

    contact = _contact_area(normals, areas, tops)
    if _small_contact(contact, total_area, height):
        report.add("PRINT.SMALL_CONTACT", Severity.WARNING,
                   f"Only {contact:.1f} mm² ({100 * contact / total_area:.1f} % of the surface) "
                   f"touches the bed under a {height:.1f} mm tall part — it may detach or topple.",
                   subject, hint="Lay it on a larger flat face (best_orientation), add a brim or "
                                 "a flat base.",
                   contact_mm2=contact, height_mm=height, contact_fraction=contact / total_area)

    rays: dict = {}
    face_idx, thickness, hit_face = _wall_rays(work, wall_samples, seed=0, rays=rays)
    valid = np.isfinite(thickness)
    min_wall = float(thickness[valid].min()) if valid.any() else None
    thin = valid & (thickness < prof.min_wall - 1e-6)
    # thin strokes of low raised features (text, ribs) at least a nozzle wide: fine detail, INFO
    cand = np.nonzero(thin & (thickness >= prof.nozzle_d - 1e-6))[0]
    fine = np.zeros(len(thin), dtype=bool)
    if len(cand):
        dirs = rays["directions"][cand]
        mid = rays["origins"][cand] + dirs * (thickness[cand, None] / 2.0)
        fine[cand] = _fine_detail(work, mid, dirs, thickness[cand], prof.min_wall)
    if fine.any():
        fine_area = total_area * float(fine.sum()) / float(valid.sum())
        fine_min = float(thickness[fine].min())
        report.add("PRINT.FINE_DETAIL", Severity.INFO,
                   f"≈{fine_area:.0f} mm² of low raised detail (≤ {FINE_DETAIL_MAX_H_MM:g} mm tall: text, "
                   f"ribs) is {fine_min:.2f}–{prof.min_wall:g} mm thin — printed with single lines.",
                   subject, hint="Fine for labels and decoration; make load-bearing features "
                                 f"≥ {prof.min_wall:g} mm.",
                   min_wall_mm=fine_min, area_mm2=fine_area, max_height_mm=FINE_DETAIL_MAX_H_MM,
                   threshold_mm=prof.min_wall)
    thin &= ~fine
    if thin.any():
        min_wall_thin = float(thickness[thin].min())
    thin_mask = np.zeros(len(work.faces), dtype=bool)
    thin_mask[face_idx[thin]] = True
    thin_mask[hit_face[thin]] = True  # the opposite side of the same wall
    if thin.any() and min_wall is not None:
        thin_area = total_area * float(thin.sum()) / float(valid.sum())
        below_nozzle = min_wall_thin < prof.nozzle_d - 1e-6
        report.add("PRINT.THIN_WALL", Severity.ERROR if below_nozzle else Severity.WARNING,
                   f"Walls down to {min_wall_thin:.2f} mm (≈{thin_area:.0f} mm² thinner than the "
                   f"{prof.min_wall:g} mm minimum"
                   + (f"; below the {prof.nozzle_d:g} mm nozzle the slicer drops them)."
                      if below_nozzle else ")."), subject,
                   hint=f"Make walls ≥ {prof.min_wall:g} mm (two perimeters).",
                   min_wall_mm=min_wall_thin, area_mm2=thin_area, threshold_mm=prof.min_wall,
                   nozzle_mm=prof.nozzle_d)

    est = estimate_print(work, prof, mat, infill=infill)
    report.add("PRINT.ESTIMATE", Severity.INFO,
               f"≈{est.mass_g:.1f} g of {mat.name} ({est.filament_m:.2f} m), ≈{_fmt_hours(est.time_h)}, "
               f"≈{est.cost:.2f} {CURRENCY} on {prof.name} at {100 * infill:g} % infill.", subject,
               mass_g=est.mass_g, filament_m=est.filament_m, time_h=est.time_h, cost=est.cost,
               currency=CURRENCY, infill=infill)

    logger.debug("analyze_mesh(%s): %d faces in %.3f s", name, len(work.faces),
                 time.perf_counter() - started)
    return PrintAnalysis(
        name=name, printer=prof.name, material=mat.name, rotation=rot,
        watertight=watertight, winding_consistent=winding, body_count=bodies,
        volume_mm3=abs(float(work.volume)), area_mm2=total_area, size_mm=size, fits_bed=fits,
        overhang_area_mm2=overhang, overhang_face_mask=over_mask, bridge_face_mask=bridge_mask,
        bridge_count=bridge_count, bridge_area_mm2=bridge_area, bed_contact_area_mm2=contact,
        min_wall_mm=min_wall, thin_face_mask=thin_mask, needs_supports=needs_supports,
        estimate=est, report=report, fillet_count=fillet_count, fillet_area_mm2=fillet_area,
    )


def _fmt_hours(hours: float) -> str:
    minutes = round(hours * 60.0)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60:02d} min"
