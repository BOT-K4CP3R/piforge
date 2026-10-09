"""Tests for piforge.mech.assembly (OCC kernel → slow)."""

from __future__ import annotations

import json
import re

import numpy as np
import pytest

from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.core.report import Severity

pytestmark = pytest.mark.slow


def _box(lx, ly, lz, *, min_x=False):
    from build123d import Align, Box

    ax = Align.MIN if min_x else Align.CENTER
    return Box(lx, ly, lz, align=(ax, Align.CENTER, Align.CENTER))


def _apply(loc, p):
    """Apply a build123d Location to a point (via its 3x4 gp_Trsf matrix)."""
    t = loc.wrapped.Transformation()
    m = np.array([[t.Value(r, c) for c in range(1, 5)] for r in range(1, 4)])
    return tuple(m[:, :3] @ np.asarray(p, float) + m[:, 3])


def test_assembly_world_location_with_joint():
    from piforge.mech.assembly import Assembly, Joint
    from piforge.mech.part import PartSpec

    asm = Assembly("arm")
    asm.add(PartSpec("base", _box(10, 10, 2)), id="base")
    asm.add(PartSpec("arm", _box(20, 4, 2, min_x=True)), (10, 0, 2), id="arm", parent="base",
            joint=Joint("revolute", (0, 0, 1), min=-90, max=90))
    tip = asm.add(PartSpec("tip", _box(1, 1, 1)), (5, 0, 0), parent="arm")
    assert tip == "tip"

    rest = asm.world_location("tip")
    assert _apply(rest, (0, 0, 0)) == pytest.approx((15, 0, 2))
    turned = asm.world_location("tip", {"arm": 90})
    assert _apply(turned, (0, 0, 0)) == pytest.approx((10, 5, 2), abs=1e-9)
    assert _apply(turned, (1, 0, 0)) == pytest.approx((10, 6, 2), abs=1e-9)

    # joint origin away from the node origin: rotate about the base centre instead
    asm.add(PartSpec("arm2", _box(20, 4, 2, min_x=True)), (10, 0, 2), id="arm2", parent="base",
            joint=Joint("revolute", (0, 0, 1), origin=(-10, 0, 0), min=0, max=180))
    assert _apply(asm.world_location("arm2", {"arm2": 90}), (0, 0, 0)) == pytest.approx((0, 10, 2), abs=1e-9)

    asm.add(PartSpec("slider", _box(2, 2, 2)), (0, 0, 10), id="slider",
            joint=Joint("prismatic", (2, 0, 0), min=0, max=50, value=5))
    assert asm.node("slider").joint.axis == pytest.approx((1, 0, 0))  # normalised
    assert _apply(asm.world_location("slider"), (0, 0, 0)) == pytest.approx((5, 0, 10))
    assert _apply(asm.world_location("slider", {"slider": 20}), (0, 0, 0)) == pytest.approx((20, 0, 10))

    with pytest.raises(ValidationError):
        asm.world_location("arm", {"arm": 120})  # outside [min, max]

    moved = asm.world_shape("tip", {"arm": 90}).bounding_box()
    assert (moved.min.X, moved.min.Y) == pytest.approx((9.5, 4.5), abs=1e-6)


def test_location_tuple_matches_meshutil_rotation():
    from piforge.fab.meshutil import rotation_matrix
    from piforge.mech.assembly import Assembly
    from piforge.mech.part import PartSpec

    asm = Assembly("rot")
    asm.add(PartSpec("p", _box(1, 1, 1)), (1, 2, 3, 30, -45, 120), id="p")
    loc = asm.world_location("p")
    expected = rotation_matrix(30, -45, 120) @ np.array([1.0, 0.5, -2.0]) + [1, 2, 3]
    assert _apply(loc, (1.0, 0.5, -2.0)) == pytest.approx(tuple(expected), abs=1e-9)


def test_add_validation():
    from piforge.mech.assembly import Assembly, DuplicateIdError, Joint
    from piforge.mech.part import PartSpec

    asm = Assembly("v")
    part = PartSpec("Block", _box(1, 1, 1))
    assert asm.add(part, id="a") == "a"
    with pytest.raises(DuplicateIdError):
        asm.add(part, id="a")
    assert issubclass(DuplicateIdError, PiForgeError)
    auto1, auto2 = asm.add(part), asm.add(part)
    assert len({"a", auto1, auto2}) == 3
    with pytest.raises(NotFoundError):
        asm.add(part, parent="nope")
    with pytest.raises(NotFoundError):
        asm.node("missing")
    with pytest.raises(ValidationError):
        asm.add(part, (1, 2))
    with pytest.raises(ValidationError):
        Joint("ball", (0, 0, 1))
    with pytest.raises(ValidationError):
        Joint("revolute", (0, 0, 0))
    with pytest.raises(ValidationError):
        Joint("revolute", (0, 0, 1), min=10, max=0)
    assert [n.id for n in asm.nodes] == ["a", auto1, auto2]


