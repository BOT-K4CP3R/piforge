"""Export: build123d → trimesh bridge, printable part files (STL/3MF/STEP) and GLB for the viewer.

:func:`to_trimesh` is the ONLY bridge from B-rep shapes to meshes (spec §5.3). Meshes and GLBs are
in millimetres with Z up; GLBs are written as-is (no glTF Y-up conversion, no metre scaling) so
they line up with the column-major matrices in ``scene.json`` (spec §5.4).
"""

from __future__ import annotations

import io
import logging
import os
import re
import json
import shutil
import struct
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np
import trimesh
from build123d import Location, Shape, export_step
from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_REVERSED
from OCP.TopLoc import TopLoc_Location

from piforge.core.errors import PiForgeError, ValidationError
from piforge.fab.meshutil import place_on_bed, rotation_matrix
from piforge.mech.part import PartSpec, normalize_color

log = logging.getLogger(__name__)

FORMATS = ("stl", "3mf", "step", "glb")


def _face_arrays(face: object) -> tuple[np.ndarray, np.ndarray] | None:
    """(vertices N×3, triangles M×3, 0-based) of one meshed face, or None if it has no mesh."""
    loc = TopLoc_Location()
    poly = BRep_Tool.Triangulation_s(face.wrapped, loc)  # type: ignore[attr-defined]
    if poly is None:
        return None
    trsf = loc.Transformation()
    n, m = poly.NbNodes(), poly.NbTriangles()
    pts = np.empty((n, 3))
    for i in range(1, n + 1):
        p = poly.Node(i).Transformed(trsf)
        pts[i - 1] = (p.X(), p.Y(), p.Z())
    tri = np.empty((m, 3), dtype=np.int64)
    for i in range(1, m + 1):
        tri[i - 1] = poly.Triangle(i).Get()
    tri -= 1
    if face.wrapped.Orientation() == TopAbs_REVERSED:  # type: ignore[attr-defined]
        tri = tri[:, [0, 2, 1]]
    return pts, tri


def to_trimesh(shape: object, tolerance: float = 0.05, angular_tolerance: float = 0.2) -> trimesh.Trimesh:
    """Tessellate a build123d shape into a watertight-when-closed ``trimesh.Trimesh`` (mm, Z up).

    ``tolerance`` is the chordal deflection in mm, ``angular_tolerance`` in radians. Shared edge
    vertices are merged, so closed solids give watertight meshes.
    """
    wrapped = getattr(shape, "_wrapped", None)  # .wrapped asserts on empty shapes
    if not isinstance(shape, Shape) or wrapped is None or wrapped.IsNull():
        raise PiForgeError(f"to_trimesh needs a non-empty build123d shape, got {shape!r}")
    if tolerance <= 0 or angular_tolerance <= 0:
        raise ValidationError("tolerance and angular_tolerance must be > 0")
    BRepMesh_IncrementalMesh(shape.wrapped, tolerance, False, angular_tolerance, True)
    verts, tris, offset = [], [], 0
    for face in shape.faces():
        arrays = _face_arrays(face)
        if arrays is None:
            log.debug("to_trimesh: a face has no triangulation (degenerate face skipped)")
            continue
        pts, tri = arrays
        verts.append(pts)
        tris.append(tri + offset)
        offset += len(pts)
    if not tris:
        raise PiForgeError("to_trimesh: the shape produced no triangles (empty or non-solid shape?)")
    mesh = trimesh.Trimesh(np.vstack(verts), np.vstack(tris), process=True)
    mesh.remove_unreferenced_vertices()
    return mesh


def _safe_name(name: str) -> str:
    """File-system friendly version of ``name`` (keeps letters incl. non-ASCII, digits, ._-)."""
    s = re.sub(r"[^\w.\-]+", "_", name.strip(), flags=re.UNICODE).strip("._")
    return s or "part"


