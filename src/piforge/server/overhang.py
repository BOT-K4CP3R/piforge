"""Per-face overhang masks for the GUI heat-map, aligned with the GLB face order the browser sees.

Face order contract: triangles are listed scene node by scene node in glTF depth-first order
(``scene.nodes`` in order, a node's own mesh primitives before its children) — the order in which
three.js' ``GLTFLoader`` builds the object tree and ``Object3D.traverse`` visits it. trimesh's
``force="mesh"`` concatenation does NOT follow that order for multi-mesh GLBs, so the GLB is read
here directly.

Uses :func:`piforge.fab.analyze.overhang_mask`: the returned ``mask`` holds the faces that need
support — short supported spans (≤ the printer's ``max_bridge_mm``) print as *bridges* and are
excluded from it and listed in ``bridge_mask`` instead (spec §6.1). Without the fab package the same
geometric rule runs locally (no bridge detection): a face is an overhang when it faces down and
leans more than ``max_overhang_deg + 1°`` from vertical, excluding faces within ``bed_tol`` of the bed.
"""

from __future__ import annotations

import inspect
import json
import logging
import struct
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError(f"{path.name} is not a binary glTF (GLB) file")
    pos, doc, binary = 12, None, b""
    while pos + 8 <= len(data):
        length, kind = struct.unpack_from("<II", data, pos)
        chunk = data[pos + 8: pos + 8 + length]
        if kind == 0x4E4F534A:  # JSON
            doc = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942:  # BIN
            binary = chunk
        pos += 8 + length
    if doc is None:
        raise ValueError(f"{path.name} has no JSON chunk")
    return doc, binary