def test_interference_detected():
    from piforge.mech.assembly import Assembly
    from piforge.mech.part import PartSpec

    asm = Assembly("clash")
    asm.add(PartSpec("a", _box(10, 10, 10)), id="a")
    asm.add(PartSpec("b", _box(10, 10, 10)), (5, 0, 0), id="b")
    rep = asm.check_interference()
    errs = rep.by_code("ASM.INTERFERENCE")
    assert len(errs) == 1 and errs[0].severity == Severity.ERROR
    assert errs[0].data["volume_mm3"] == pytest.approx(500, rel=1e-6)
    assert not rep.ok
    assert asm.check_interference(ignore={frozenset({"a", "b"})}).ok
    assert asm.check_interference(ignore=[("b", "a")]).ok  # plain tuples work too
    assert asm.check_interference(kinds={"pcb"}).ok
    assert not asm.check_interference(kinds="printed").ok  # a bare string is one kind, not letters
    with pytest.raises(ValidationError):
        asm.check_interference(ignore=["ab"])

    touching = Assembly("touch")
    touching.add(PartSpec("a", _box(10, 10, 10)), id="a")
    touching.add(PartSpec("b", _box(10, 10, 10)), (10, 0, 0), id="b")
    touching.add(PartSpec("c", _box(10, 10, 10)), (0, 0, 10), id="c")
    rep2 = touching.check_interference()
    assert not rep2.by_code("ASM.INTERFERENCE")
    assert rep2.ok


def test_sweep_joint_reports_collision_angles():
    from piforge.mech.assembly import Assembly, Joint
    from piforge.mech.part import PartSpec

    asm = Assembly("sweep")
    asm.add(PartSpec("base", _box(4, 4, 4)), id="base")
    asm.add(PartSpec("wall", _box(2, 20, 10)), (15, 0, 5), id="wall")
    asm.add(PartSpec("bar", _box(20, 2, 2, min_x=True)), (0, 0, 5), id="bar", parent="base",
            joint=Joint("revolute", (0, 0, 1), min=-90, max=90))
    rep = asm.sweep_joint("bar", steps=7, ignore={frozenset({"bar", "base"})})
    hits = rep.by_code("ASM.JOINT_COLLISION")
    assert len(hits) == 1
    assert hits[0].severity == Severity.ERROR
    assert hits[0].data["other"] == "wall"
    assert hits[0].data["angles"] == pytest.approx([-30, 0, 30])

    free = Assembly("free")
    free.add(PartSpec("base", _box(4, 4, 4)), id="base")
    free.add(PartSpec("bar", _box(20, 2, 2, min_x=True)), (0, 0, 5), id="bar", parent="base",
             joint=Joint("revolute", (0, 0, 1), min=-90, max=90))
    ok = free.sweep_joint("bar", steps=5, ignore={frozenset({"bar", "base"})})
    assert ok.ok and ok.by_code("ASM.JOINT_OK")
    with pytest.raises(ValidationError):
        free.sweep_joint("base")


def test_bounds_follow_joints():
    from piforge.mech.assembly import Assembly, Joint
    from piforge.mech.part import PartSpec

    asm = Assembly("b")
    assert asm.bounds() == ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    asm.add(PartSpec("bar", _box(20, 2, 2, min_x=True)), id="bar",
            joint=Joint("revolute", (0, 0, 1), min=0, max=90))
    lo, hi = asm.bounds()
    assert lo == pytest.approx((0, -1, -1), abs=1e-6) and hi == pytest.approx((20, 1, 1), abs=1e-6)
    lo, hi = asm.bounds({"bar": 90})
    assert lo == pytest.approx((-1, 0, -1), abs=1e-6) and hi == pytest.approx((1, 20, 1), abs=1e-6)