def _atomic_write(path: Path, writer) -> Path:
    """Write via a temp file in the target directory, then rename (no half-written files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    try:
        writer(Path(tmp))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def _write_3mf(mesh: trimesh.Trimesh, path: Path, *, name: str = "part") -> None:
    """Minimal 3MF (core spec 2015/02) writer: one object, millimetres. No lxml needed."""
    v = mesh.vertices
    f = mesh.faces
    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
              ' <metadata name="Application">PiForge</metadata>\n'
              ' <resources>\n')
    title = re.sub(r'[<>&"]', "_", name)
    buf.write(f'  <object id="1" type="model" name="{title}">\n   <mesh>\n    <vertices>\n')
    buf.writelines(f'     <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>\n' for x, y, z in v)
    buf.write('    </vertices>\n    <triangles>\n')
    buf.writelines(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in f)
    buf.write('    </triangles>\n   </mesh>\n  </object>\n </resources>\n'
              ' <build>\n  <item objectid="1"/>\n </build>\n</model>\n')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" '
                     'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="model" '
                     'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", buf.getvalue())


def _write_step(shape: object, path: Path) -> None:
    """STEP via OCCT; written to an ASCII temp path first (OCCT + non-ASCII paths are fragile)."""
    fd, tmp = tempfile.mkstemp(suffix=".step")
    os.close(fd)
    try:
        if not export_step(shape, tmp):  # type: ignore[arg-type]
            raise PiForgeError(f"STEP export failed for {path.name}")
        shutil.copyfile(tmp, path)
    finally:
        os.unlink(tmp)


def _print_rotation(part: PartSpec, mesh: trimesh.Trimesh) -> np.ndarray:
    """3×3 rotation for printing: the part's own, else auto-orientation, else identity."""
    if part.print_rotation is not None:
        return rotation_matrix(*part.print_rotation)
    try:
        from piforge.fab.orient import best_orientation  # lazy: optional (Task 1)
    except ImportError as exc:
        log.warning("export_part(%s): auto-orientation unavailable (%s); using the design orientation",
                    part.name, exc)
        return np.eye(3)
    printer = part.meta.get("printer", "generic")
    best = best_orientation(mesh, printer)[0]
    log.info("export_part(%s): auto-orientation %s", part.name, getattr(best, "label", "?"))
    return np.asarray(best.rotation, dtype=float)


