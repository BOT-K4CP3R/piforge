"""Tests for piforge.mech.export (OCC kernel → slow)."""

from __future__ import annotations

import json
import logging
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pytest
import trimesh

from piforge.core.errors import ValidationError

pytestmark = pytest.mark.slow


def _load_3mf(path) -> trimesh.Trimesh:
    """Minimal 3MF reader (core spec) so the test does not depend on lxml."""
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert "[Content_Types].xml" in names and "_rels/.rels" in names
        root = ET.fromstring(z.read("3D/3dmodel.model"))
    assert root.get("unit") == "millimeter"
    verts, faces = [], []
    for obj in root.findall("m:resources/m:object", ns):
        base = len(verts)
        for v in obj.findall("m:mesh/m:vertices/m:vertex", ns):
            verts.append([float(v.get("x")), float(v.get("y")), float(v.get("z"))])
        for t in obj.findall("m:mesh/m:triangles/m:triangle", ns):
            faces.append([base + int(t.get("v1")), base + int(t.get("v2")), base + int(t.get("v3"))])
    assert root.findall("m:build/m:item", ns)
    return trimesh.Trimesh(np.array(verts), np.array(faces), process=True)


def _glb_json(path) -> dict:
    raw = path.read_bytes()
    assert raw[:4] == b"glTF"
    length = struct.unpack("<I", raw[12:16])[0]
    assert raw[16:20] == b"JSON"
    return json.loads(raw[20:20 + length])


def test_to_trimesh_is_watertight_and_respects_tolerance():
    from build123d import Cylinder

    from piforge.mech.export import to_trimesh

    cyl = Cylinder(10, 5)
    coarse = to_trimesh(cyl, tolerance=0.2)
    fine = to_trimesh(cyl, tolerance=0.01)
    assert coarse.is_watertight and fine.is_watertight
    assert len(fine.faces) > len(coarse.faces)
    assert fine.volume == pytest.approx(cyl.volume, rel=1e-3)
    assert np.allclose(fine.bounds, [[-10, -10, -2.5], [10, 10, 2.5]], atol=1e-6)


def test_to_trimesh_handles_compounds_and_rejects_empty():
    from build123d import Box, Compound, Location

    from piforge.core.errors import PiForgeError
    from piforge.mech.export import to_trimesh

    comp = Compound(children=[Box(2, 2, 2), Box(2, 2, 2).moved(Location((10, 0, 0)))])
    mesh = to_trimesh(comp)
    assert mesh.volume == pytest.approx(16.0, rel=1e-6)
    assert len(mesh.split(only_watertight=True)) == 2
    with pytest.raises(PiForgeError):
        to_trimesh(Compound(children=[]))


def test_export_formats(spaced_tmp):
    """Review focus #1: exports must work in a directory with spaces, '~' and non-ASCII."""
    from build123d import import_step

    from piforge.mech.export import export_part
    from piforge.mech.part import PartSpec
    from piforge.mech.primitives import rounded_box

    part = PartSpec("bracket ż", rounded_box(30, 20, 10, radius=3), print_rotation=(90, 0, 0))
    out = spaced_tmp / "out dir"
    files = export_part(part, out)
    assert set(files) == {"stl", "3mf", "step"}
    for fmt, path in files.items():
        assert path.exists() and path.stat().st_size > 0, fmt
        assert path.parent == out

    stl = trimesh.load(files["stl"], force="mesh")
    assert stl.is_watertight
    assert stl.bounds[0][2] == pytest.approx(0.0, abs=1e-6)  # on the bed
    assert stl.extents == pytest.approx((30, 10, 20), abs=0.01)  # rotated 90° about X
    assert stl.volume == pytest.approx(part.volume, rel=0.01)

    m3 = _load_3mf(files["3mf"])
    assert m3.is_watertight
    assert m3.bounds[0][2] == pytest.approx(0.0, abs=1e-5)
    assert m3.extents == pytest.approx((30, 10, 20), abs=0.01)

    step = import_step(files["step"])  # STEP keeps the design frame
    bb = step.bounding_box()
    assert (bb.min.Z, bb.max.Z) == pytest.approx((0.0, 10.0), abs=1e-3)
    assert bb.size.X == pytest.approx(30, abs=1e-3)