def test_to_scene_contract(spaced_tmp):
    """scene.json must follow spec §5.4 exactly (keys, column-major 16-float matrices, GLBs)."""
    from piforge.mech.assembly import Assembly, Joint
    from piforge.mech.fasteners import screw
    from piforge.mech.part import PartSpec
    from piforge.mech.primitives import rounded_box

    asm = Assembly("demo ż")
    base = PartSpec("Base", rounded_box(40, 30, 5), color="#3b82f6", material="PETG")
    bolt = PartSpec("M3 screw", screw("M3", 8), kind="fastener", color="#888888")
    flap = PartSpec("Flap", rounded_box(20, 2, 10))
    asm.add(base, (100, 0, 0), id="base")
    asm.add(bolt, (10, 10, 5), id="screw/1", parent="base")
    asm.add(bolt, (-10, 10, 5), id="screw 2", parent="base")
    asm.add(flap, (0, 0, 5), id="flap", parent="base",
            joint=Joint("revolute", (1, 0, 0), min=-90, max=90, value=10,
                        driven_by={"device": "SERVO1", "prop": "angle"}),
            explode=(0, 0, 1), emissive_from={"device": "D1", "prop": "brightness", "color": "#ff2200"})

    path = asm.to_scene(spaced_tmp / "build")
    assert path.name == "scene.json" and path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "demo ż" and data["units"] == "mm" and data["up"] == "Z"
    assert len(data["bounds"]) == 2 and all(len(v) == 3 for v in data["bounds"])
    lo, hi = data["bounds"]
    assert lo[0] == pytest.approx(80, abs=0.01) and hi[0] == pytest.approx(120, abs=0.01)

    nodes = {n["id"]: n for n in data["nodes"]}
    assert list(nodes) == ["base", "screw/1", "screw 2", "flap"]
    required = {"id", "name", "kind", "mesh", "color", "matrix", "parent", "material",
                "joint", "emissive_from", "display_from", "explode"}
    meshes = set()
    for n in data["nodes"]:
        assert required <= set(n), n["id"]
        assert len(n["matrix"]) == 16 and all(isinstance(v, float) for v in n["matrix"])
        assert re.fullmatch(r"#[0-9a-f]{6}", n["color"])
        assert n["mesh"].startswith("meshes/") and n["mesh"].endswith(".glb")
        assert (path.parent / n["mesh"]).exists()
        meshes.add(n["mesh"])
    assert len(meshes) == 3  # the two screws share one GLB

    assert nodes["base"]["parent"] is None and nodes["screw 2"]["parent"] == "base"
    assert nodes["base"]["kind"] == "printed" and nodes["base"]["material"] == "PETG"
    assert nodes["screw/1"]["kind"] == "fastener"
    # column-major: translation lives in elements 12..14; matrix is local to the parent
    assert nodes["base"]["matrix"][12:15] == pytest.approx([100, 0, 0])
    assert nodes["screw/1"]["matrix"][12:15] == pytest.approx([10, 10, 5])
    assert nodes["screw/1"]["matrix"][15] == pytest.approx(1.0)
    assert nodes["screw/1"]["world_matrix"][12:15] == pytest.approx([110, 10, 5])

    j = nodes["flap"]["joint"]
    assert j == {"type": "revolute", "axis": [1.0, 0.0, 0.0], "origin": [0.0, 0.0, 0.0],
                 "min": -90.0, "max": 90.0, "value": 10.0,
                 "driven_by": {"device": "SERVO1", "prop": "angle"}}
    # rest pose in "matrix" (no joint rotation); the joint value is applied by the viewer
    assert nodes["flap"]["matrix"][:3] == pytest.approx([1, 0, 0])
    assert nodes["flap"]["matrix"][4:7] == pytest.approx([0, 1, 0])
    assert nodes["flap"]["explode"] == [0.0, 0.0, 1.0]
    assert nodes["flap"]["emissive_from"] == {"device": "D1", "prop": "brightness", "color": "#ff2200"}
    assert nodes["base"]["joint"] is None and nodes["base"]["explode"] is None
    assert all(n["display_from"] is None for n in data["nodes"])