def export_part(part: PartSpec, out_dir: "str | os.PathLike", formats: Iterable[str] = ("stl", "3mf", "step"),
                *, for_print: bool = True) -> dict[str, Path]:
    """Write ``part`` to ``out_dir/<name>.<ext>`` for each format; returns {format: path}.

    With ``for_print`` the STL/3MF mesh is rotated by ``part.print_rotation`` (or auto-oriented via
    :func:`piforge.fab.orient.best_orientation` when it is None) and placed on the bed (min z = 0,
    centred in XY). STEP (and GLB) always keep the design frame. Files are named from the
    sanitised ``part.name`` and overwrite existing ones; to export several parts into one
    directory use :func:`export_parts`, which rejects names that map to the same file.
    """
    fmts = [str(f).lower().lstrip(".") for f in formats]
    bad = [f for f in fmts if f not in FORMATS]
    if bad:
        raise ValidationError(f"unknown export format(s) {bad}; choose from {FORMATS}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = _safe_name(part.name)
    written: dict[str, Path] = {}
    mesh: trimesh.Trimesh | None = None
    if {"stl", "3mf"} & set(fmts):
        mesh = to_trimesh(part.shape)
        if for_print:
            mesh = place_on_bed(mesh, _print_rotation(part, mesh))
    for fmt in fmts:
        path = out / f"{base}.{fmt}"
        if fmt == "stl":
            assert mesh is not None
            _atomic_write(path, lambda p, m=mesh: m.export(p, file_type="stl"))
        elif fmt == "3mf":
            assert mesh is not None
            _atomic_write(path, lambda p, m=mesh: _write_3mf(m, p, name=part.name))
        elif fmt == "step":
            _atomic_write(path, lambda p: _write_step(part.shape, p))
        else:
            export_glb(part.shape, path, color=part.color)
        written[fmt] = path
    log.debug("export_part(%s) → %s", part.name, {k: str(v) for k, v in written.items()})
    return written


def _srgb_to_linear(c: float) -> float:
    """sRGB transfer function decode (IEC 61966-2-1) of one channel in [0, 1]."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def export_parts(parts: Iterable[PartSpec], out_dir: "str | os.PathLike",
                 formats: Iterable[str] = ("stl", "3mf", "step"), *,
                 for_print: bool = True) -> dict[str, dict[str, Path]]:
    """:func:`export_part` for several parts into one directory; returns {part name: {format: path}}.

    File names come from the sanitised part names, so two parts whose names map to the same file
    (``"lid box"`` / ``"lid_box"``, or ``"Lid"`` / ``"lid"`` on case-insensitive disks) would
    overwrite each other: that raises :class:`ValidationError` before anything is written.
    """
    parts = list(parts)
    taken: dict[str, str] = {}
    for part in parts:
        key = _safe_name(part.name).lower()
        if key in taken:
            raise ValidationError(
                f"parts {taken[key]!r} and {part.name!r} would both be written as "
                f"'{_safe_name(part.name)}.*' — rename one of them")
        taken[key] = part.name
    return {part.name: export_part(part, out_dir, formats, for_print=for_print) for part in parts}


def _rgba(color: str) -> list[float]:
    """Linear RGBA floats for glTF ``baseColorFactor`` from an sRGB hex colour.

    glTF 2.0 defines ``baseColorFactor`` as LINEAR, while hex colours (``PartSpec.color``,
    ``scene.json`` ``color``) are sRGB — so each channel is decoded with the sRGB curve.
    """
    h = normalize_color(color)
    return [_srgb_to_linear(int(h[i:i + 2], 16) / 255.0) for i in (1, 3, 5)] + [1.0]


def _patch_glb_factors(data: bytes, factors: dict[str, list[float]]) -> bytes:
    """Rewrite each material's ``baseColorFactor`` in a GLB with exact floats (by material name).

    trimesh keeps PBR factors as uint8, which visibly shifts dark colours once they are linear
    (sRGB #050505 → linear 0.0015 → byte 0); patching the JSON chunk keeps full float precision.
    """
    magic, version, _length = struct.unpack_from("<4sII", data, 0)
    clen, ctype = struct.unpack_from("<I4s", data, 12)
    if magic != b"glTF" or ctype != b"JSON":
        raise PiForgeError("export_glb: unexpected GLB layout from trimesh")
    doc = json.loads(data[20:20 + clen])
    for mat in doc.get("materials", ()):
        factor = factors.get(mat.get("name", ""))
        if factor is not None:
            mat.setdefault("pbrMetallicRoughness", {})["baseColorFactor"] = [round(v, 7) for v in factor]
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    body += b" " * (-len(body) % 4)  # JSON chunk padded with spaces to 4 bytes (glTF 2.0 §4.4.3)
    rest = data[20 + clen:]
    return struct.pack("<4sII", magic, version, 12 + 8 + len(body) + len(rest)) + \
        struct.pack("<I4s", len(body), b"JSON") + body + rest


def _color_of(shape: object, default: str) -> str:
    col = getattr(shape, "color", None)
    if col is None:
        return default
    try:
        r, g, b, _a = tuple(col)
        return "#{:02x}{:02x}{:02x}".format(*(int(round(max(0.0, min(1.0, c)) * 255)) for c in (r, g, b)))
    except (TypeError, ValueError):
        return default


def _colored_leaves(shape: object, default: str, parent: Location | None = None,
                    label: str = "part") -> Iterator[tuple[str, object, str]]:
    """Yield (label, located shape, hex colour) for every leaf of a (nested) compound."""
    own = getattr(shape, "color", None)
    colour = _color_of(shape, default) if own is not None else default
    children = tuple(getattr(shape, "children", ()) or ())
    loc = shape.location if parent is None else parent * shape.location  # type: ignore[attr-defined]
    if children:
        for i, child in enumerate(children):
            name = getattr(child, "label", "") or f"{label}_{i}"
            yield from _colored_leaves(child, colour, loc, name)
        return
    located = shape if parent is None else shape.moved(parent)  # type: ignore[attr-defined]
    yield (getattr(shape, "label", "") or label), located, colour


def export_glb(shape: object, path: "str | os.PathLike", *, color: str = "#cccccc",
               tolerance: float = 0.1) -> Path:
    """Write ``shape`` as binary glTF (one PBR-coloured mesh per coloured leaf), mm, Z up.

    Compound children keep their own ``.color``; everything else gets ``color``. Vertices are split
    at sharp edges so viewers shade flat faces flat and curved faces smooth. Colours are sRGB hex;
    they are written as LINEAR float ``baseColorFactor`` as glTF 2.0 requires (three.js, Blender
    and other glTF readers then show the intended colour).
    """
    path = Path(path)
    scene = trimesh.Scene()
    factors: dict[str, list[float]] = {}
    for i, (name, leaf, hexcol) in enumerate(_colored_leaves(shape, normalize_color(color))):
        wrapped = getattr(leaf, "_wrapped", None)
        if wrapped is None or wrapped.IsNull() or not leaf.faces():  # type: ignore[attr-defined]
            continue
        mesh = to_trimesh(leaf, tolerance=tolerance).smooth_shaded
        mat_name = f"{_safe_name(name)}_{hexcol[1:]}"
        factors[mat_name] = _rgba(hexcol)
        mat = trimesh.visual.material.PBRMaterial(
            name=mat_name, baseColorFactor=factors[mat_name],
            metallicFactor=0.0, roughnessFactor=0.6)
        mesh.visual = trimesh.visual.TextureVisuals(material=mat)
        node = f"{_safe_name(name)}_{i}"
        scene.add_geometry(mesh, node_name=node, geom_name=node)
    if not scene.geometry:
        raise PiForgeError(f"export_glb: nothing to export for {path.name}")
    data = _patch_glb_factors(scene.export(file_type="glb", include_normals=True), factors)
    return _atomic_write(path, lambda p: p.write_bytes(data))