def test_export_design_frame_and_format_selection(tmp_path):
    from piforge.mech.export import export_part
    from piforge.mech.part import PartSpec
    from piforge.mech.primitives import rounded_box

    part = PartSpec("plate", rounded_box(30, 20, 10), print_rotation=(0, 0, 0))
    files = export_part(part, tmp_path, formats=("stl",), for_print=False)
    assert set(files) == {"stl"}
    mesh = trimesh.load(files["stl"], force="mesh")
    assert np.allclose(mesh.bounds, [[-15, -10, 0], [15, 10, 10]], atol=1e-6)
    with pytest.raises(ValidationError):
        export_part(part, tmp_path, formats=("dwg",))


def test_export_auto_orientation_falls_back_to_identity(tmp_path, monkeypatch, caplog):
    from piforge.mech.export import export_part
    from piforge.mech.part import PartSpec
    from piforge.mech.primitives import rounded_box

    monkeypatch.setitem(sys.modules, "piforge.fab.orient", None)  # import → ImportError
    part = PartSpec("tower", rounded_box(10, 10, 40))  # print_rotation=None → auto
    with caplog.at_level(logging.WARNING, logger="piforge.mech.export"):
        files = export_part(part, tmp_path, formats=("stl",))
    mesh = trimesh.load(files["stl"], force="mesh")
    assert mesh.extents == pytest.approx((10, 10, 40), abs=0.01)  # identity rotation
    assert mesh.bounds[0][2] == pytest.approx(0, abs=1e-6)
    assert any("orient" in r.message for r in caplog.records)


def test_export_glb_colour_and_units(tmp_path):
    from build123d import Box, Color, Compound, Location

    from piforge.mech.export import export_glb

    path = export_glb(Box(10, 20, 30), tmp_path / "sub dir" / "box.glb", color="#ff0000")
    assert path.exists()
    doc = _glb_json(path)
    factors = [m["pbrMetallicRoughness"]["baseColorFactor"] for m in doc["materials"]]
    assert factors[0][:3] == pytest.approx([1.0, 0.0, 0.0], abs=1e-3)
    scene = trimesh.load(path)
    assert np.allclose(scene.bounds, [[-5, -10, -15], [5, 10, 15]], atol=1e-6)  # mm, Z-up, untouched

    a = Box(1, 1, 1)
    a.color = Color("#00ff00")
    comp = Compound(children=[a, Box(1, 1, 1).moved(Location((3, 0, 0)))])
    path2 = export_glb(comp.moved(Location((0, 0, 5))), tmp_path / "two.glb", color="#0000ff")
    doc2 = _glb_json(path2)
    colours = sorted(tuple(round(c, 3) for c in m["pbrMetallicRoughness"]["baseColorFactor"][:3])
                     for m in doc2["materials"])
    assert colours == [(0.0, 0.0, 1.0), (0.0, 1.0, 0.0)]
    assert np.allclose(trimesh.load(path2).bounds, [[-0.5, -0.5, 4.5], [3.5, 0.5, 5.5]], atol=1e-6)


