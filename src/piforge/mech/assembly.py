"""Assemblies: placed parts with a parent/child hierarchy and joints, interference checks, scene export.

Transform chain (binding, also used by the web viewer)::

    world(node) = world(parent) · loc · J(value)
    J = T(origin) · R(axis, value°) · T(−origin)   revolute (origin and axis in the node frame)
    J = T(axis · value mm)                           prismatic

``scene.json`` (spec §5.4) stores per node ``matrix`` = ``loc`` (local to the parent, rest pose,
WITHOUT the joint motion) as 16 floats column-major; the viewer applies the joint on top using the
``joint`` record. ``world_matrix`` (extra, informative) is the world transform at the current
joint values. Meshes are GLB files in the part's own frame, mm, Z up.

Harness wires (:mod:`piforge.mech.harness`) are nodes with ``kind: "wire"``, an identity matrix (the
tube mesh is in the world frame), ``color`` = insulation colour and a ``wire`` record; the top-level
``connectors`` list holds every wire contact point (``id``, ``ref``, ``pin``, ``label``, ``node``,
world ``pos`` and exit ``dir``). Both are empty/absent for assemblies without a harness.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from build123d import Location
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Trsf

from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.core.report import Report, Severity
from piforge.fab.meshutil import rotation_matrix
from piforge.mech.part import PartSpec

log = logging.getLogger(__name__)

JOINT_TYPES = ("revolute", "prismatic")
Vec3 = tuple[float, float, float]


class DuplicateIdError(ValidationError):
    """An assembly node id is already in use."""


def _vec3(value: Any, what: str) -> Vec3:
    try:
        vals = tuple(float(v) for v in value)
    except TypeError:
        raise ValidationError(f"{what} must be three numbers, got {value!r}") from None
    if len(vals) != 3 or not all(math.isfinite(v) for v in vals):
        raise ValidationError(f"{what} must be three finite numbers, got {value!r}")
    return vals  # type: ignore[return-value]


def _translation(v: Iterable[float]) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = list(v)
    return m


def _axis_rotation(axis: Vec3, angle_deg: float) -> np.ndarray:
    """4×4 rotation by ``angle_deg`` about the unit ``axis`` through the origin (Rodrigues)."""
    x, y, z = axis
    a = math.radians(angle_deg)
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    m = np.eye(4)
    m[:3, :3] = [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                 [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                 [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]
    return m


def location_to_matrix(loc: Location) -> np.ndarray:
    """4×4 homogeneous matrix (row-major numpy) of a build123d Location."""
    t = loc.wrapped.Transformation()
    m = np.eye(4)
    for r in range(3):
        for c in range(4):
            m[r, c] = t.Value(r + 1, c + 1)
    return m


def matrix_to_location(m: np.ndarray) -> Location:
    """build123d Location from a rigid 4×4 matrix."""
    trsf = gp_Trsf()
    trsf.SetValues(*(float(m[r, c]) for r in range(3) for c in range(4)))
    return Location(trsf)


def to_location(loc: "Location | Iterable[float] | None") -> Location:
    """Accept a Location, (x, y, z) or (x, y, z, rx, ry, rz) — degrees, fixed X then Y then Z.

    Tuple rotations are EXTRINSIC (about the fixed world/parent axes: first X, then Y, then Z;
    R = Rz·Ry·Rx, as :func:`piforge.fab.meshutil.rotation_matrix` and ``PartSpec.print_rotation``).
    A build123d ``Location((x, y, z), (rx, ry, rz))`` passed as-is keeps build123d's own INTRINSIC
    convention, so the same three angles give a different orientation whenever two are non-zero.
    """
    if loc is None:
        return Location()
    if isinstance(loc, Location):
        return loc
    try:
        vals = [float(v) for v in loc]  # type: ignore[union-attr]
    except TypeError:
        raise ValidationError(f"location must be a Location or a tuple of 3 or 6 numbers, got {loc!r}") from None
    if len(vals) == 3:
        return Location(tuple(vals))
    if len(vals) == 6:
        m = np.eye(4)
        m[:3, :3] = rotation_matrix(*vals[3:])
        m[:3, 3] = vals[:3]
        return matrix_to_location(m)
    raise ValidationError(f"location tuple needs 3 (x, y, z) or 6 (x, y, z, rx, ry, rz) numbers, got {loc!r}")


@dataclass
class Joint:
    """A single-axis joint between a node and its parent (``value`` in degrees or mm)."""

    type: str
    axis: Vec3
    origin: Vec3 = (0.0, 0.0, 0.0)
    min: float = -180.0
    max: float = 180.0
    value: float = 0.0
    driven_by: dict | None = None  # {"device": "SERVO1", "prop": "angle", "scale": 1.0, "offset": 0.0}

    def __post_init__(self) -> None:
        if self.type not in JOINT_TYPES:
            raise ValidationError(f"joint type must be one of {JOINT_TYPES}, got {self.type!r}")
        axis = np.array(_vec3(self.axis, "joint axis"))
        n = float(np.linalg.norm(axis))
        if n < 1e-12:
            raise ValidationError("joint axis must not be the zero vector")
        self.axis = tuple(float(v) for v in axis / n)  # type: ignore[assignment]
        self.origin = _vec3(self.origin, "joint origin")
        self.min, self.max, self.value = float(self.min), float(self.max), float(self.value)
        if self.min > self.max:
            raise ValidationError(f"joint min {self.min} > max {self.max}")
        self.check(self.value)
        if self.driven_by is not None:
            if not isinstance(self.driven_by, dict) or not {"device", "prop"} <= set(self.driven_by):
                raise ValidationError("driven_by must be a dict with at least 'device' and 'prop'")
            self.driven_by = dict(self.driven_by)

    def check(self, value: float) -> float:
        """Return ``value`` as float if it lies within [min, max], else raise ValidationError."""
        v = float(value)
        if not (self.min - 1e-9 <= v <= self.max + 1e-9):
            unit = "°" if self.type == "revolute" else " mm"
            raise ValidationError(f"joint value {v}{unit} outside [{self.min}, {self.max}]")
        return v

    def matrix(self, value: float | None = None) -> np.ndarray:
        """4×4 joint motion J(value) in the node frame (``value`` defaults to ``self.value``).

        Both an explicit ``value`` and the stored ``self.value`` (which callers may reassign after
        construction) are range-checked: outside [min, max] raises :class:`ValidationError` rather
        than silently clamping, so an impossible pose never reaches a check or the scene.
        """
        v = self.check(self.value if value is None else value)
        if self.type == "revolute":
            return _translation(self.origin) @ _axis_rotation(self.axis, v) @ _translation(
                [-c for c in self.origin])
        return _translation([a * v for a in self.axis])

    def to_dict(self) -> dict:
        """JSON-ready record as used in scene.json (spec §5.4)."""
        return {"type": self.type, "axis": list(self.axis), "origin": list(self.origin),
                "min": self.min, "max": self.max, "value": self.value,
                "driven_by": dict(self.driven_by) if self.driven_by is not None else None}


@dataclass
class Node:
    """One placed part. ``loc`` is relative to ``parent`` (or the world)."""

    id: str
    part: PartSpec
    loc: Location
    parent: str | None = None
    joint: Joint | None = None
    explode: Vec3 | None = None  # offset (mm) applied at full explode
    emissive_from: dict | None = None  # {"device": "D1", "prop": "brightness", "color": "#ff2200"}
    display_from: dict | None = None  # {"device": "F1", "kind": "splitflap"} — GUI draws a live display face
    wire: dict | None = None  # harness wire record (kind "wire" nodes, see piforge.mech.harness)


DISPLAY_KINDS = ("splitflap",)


def _common_volume(a: object, b: object) -> float:
    """Volume (mm³) of the boolean common of two located shapes."""
    op = BRepAlgoAPI_Common(a.wrapped, b.wrapped)  # type: ignore[attr-defined]
    if not op.IsDone():
        raise PiForgeError("boolean common failed")
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(op.Shape(), props)
    return abs(props.Mass())


def _bbox(shape: object) -> tuple[np.ndarray, np.ndarray]:
    bb = shape.bounding_box()  # type: ignore[attr-defined]
    return np.array([bb.min.X, bb.min.Y, bb.min.Z]), np.array([bb.max.X, bb.max.Y, bb.max.Z])


def _boxes_overlap(a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray],
                   tol: float = 1e-6) -> bool:
    return bool(np.all(a[0] < b[1] - tol) and np.all(b[0] < a[1] - tol))


def _fmt(values: list[float], unit: str) -> str:
    return ", ".join(f"{v:g}{unit}" for v in values)


def _pairs(ignore: Iterable[Iterable[str]] | None) -> set[frozenset[str]]:
    """Normalise ``ignore`` (frozensets, tuples or lists of two node ids) to a set of frozensets."""
    out: set[frozenset[str]] = set()
    for pair in ignore or ():
        if isinstance(pair, str):
            raise ValidationError(f"ignore entries must be pairs of node ids, got {pair!r}")
        fs = frozenset(pair)
        if len(fs) != 2:
            raise ValidationError(f"ignore entries must name two different nodes, got {pair!r}")
        out.add(fs)
    return out


class Assembly:
    """A named tree of placed :class:`PartSpec` nodes with optional joints."""

    def __init__(self, name: str):
        if not str(name).strip():
            raise ValidationError("Assembly name must not be empty")
        self.name = str(name)
        self._nodes: dict[str, Node] = {}
        self.connectors: list[dict] = []  # wire contact points (scene.json "connectors"), see harness

    # -- building ------------------------------------------------------------------------------
    def add(self, part: PartSpec, loc: "Location | Iterable[float] | None" = None, *, id: str | None = None,
            parent: str | None = None, joint: "Joint | dict | None" = None,
            explode: Iterable[float] | None = None, emissive_from: dict | None = None,
            display_from: dict | None = None, wire: dict | None = None) -> str:
        """Place ``part`` at ``loc`` (relative to ``parent``); returns the node id.

        ``loc``: Location | (x, y, z) | (x, y, z, rx, ry, rz) in mm/degrees.

        Rotation conventions differ by type — mind this when mixing them:

        * tuple ``(x, y, z, rx, ry, rz)``: rotations about the FIXED parent axes, X first, then Y,
          then Z (extrinsic, R = Rz·Ry·Rx; same as ``fab.meshutil.rotation_matrix`` and
          ``PartSpec.print_rotation``);
        * build123d ``Location((x, y, z), (rx, ry, rz))``: used unchanged, i.e. build123d's
          INTRINSIC convention (each rotation about the already-rotated axes).

        They agree when at most one angle is non-zero.

        ``display_from`` (e.g. ``{"device": "F1", "kind": "splitflap"}``) makes the web viewer draw a
        live display on this node, fed by the twin device's state; for ``"splitflap"`` the node's
        bounding box is the window face (a two-half flap with the current digit, animated flips).

        ``wire`` is the harness record of a ``kind="wire"`` node (written to scene.json as ``wire``).

        ``id`` defaults to the
        part name (suffixed ``_2``, ``_3``… when taken); an explicit duplicate id raises
        :class:`DuplicateIdError`.
        """
        if not isinstance(part, PartSpec):
            raise ValidationError(f"Assembly.add expects a PartSpec, got {type(part).__name__}")
        if id is None:
            base, n, id = part.name, 2, part.name
            while id in self._nodes:
                id, n = f"{base}_{n}", n + 1
        elif not str(id).strip():
            raise ValidationError("node id must not be empty")
        elif id in self._nodes:
            raise DuplicateIdError(f"node id {id!r} already exists in assembly {self.name!r}")
        if parent is not None and parent not in self._nodes:
            raise NotFoundError("assembly node", parent, self._nodes)
        if isinstance(joint, dict):
            joint = Joint(**joint)
        if joint is not None and not isinstance(joint, Joint):
            raise ValidationError(f"joint must be a Joint or dict, got {type(joint).__name__}")
        if emissive_from is not None and (not isinstance(emissive_from, dict)
                                          or not {"device", "prop"} <= set(emissive_from)):
            raise ValidationError("emissive_from must be a dict with at least 'device' and 'prop'")
        if display_from is not None:
            if not isinstance(display_from, dict) or not {"device", "kind"} <= set(display_from):
                raise ValidationError("display_from must be a dict with at least 'device' and 'kind'")
            if display_from["kind"] not in DISPLAY_KINDS:
                raise ValidationError(f"display_from kind must be one of {DISPLAY_KINDS}, "
                                      f"got {display_from['kind']!r}")
        node = Node(id=str(id), part=part, loc=to_location(loc), parent=parent, joint=joint,
                    explode=_vec3(explode, "explode") if explode is not None else None,
                    emissive_from=dict(emissive_from) if emissive_from is not None else None,
                    display_from=dict(display_from) if display_from is not None else None,
                    wire=dict(wire) if wire is not None else None)
        self._nodes[node.id] = node
        return node.id

    def node(self, id: str) -> Node:
        """Return the node ``id``; NotFoundError lists close ids."""
        try:
            return self._nodes[id]
        except KeyError:
            raise NotFoundError("assembly node", id, self._nodes) from None

    @property
    def nodes(self) -> list[Node]:
        """Nodes in insertion order (parents always before their children)."""
        return list(self._nodes.values())

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, id: object) -> bool:
        return id in self._nodes

    def descendants(self, id: str) -> set[str]:
        """Ids of every node below ``id`` in the hierarchy."""
        self.node(id)
        out: set[str] = set()
        for n in self._nodes.values():  # insertion order ⇒ parents seen first
            if n.parent == id or n.parent in out:
                out.add(n.id)
        return out

    # -- kinematics ----------------------------------------------------------------------------
    def _joint_values(self, joint_values: dict[str, float] | None) -> dict[str, float]:
        vals: dict[str, float] = {}
        for k, v in (joint_values or {}).items():
            node = self.node(k)
            if node.joint is None:
                raise ValidationError(f"node {k!r} has no joint")
            vals[k] = node.joint.check(v)
        return vals

    def _world_matrix(self, id: str, values: dict[str, float], cache: dict[str, np.ndarray]) -> np.ndarray:
        if id in cache:
            return cache[id]
        node = self.node(id)
        m = location_to_matrix(node.loc)
        if node.joint is not None:
            m = m @ node.joint.matrix(values.get(id))
        if node.parent is not None:
            m = self._world_matrix(node.parent, values, cache) @ m
        cache[id] = m
        return m

    def world_matrix(self, id: str, joint_values: dict[str, float] | None = None) -> np.ndarray:
        """4×4 world transform (row-major numpy) of node ``id`` at the given joint values."""
        return self._world_matrix(id, self._joint_values(joint_values), {})

    def world_location(self, id: str, joint_values: dict[str, float] | None = None) -> Location:
        """World Location of node ``id``; ``joint_values`` maps node id → joint value."""
        return matrix_to_location(self.world_matrix(id, joint_values))

    def world_shape(self, id: str, joint_values: dict[str, float] | None = None) -> object:
        """The node's part shape moved to its world placement."""
        return self.node(id).part.shape.moved(self.world_location(id, joint_values))  # type: ignore[attr-defined]

    def _world_shapes(self, ids: Iterable[str], values: dict[str, float]) -> dict[str, object]:
        cache: dict[str, np.ndarray] = {}
        return {i: self.node(i).part.shape.moved(  # type: ignore[attr-defined]
            matrix_to_location(self._world_matrix(i, values, cache))) for i in ids}

    def bounds(self, joint_values: dict[str, float] | None = None) -> tuple[Vec3, Vec3]:
        """World AABB of all nodes ((xmin, ymin, zmin), (xmax, ymax, zmax)); zeros when empty."""
        if not self._nodes:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        shapes = self._world_shapes(self._nodes, self._joint_values(joint_values))
        boxes = [_bbox(s) for s in shapes.values()]
        lo = np.min([b[0] for b in boxes], axis=0)
        hi = np.max([b[1] for b in boxes], axis=0)
        return tuple(float(v) for v in lo), tuple(float(v) for v in hi)  # type: ignore[return-value]

    # -- checks --------------------------------------------------------------------------------
    def check_interference(self, *, ignore: Iterable[Iterable[str]] | None = None, min_volume: float = 0.5,
                           joint_values: dict[str, float] | None = None,
                           kinds: "Iterable[str] | str | None" = None) -> Report:
        """Pairwise solid overlap check (bbox prefilter + OCC common volume).

        Emits ``ASM.INTERFERENCE`` (ERROR, data ``volume_mm3``) for every pair overlapping by more
        than ``min_volume`` mm³; touching faces are fine. ``ignore`` holds id pairs (frozensets or
        tuples), ``kinds`` restricts the check to parts of those kinds (e.g. ``{"printed", "pcb"}``;
        default: every kind except ``wire``).
        """
        rep = Report(title=f"interference: {self.name}")
        ignore = _pairs(ignore)
        kind_set = ({kinds} if isinstance(kinds, str) else set(kinds)) if kinds is not None else None
        # harness wires are checked by Harness.checks (WIRE.COLLISION), not here, unless asked for
        ids = [n.id for n in self._nodes.values()
               if (n.part.kind != "wire" if kind_set is None else n.part.kind in kind_set)]
        shapes = self._world_shapes(ids, self._joint_values(joint_values))
        boxes = {i: _bbox(s) for i, s in shapes.items()}
        n_bool = 0
        for ai, a in enumerate(ids):
            for b in ids[ai + 1:]:
                if frozenset((a, b)) in ignore or not _boxes_overlap(boxes[a], boxes[b]):
                    continue
                n_bool += 1
                try:
                    vol = _common_volume(shapes[a], shapes[b])
                except Exception as exc:  # noqa: BLE001 - kernel failure must not abort a check
                    rep.add("ASM.CHECK_FAILED", Severity.WARNING,
                            f"Could not intersect {a} and {b}: {exc}", subject=f"node:{a}/{b}", a=a, b=b)
                    continue
                if vol > min_volume:
                    rep.add("ASM.INTERFERENCE", Severity.ERROR,
                            f"{a} and {b} overlap by {vol:.1f} mm³.", subject=f"node:{a}/{b}",
                            hint="Move or resize one of the parts, or add the pair to ignore= if intended.",
                            a=a, b=b, volume_mm3=round(vol, 4))
        rep.add("ASM.CHECKED", Severity.INFO,
                f"Checked {len(ids)} part(s): {n_bool} pair(s) with overlapping bounding boxes.",
                parts=len(ids), boolean_pairs=n_bool)
        return rep

    def sweep_joint(self, joint_id: str, *, steps: int = 13, ignore: Iterable[Iterable[str]] | None = None,
                    min_volume: float = 0.5) -> Report:
        """Move joint ``joint_id`` through ``steps`` values in [min, max] and check collisions.

        Only pairs between the moving subtree (the node and its descendants) and the rest are
        checked. Emits ``ASM.JOINT_COLLISION`` (ERROR) per colliding pair with the list of colliding
        joint values in ``data['angles']`` (degrees, or mm for prismatic joints), or
        ``ASM.JOINT_OK`` (INFO) when the full range is free.
        """
        node = self.node(joint_id)
        if node.joint is None:
            raise ValidationError(f"node {joint_id!r} has no joint to sweep")
        if steps < 2:
            raise ValidationError("steps must be >= 2")
        j = node.joint
        unit = "°" if j.type == "revolute" else " mm"
        rep = Report(title=f"joint sweep: {joint_id}")
        ignore = _pairs(ignore)
        moving = {joint_id} | self.descendants(joint_id)
        static = [i for i in self._nodes if i not in moving]
        static_shapes = self._world_shapes(static, {})
        static_boxes = {i: _bbox(s) for i, s in static_shapes.items()}
        hits: dict[tuple[str, str], list[float]] = {}
        worst: dict[tuple[str, str], float] = {}
        values = [float(v) for v in np.linspace(j.min, j.max, steps)]
        for v in values:
            mshapes = self._world_shapes(sorted(moving), {joint_id: v})
            for m, ms in mshapes.items():
                mbox = _bbox(ms)
                for s in static:
                    if frozenset((m, s)) in ignore or not _boxes_overlap(mbox, static_boxes[s]):
                        continue
                    try:
                        vol = _common_volume(ms, static_shapes[s])
                    except Exception as exc:  # noqa: BLE001
                        rep.add("ASM.CHECK_FAILED", Severity.WARNING,
                                f"Could not intersect {m} and {s} at {v:g}{unit}: {exc}", subject=f"joint:{joint_id}")
                        continue
                    if vol > min_volume:
                        hits.setdefault((m, s), []).append(round(v, 6))
                        worst[(m, s)] = max(worst.get((m, s), 0.0), vol)
        for (m, s), vals in hits.items():
            rep.add("ASM.JOINT_COLLISION", Severity.ERROR,
                    f"Joint {joint_id} ({j.type}): {m} hits {s} at {_fmt(vals, unit)} "
                    f"(max overlap {worst[(m, s)]:.1f} mm³).", subject=f"joint:{joint_id}",
                    hint="Limit the joint range (min/max) or move the obstacle.",
                    node=m, other=s, angles=vals, max_volume_mm3=round(worst[(m, s)], 4),
                    unit=unit.strip())
        if not hits:
            rep.add("ASM.JOINT_OK", Severity.INFO,
                    f"Joint {joint_id} moves freely over [{j.min:g}, {j.max:g}]{unit} ({steps} steps).",
                    subject=f"joint:{joint_id}", steps=steps, min=j.min, max=j.max)
        return rep

    # -- export --------------------------------------------------------------------------------
    def to_scene(self, out_dir: "str | os.PathLike", *, tolerance: float = 0.1) -> Path:
        """Write ``out_dir/scene.json`` (spec §5.4) and ``out_dir/meshes/*.glb`` (one per unique part).

        ``out_dir/meshes`` is owned by this method: ``*.glb`` files there that the new scene does not
        reference (left over from an earlier build) are deleted; other files are left alone. Node
        ids that sanitise to the same file name (case-insensitively) get ``_2``, ``_3``… suffixes.
        """
        from piforge.mech.export import export_glb

        out = Path(out_dir)
        mesh_dir = out / "meshes"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        mesh_of: dict[int, str] = {}
        used: set[str] = set()
        records = []
        for node in self._nodes.values():
            key = id(node.part)
            if key not in mesh_of:
                stem = re.sub(r"[^\w.\-]+", "_", node.id, flags=re.UNICODE).strip("._") or "node"
                name, n = stem, 2
                while name.lower() in used:
                    name, n = f"{stem}_{n}", n + 1
                used.add(name.lower())
                export_glb(node.part.shape, mesh_dir / f"{name}.glb", color=node.part.color, tolerance=tolerance)
                mesh_of[key] = f"meshes/{name}.glb"
            records.append({
                "id": node.id, "name": node.part.name, "kind": node.part.kind, "mesh": mesh_of[key],
                "color": node.part.color,
                "matrix": [float(v) for v in location_to_matrix(node.loc).flatten(order="F")],
                "world_matrix": [float(v) for v in self.world_matrix(node.id).flatten(order="F")],
                "parent": node.parent, "material": node.part.material,
                "joint": node.joint.to_dict() if node.joint is not None else None,
                "emissive_from": dict(node.emissive_from) if node.emissive_from is not None else None,
                "display_from": dict(node.display_from) if node.display_from is not None else None,
                "explode": list(node.explode) if node.explode is not None else None,
            })
            if node.wire is not None:
                records[-1]["wire"] = node.wire
        lo, hi = self.bounds()
        scene = {"name": self.name, "units": "mm", "up": "Z", "nodes": records, "bounds": [list(lo), list(hi)],
                 "connectors": [dict(c) for c in self.connectors]}
        path = out / "scene.json"
        fd, tmp = tempfile.mkstemp(prefix=".scene-", suffix=".json", dir=out)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(scene, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        # compare case-insensitively: on APFS/NTFS replacing "Lid.glb" with "lid.glb" keeps the old entry
        keep = {Path(m).name.lower() for m in mesh_of.values()}
        for stale in mesh_dir.glob("*.glb"):
            if stale.name.lower() not in keep and stale.is_file():
                log.debug("to_scene(%s): removing stale mesh %s", self.name, stale.name)
                stale.unlink()
        log.debug("to_scene(%s): %d nodes, %d meshes → %s", self.name, len(records), len(mesh_of), path)
        return path