def test_display_from_stored_and_exported(spaced_tmp):
    """display_from: validated on add, kept on the node, written to scene.json (pure data path)."""
    from piforge.mech import PartSpec, rounded_box
    from piforge.mech.assembly import Assembly

    asm = Assembly("sf")
    face = PartSpec("window", rounded_box(40, 4, 60, radius=1), kind="reference")
    src = {"device": "F1", "kind": "splitflap"}
    asm.add(face, (0, 0, 0), id="win1", display_from=src)
    asm.add(face, (50, 0, 0), id="win2")
    src["device"] = "changed"  # the node keeps its own copy
    assert asm.node("win1").display_from == {"device": "F1", "kind": "splitflap"}
    assert asm.node("win2").display_from is None
    for bad in ({"device": "F1"}, {"kind": "splitflap"}, "F1", {"device": "F1", "kind": "lcd"}):
        with pytest.raises(ValidationError):
            asm.add(face, id=f"bad{len(asm)}", display_from=bad)  # type: ignore[arg-type]
    assert len(asm) == 2

    data = json.loads(asm.to_scene(spaced_tmp / "b").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["win1"]["display_from"] == {"device": "F1", "kind": "splitflap"}
    assert nodes["win2"]["display_from"] is None


def test_joint_matrix_rejects_out_of_range_mutated_value():
    """Joint.value may be reassigned after construction; matrix() must range-check it too."""
    from piforge.mech.assembly import Assembly, Joint
    from piforge.mech.part import PartSpec

    j = Joint("revolute", (0, 0, 1), min=-45, max=45, value=0)
    j.value = 30.0
    assert j.matrix()[:3, :3] == pytest.approx(j.matrix(30.0)[:3, :3])
    j.value = 90.0
    with pytest.raises(ValidationError, match="outside"):
        j.matrix()
    asm = Assembly("a")
    asm.add(PartSpec("p", _box(1, 1, 1)), id="p", joint=Joint("prismatic", (1, 0, 0), min=0, max=5))
    asm.node("p").joint.value = -1.0
    with pytest.raises(ValidationError):
        asm.world_matrix("p")
    assert asm.world_matrix("p", {"p": 2.0})[0, 3] == pytest.approx(2.0)  # explicit values still work


def test_to_scene_removes_stale_meshes(spaced_tmp):
    from piforge.mech.assembly import Assembly
    from piforge.mech.part import PartSpec

    out = spaced_tmp / "build"
    first = Assembly("v1")
    first.add(PartSpec("old part", _box(2, 2, 2)), id="old part")
    first.add(PartSpec("keep", _box(1, 1, 1)), (5, 0, 0), id="keep")
    first.to_scene(out)
    assert (out / "meshes" / "old_part.glb").exists()
    (out / "meshes" / "notes.txt").write_text("not ours")
    (out / "other.glb").write_bytes(b"outside the meshes dir")

    second = Assembly("v2")
    second.add(PartSpec("keep", _box(1, 1, 1)), id="keep")
    second.add(PartSpec("new", _box(1, 1, 1)), (5, 0, 0), id="new")
    path = second.to_scene(out)
    referenced = {n["mesh"] for n in json.loads(path.read_text(encoding="utf-8"))["nodes"]}
    on_disk = {f"meshes/{p.name}" for p in (out / "meshes").glob("*.glb")}
    assert on_disk == referenced == {"meshes/keep.glb", "meshes/new.glb"}
    assert (out / "meshes" / "notes.txt").exists() and (out / "other.glb").exists()


def test_to_scene_disambiguates_colliding_mesh_names(spaced_tmp):
    from piforge.mech.assembly import Assembly
    from piforge.mech.part import PartSpec

    asm = Assembly("clash")
    asm.add(PartSpec("a", _box(1, 1, 1)), id="lid box")
    asm.add(PartSpec("b", _box(2, 2, 2)), (5, 0, 0), id="lid_box")
    asm.add(PartSpec("c", _box(3, 3, 3)), (10, 0, 0), id="LID BOX")
    path = asm.to_scene(spaced_tmp / "b")
    meshes = [n["mesh"] for n in json.loads(path.read_text(encoding="utf-8"))["nodes"]]
    assert len({m.lower() for m in meshes}) == 3
    assert all((path.parent / m).exists() for m in meshes)


def test_to_scene_case_only_rename_keeps_the_mesh(spaced_tmp):
    """Rebuilding with an id that differs only in case must not delete the freshly written mesh."""
    from piforge.mech.assembly import Assembly
    from piforge.mech.part import PartSpec

    out = spaced_tmp / "build"
    first = Assembly("v1")
    first.add(PartSpec("Lid", _box(2, 2, 2)), id="Lid")
    first.to_scene(out)
    second = Assembly("v2")
    second.add(PartSpec("lid", _box(2, 2, 2)), id="lid")
    path = second.to_scene(out)
    for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]:
        mesh = out / node["mesh"]
        assert mesh.exists() and mesh.stat().st_size > 0