def test_partspec_basics():
    from piforge.core.errors import NotFoundError
    from piforge.mech.part import PartSpec
    from piforge.mech.primitives import rounded_box

    shape = rounded_box(10, 20, 3)
    p = PartSpec("lid", shape, material="petg", color="#4C8BF5")
    assert p.kind == "printed" and p.quantity == 1 and p.print_rotation is None
    assert p.material == "PETG" and p.color == "#4c8bf5"
    lo, hi = p.bounds()
    assert lo == pytest.approx((-5, -10, 0)) and hi == pytest.approx((5, 10, 3))
    assert p.volume == pytest.approx(600)
    assert PartSpec("pcb", shape, kind="pcb", material="FR4").material == "FR4"
    assert PartSpec("r", shape, print_rotation=[90, 0, 0]).print_rotation == (90.0, 0.0, 0.0)
    with pytest.raises(ValidationError):
        PartSpec("x", shape, kind="glued")
    with pytest.raises(ValidationError):
        PartSpec("x", shape, quantity=0)
    with pytest.raises(ValidationError):
        PartSpec("x", shape, color="blue")
    with pytest.raises(ValidationError):
        PartSpec("x", shape, print_rotation=(90, 0))
    with pytest.raises(ValidationError):
        PartSpec("", shape)
    with pytest.raises(NotFoundError):
        PartSpec("x", shape, material="unobtainium")


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def test_export_glb_writes_linear_float_base_colour(tmp_path):
    """glTF 2.0 baseColorFactor is LINEAR: sRGB hex must be decoded, and kept as floats (no uint8)."""
    from build123d import Box, Color, Compound, Location

    from piforge.mech.export import export_glb

    doc = _glb_json(export_glb(Box(1, 1, 1), tmp_path / "grey.glb", color="#808080"))
    (factor,) = [m["pbrMetallicRoughness"]["baseColorFactor"] for m in doc["materials"]]
    assert factor[:3] == pytest.approx([0.216] * 3, abs=0.005)
    assert factor[3] == pytest.approx(1.0)

    # exact float values, also for dark colours where uint8 quantisation would be visible
    for hexcol in ("#4c8bf5", "#3a3a3a", "#101010", "#050505"):
        doc = _glb_json(export_glb(Box(1, 1, 1), tmp_path / f"{hexcol[1:]}.glb", color=hexcol))
        want = [_srgb_to_linear(int(hexcol[i:i + 2], 16) / 255) for i in (1, 3, 5)]
        got = doc["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"][:3]
        assert got == pytest.approx(want, abs=1e-5), hexcol
    assert _glb_json(tmp_path / "4c8bf5.glb")["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"][:3] \
        == pytest.approx([0.072, 0.258, 0.913], abs=1e-3)

    # multi-leaf compound: each leaf's colour converted; the patched GLB still loads with its geometry
    a = Box(1, 1, 1)
    a.color = Color("#808080")
    comp = Compound(children=[a, Box(1, 1, 1).moved(Location((3, 0, 0)))])
    path = export_glb(comp, tmp_path / "two.glb", color="#4c8bf5")
    factors = sorted(tuple(round(c, 3) for c in m["pbrMetallicRoughness"]["baseColorFactor"][:3])
                     for m in _glb_json(path)["materials"])
    assert factors == [(0.072, 0.258, 0.913), (0.216, 0.216, 0.216)]
    loaded = trimesh.load(path)
    assert len(loaded.geometry) == 2
    assert np.allclose(loaded.bounds, [[-0.5, -0.5, -0.5], [3.5, 0.5, 0.5]], atol=1e-6)
    raw = path.read_bytes()
    assert struct.unpack("<I", raw[8:12])[0] == len(raw)  # header length matches after patching


def test_export_parts_rejects_file_name_collisions(tmp_path):
    from build123d import Box

    from piforge.mech.export import export_parts
    from piforge.mech.part import PartSpec

    a = PartSpec("lid box", Box(10, 10, 2), print_rotation=(0, 0, 0))
    b = PartSpec("lid_box", Box(12, 10, 2), print_rotation=(0, 0, 0))
    with pytest.raises(ValidationError, match="lid box.*lid_box|lid_box.*lid box"):
        export_parts([a, b], tmp_path / "out", ("stl",))
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())  # nothing written

    c = PartSpec("Lid", Box(10, 10, 2), print_rotation=(0, 0, 0))
    d = PartSpec("lid", Box(10, 10, 2), print_rotation=(0, 0, 0))  # same file on case-insensitive disks
    with pytest.raises(ValidationError):
        export_parts([c, d], tmp_path / "out2", ("stl",))

    written = export_parts([a, PartSpec("base", Box(5, 5, 5), print_rotation=(0, 0, 0))], tmp_path / "ok",
                           ("stl", "3mf"))
    assert set(written) == {"lid box", "base"}
    assert written["lid box"]["stl"].name == "lid_box.stl" and written["base"]["3mf"].exists()