def _accessor(doc: dict, binary: bytes, index: int) -> np.ndarray:
    acc = doc["accessors"][index]
    dtype = np.dtype(_COMPONENT[acc["componentType"]])
    ncomp = _NCOMP[acc["type"]]
    count = int(acc["count"])
    if "bufferView" not in acc:  # all zeros by spec
        return np.zeros((count, ncomp), dtype=dtype)
    view = doc["bufferViews"][acc["bufferView"]]
    if view.get("buffer", 0) != 0:
        raise ValueError("external buffers are not supported in GLB meshes")
    start = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(view.get("byteStride", 0)) or dtype.itemsize * ncomp
    if stride == dtype.itemsize * ncomp:
        arr = np.frombuffer(binary, dtype=dtype, count=count * ncomp, offset=start)
        return arr.reshape(count, ncomp)
    rows = [np.frombuffer(binary, dtype=dtype, count=ncomp, offset=start + i * stride) for i in range(count)]
    return np.array(rows, dtype=dtype).reshape(count, ncomp)


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=float).reshape(4, 4).T  # column-major in glTF
    m = np.eye(4)
    if "rotation" in node:
        x, y, z, w = (float(v) for v in node["rotation"])
        m[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    if "scale" in node:
        m[:3, :3] = m[:3, :3] @ np.diag([float(v) for v in node["scale"]])
    if "translation" in node:
        m[:3, 3] = [float(v) for v in node["translation"]]
    return m


def glb_triangles(path: Path) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """(vertices (n,3), faces (m,3), faces per primitive) in GLTFLoader traversal order, node transforms applied."""
    doc, binary = _read_glb(Path(path))
    nodes = doc.get("nodes", [])
    scene = doc.get("scenes", [{}])[doc.get("scene", 0)] if doc.get("scenes") else {"nodes": list(range(len(nodes)))}
    verts: list[np.ndarray] = []
    faces: list[np.ndarray] = []
    counts: list[int] = []
    offset = 0

    def visit(index: int, parent: np.ndarray) -> None:
        nonlocal offset
        node = nodes[index]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            for prim in doc["meshes"][node["mesh"]].get("primitives", []):
                if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
                    continue  # GLTFLoader also turns these into lines/points, not triangles
                pos = _accessor(doc, binary, prim["attributes"]["POSITION"]).astype(float)
                idx = (_accessor(doc, binary, prim["indices"]).reshape(-1).astype(np.int64)
                       if "indices" in prim else np.arange(len(pos), dtype=np.int64))
                tri = idx[: len(idx) // 3 * 3].reshape(-1, 3)
                pos = (np.c_[pos, np.ones(len(pos))] @ world.T)[:, :3]
                verts.append(pos)
                faces.append(tri + offset)
                counts.append(len(tri))
                offset += len(pos)
        for child in node.get("children", []):
            visit(child, world)

    for root in scene.get("nodes", []):
        visit(root, np.eye(4))
    if not faces:
        raise ValueError(f"{Path(path).name} contains no triangles")
    return np.vstack(verts), np.vstack(faces), counts


def local_overhang_mask(mesh: Any, max_overhang_deg: float = 45.0, bed_tol: float = 0.05) -> np.ndarray:
    """Bool per face: down-facing beyond ``max_overhang_deg`` (+1° tolerance), not on the bed."""
    nz = np.asarray(mesh.face_normals)[:, 2]
    tri_z = np.asarray(mesh.vertices)[np.asarray(mesh.faces)][:, :, 2]
    bed = float(np.asarray(mesh.vertices)[:, 2].min()) if len(mesh.vertices) else 0.0
    on_bed = tri_z.max(axis=1) <= bed + bed_tol
    limit = np.sin(np.radians(min(max_overhang_deg + 1.0, 90.0)))
    return (-nz > limit) & ~on_bed


def rotation_from_part(entry: dict) -> tuple[np.ndarray | None, list[float] | None]:
    """Print rotation of a ``parts/index.json`` entry → (3x3 matrix | None, [rx, ry, rz] | None)."""
    from piforge.fab.meshutil import rotation_matrix

    rot = entry.get("print_rotation")
    if isinstance(rot, (list, tuple)) and len(rot) == 3:
        angles = [float(v) for v in rot]
        return rotation_matrix(*angles), angles
    mat = (entry.get("analysis") or {}).get("rotation")
    try:
        arr = np.asarray(mat, dtype=float)
        if arr.shape == (3, 3):
            return arr, None
    except (TypeError, ValueError):
        pass
    return None, None


def _fab_mask(fn: Any, mesh: Any, n: int, **kwargs: Any) -> np.ndarray | None:
    """``fn(mesh, **kwargs)`` as a bool array of ``n`` faces, or None (never a misaligned mask)."""
    mask = np.asarray(fn(mesh, **kwargs), dtype=bool).reshape(-1)
    if mask.shape != (n,):
        log.warning("fab.overhang_mask returned %s for %d faces; ignoring it", mask.shape, n)
        return None
    return mask


def compute_overhang(glb: Path, rotation: np.ndarray | None, max_overhang_deg: float,
                     max_bridge_mm: float | None = None, layer_h: float = 0.2) -> dict:
    """Overhang face mask (bridges and bed fillets excluded) and bridge face mask of ``glb`` in print orientation.

    ``mask[i]`` — face ``i`` needs support; ``bridge_mask[i]`` — face ``i`` overhangs but spans at
    most ``max_bridge_mm`` between two supports, so it prints as a bridge (spec §6.1).
    ``fillet_mask[i]`` — small near-bed fillet/chamfer (``PRINT.BED_FILLET``), also not an overhang.
    ``overhang_count``/``bridge_count`` count faces (triangles), not regions.
    """
    import trimesh

    from piforge.fab.meshutil import place_on_bed

    vertices, faces, counts = glb_triangles(glb)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    placed = place_on_bed(mesh, rotation)
    # GLB exporters split vertices along sharp edges (flat shading). Bridge detection walks face
    # adjacency, so weld coincident vertices first; faces keep their count and order.
    placed.merge_vertices(merge_tex=True, merge_norm=True)
    n = len(placed.faces)
    try:
        from piforge.fab import analyze as fab_analyze
    except ImportError:
        fab_analyze = None
    fn = getattr(fab_analyze, "overhang_mask", None)
    geometric = mask = bridge = None
    bridges_excluded = False
    if fn is not None:
        geometric = _fab_mask(fn, placed, n, max_overhang_deg=max_overhang_deg)
        if geometric is not None and max_bridge_mm and "max_bridge_mm" in inspect.signature(fn).parameters:
            try:
                kw = {"max_overhang_deg": max_overhang_deg, "max_bridge_mm": float(max_bridge_mm)}
                if "layer_h" in inspect.signature(fn).parameters:
                    kw["layer_h"] = float(layer_h)
                mask = _fab_mask(fn, placed, n, **kw)
                bridge_fn = getattr(fab_analyze, "bridge_face_mask", None)
                if mask is not None and bridge_fn is not None:
                    bridge = _fab_mask(bridge_fn, placed, n, max_overhang_deg=max_overhang_deg,
                                       max_bridge_mm=float(max_bridge_mm))
            except Exception as exc:  # noqa: BLE001 — fall back to the plain geometric mask
                log.warning("bridge detection failed for %s: %s", Path(glb).name, exc)
                mask = bridge = None
            bridges_excluded = mask is not None and bridge is not None
        if not bridges_excluded:
            mask, bridge = geometric, None
    method = "fab"
    if mask is None:
        mask, method = local_overhang_mask(placed, max_overhang_deg), "local"
    zeros = np.zeros(n, dtype=bool)
    if bridges_excluded:
        bridge = bridge & geometric
        fillet = geometric & ~mask & ~bridge
    else:
        bridge, fillet = zeros, zeros
    areas = np.asarray(placed.area_faces)
    return {"face_count": int(n), "mesh_face_counts": counts, "mask": [bool(v) for v in mask],
            "overhang_count": int(mask.sum()), "overhang_area_mm2": float(areas[mask].sum()),
            "bridge_mask": [bool(v) for v in bridge], "bridge_count": int(bridge.sum()),
            "bridge_area_mm2": float(areas[bridge].sum()),
            "fillet_mask": [bool(v) for v in fillet], "fillet_count": int(fillet.sum()),
            "fillet_area_mm2": float(areas[fillet].sum()), "bridges_excluded": bridges_excluded,
            "max_bridge_mm": float(max_bridge_mm) if max_bridge_mm else None,
            "method": method, "size_mm": [float(v) for v in placed.extents]}
