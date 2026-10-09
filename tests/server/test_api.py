"""REST API of the PiForge web server, exercised on the hand-made fixture build directory."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import trimesh

from . import fixture_build as fb


def _wait_status(client, states: set[str], timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        st = client.get("/api/build/status").json()
        if st["state"] in states or time.monotonic() > deadline:
            return st
        time.sleep(0.05)


def _spice_ready() -> bool:
    return (importlib.util.find_spec("piforge.spice") is not None
            and importlib.util.find_spec("piforge.spice.benches") is not None
            and (shutil.which("ngspice") is not None or Path("/opt/homebrew/bin/ngspice").exists()))


# ------------------------------------------------------------------------------- static files
def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'type="importmap"' in r.text
    assert "/static/vendor/three/three.module.js" in r.text


def test_static_and_vendor_served(client):
    r = client.get("/static/js/main.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert client.get("/static/vendor/three/three.module.js").status_code == 200
    assert client.get("/static/vendor/uplot/uPlot.esm.js").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200


def test_build_files_served(client):
    r = client.get("/build/meshes/lid.glb")
    assert r.status_code == 200
    assert r.content[:4] == b"glTF"
    assert "no-cache" in r.headers.get("cache-control", "")
    assert client.get("/build/parts/bracket.stl").status_code == 200


def test_build_path_traversal_blocked(client, fixture_project: Path):
    (fixture_project / "secret.txt").write_text("top secret", encoding="utf-8")
    for url in ("/build/../secret.txt", "/build/%2e%2e/secret.txt", "/build/..%2fsecret.txt"):
        r = client.get(url)
        assert "top secret" not in r.text


# ------------------------------------------------------------------------------- project data
def test_project(client, fixture_project: Path):
    p = client.get("/api/project").json()
    assert p["name"] == "fixture_gauge"
    assert p["board"] == "rpi4b"
    assert p["has_build"] is True
    assert p["firmware"] == "firmware/main.py"
    assert Path(p["project_dir"]) == fixture_project.resolve()
    assert p["counts"] == {"error": 1, "warning": 3, "info": 6}
    assert p["printer_profile"]["name"] == "prusa_mk4"
    assert p["printer_profile"]["build_x"] == 250
    assert p["printer_profile"]["max_overhang_deg"] == 45.0
    assert p["version"]
    assert p["build"]["state"] == "idle"


def test_scene(client):
    s = client.get("/api/scene").json()
    assert s["units"] == "mm" and s["up"] == "Z"
    ids = [n["id"] for n in s["nodes"]]
    assert ids == ["base", "lid", "pi", "dial", "needle", "led", "button", "bracket", "screw1", "screw2"]
    for n in s["nodes"]:
        assert len(n["matrix"]) == 16
        assert n["mesh"].startswith("meshes/")
    needle = next(n for n in s["nodes"] if n["id"] == "needle")
    assert needle["joint"]["driven_by"]["device"] == "SERVO1"
    assert s["version"]
    assert all(n["display_from"] is None for n in s["nodes"])


def test_scene_display_from_passthrough(tmp_path):
    """Split-flap windows: ``display_from`` reaches the GUI unchanged, spools keep their driven joint."""
    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    from .fixture_build import make_fixture_project, sf_spool_id, sf_window_id

    root = make_fixture_project(tmp_path / "flaps ~ż", splitflaps=3, flap_device=lambda i: f"F{i}")
    with TestClient(create_app(root)) as c:
        nodes = {n["id"]: n for n in c.get("/api/scene").json()["nodes"]}
        for i in range(3):
            assert nodes[sf_window_id(i)]["display_from"] == {"device": f"F{i}", "kind": "splitflap"}
            j = nodes[sf_spool_id(i)]["joint"]
            assert j["driven_by"] == {"device": f"F{i}", "prop": "angle", "scale": 1.0, "offset": 0.0}
            assert (j["min"], j["max"]) == (0.0, 360.0)
        assert c.get("/build/meshes/sf_window.glb").status_code == 200


def test_report(client):
    r = client.get("/api/report").json()
    assert r["counts"] == {"error": 1, "warning": 3, "info": 6}
    assert len(r["findings"]) == 10
    assert {f["source"] for f in r["findings"]} >= {"erc", "power", "assembly", "print:bracket"}
    asm = next(f for f in r["findings"] if f["code"] == "ASM.INTERFERENCE")
    assert asm["subject"] == "node:bracket/base" and asm["data"] == {"a": "bracket", "b": "base",
                                                                      "volume_mm3": 12.5}


def test_report_severities_are_normalised(client, fixture_project: Path):
    """Numeric (IntEnum), upper-case or unknown severities never break the counts or the GUI."""
    import json

    path = fixture_project / "build" / "report.json"
    path.write_text(json.dumps({"title": "build", "findings": [
        {"code": "A.B", "severity": 2, "message": "m"}, {"code": "A.C", "severity": "WARNING", "message": "m"},
        {"code": "A.D", "severity": "weird", "message": None}, "not a finding"]}), encoding="utf-8")
    r = client.get("/api/report").json()
    assert [f["severity"] for f in r["findings"]] == ["error", "warning", "info"]
    assert r["counts"] == {"error": 1, "warning": 1, "info": 1} and r["ok"] is False
    assert r["findings"][2]["message"] == "" and r["findings"][2]["source"] == ""
    assert client.get("/api/project").json()["counts"] == r["counts"]


def test_parts(client):
    parts = client.get("/api/parts").json()
    assert [p["name"] for p in parts] == ["base", "lid", "dial", "needle", "bracket"]
    for p in parts:
        assert p["urls"]["stl"] == f"/build/parts/{p['name']}.stl"
        assert "3mf" not in p["urls"]  # fixture has no 3MF files → no dead download links
        assert p["node_ids"] == [p["name"]]
        assert p["analysis"]["estimate"]["mass_g"] > 0
    bracket = next(p for p in parts if p["name"] == "bracket")
    assert bracket["status"] == "warning"


def _expected_masks(glb: Path, rotation: tuple[float, float, float]) -> tuple[list[bool], list[bool]]:
    """(overhang, bridge) masks computed independently: the fixture leaves as stored in the GLB, in
    its node order, welded, with the fab rule and the prusa_mk4 bridge limit (10 mm)."""
    from piforge.fab.analyze import overhang_mask
    from piforge.fab.meshutil import place_on_bed, rotation_matrix

    leaves = {name: mesh for name, mesh, _ in fb.make_leaves()[glb.stem]}
    ordered = [fb.glb_leaf(leaves[n.rsplit("_", 1)[0]]) for n in fb.glb_node_names(glb)]
    placed = place_on_bed(trimesh.util.concatenate(ordered), rotation_matrix(*rotation))
    placed.merge_vertices(merge_tex=True, merge_norm=True)
    geometric = overhang_mask(placed, max_overhang_deg=45.0)
    over = overhang_mask(placed, max_overhang_deg=45.0, max_bridge_mm=10.0)
    return [bool(v) for v in over], [bool(v) for v in geometric & ~over]


@pytest.mark.parametrize("name", ["base", "lid", "dial", "needle", "bracket"])
def test_overhang_mask_matches_glb_faces(client, fixture_project: Path, name: str):
    """Mask length AND order match the GLB faces as three.js lists them (glTF node order)."""
    data = client.get(f"/api/parts/{name}/overhang").json()
    glb = fixture_project / "build" / data["mesh"]
    leaves = {n: m for n, m, _ in fb.make_leaves()[glb.stem]}
    counts = [len(leaves[n.rsplit("_", 1)[0]].faces) for n in fb.glb_node_names(glb)]
    assert data["mesh_face_counts"] == counts
    assert data["face_count"] == len(data["mask"]) == len(data["bridge_mask"]) == sum(counts)
    assert all(isinstance(v, bool) for v in data["mask"] + data["bridge_mask"])
    assert not any(a and b for a, b in zip(data["mask"], data["bridge_mask"], strict=True))
    rot = {"lid": (180, 0, 0)}.get(name, (0, 0, 0))
    assert data["method"] == "fab" and data["bridges_excluded"] is True
    from piforge.fab.profiles import get_printer
    assert data["max_bridge_mm"] == get_printer("prusa_mk4").max_bridge_mm
    assert (data["mask"], data["bridge_mask"]) == _expected_masks(glb, rot)
    if name == "bracket":  # ring under the r=11 cap: π(11² − 5²) ≈ 301.6 mm² (polygonal ≈ 300.7)
        assert data["overhang_count"] > 0
        assert data["overhang_area_mm2"] == pytest.approx(301.6, rel=0.02)
        assert not any(data["mask"][:counts[0]])  # the gusset leaf (first in the file) has none
        assert data["bridge_count"] == 0  # one-sided ledge: not a bridge
    else:
        assert data["overhang_count"] == 0
    if name == "base":  # the port window roof (8 mm span ≤ 10 mm) is a bridge, not an overhang
        assert data["bridge_count"] == 2
        assert data["bridge_area_mm2"] == pytest.approx(fb.PORT_W * fb.WALL, rel=1e-6)
    elif name != "bracket":
        assert data["bridge_count"] == 0


def test_overhang_flat_shaded_glb_still_finds_bridges(tmp_path: Path):
    """Regression: exporters split vertices along sharp edges (flat shading). Unwelded, the port
    roof would have no neighbours, look unsupported and be painted as an overhang."""
    from piforge.server.overhang import compute_overhang, glb_triangles

    base = next(m for n, m, _ in fb.make_leaves()["base"] if n == "base").copy()
    base.unmerge_vertices()  # every face owns its 3 vertices
    scene = trimesh.Scene()
    scene.add_geometry(base, node_name="base_0", geom_name="base_0")
    glb = tmp_path / "flat base.glb"
    glb.write_bytes(scene.export(file_type="glb"))
    v, f, _ = glb_triangles(glb)
    assert len(trimesh.Trimesh(v, f, process=False).face_adjacency) == 0  # a true triangle soup
    res = compute_overhang(glb, None, 45.0, 10.0)
    assert res["face_count"] == len(base.faces)
    assert res["overhang_count"] == 0 and res["bridge_count"] == 2
    assert res["bridge_area_mm2"] == pytest.approx(fb.PORT_W * fb.WALL)
    no_bridges = compute_overhang(glb, None, 45.0, None)  # printer without a bridge limit
    assert no_bridges["overhang_count"] == 2 and no_bridges["bridges_excluded"] is False


def test_overhang_uses_print_rotation(client, fixture_project: Path):
    import json

    idx = fixture_project / "build" / "parts" / "index.json"
    parts = json.loads(idx.read_text(encoding="utf-8"))
    next(p for p in parts if p["name"] == "bracket")["print_rotation"] = [180, 0, 0]
    idx.write_text(json.dumps(parts), encoding="utf-8")
    data = client.get("/api/parts/bracket/overhang").json()
    assert data["rotation"] == [180, 0, 0]
    # upside down the cap sits on the bed; only the gusset's 3×4 mm top face now hangs (inside the cap)
    assert data["overhang_area_mm2"] == pytest.approx(12.0, abs=0.1)
    assert (data["mask"], data["bridge_mask"]) == _expected_masks(
        fixture_project / "build" / data["mesh"], (180, 0, 0))


@pytest.mark.slow
def test_overhang_on_a_real_mech_export(tmp_path: Path):
    """GLB written by the real ``Assembly.to_scene``/``export_glb`` (smooth-shaded, split vertices):
    the 8 mm port roof is found as a bridge and the masks line up with the GLB's faces."""
    import json

    bd = pytest.importorskip("build123d")
    from fastapi.testclient import TestClient

    from piforge.mech.assembly import Assembly
    from piforge.mech.part import PartSpec
    from piforge.mech.primitives import hollow_box
    from piforge.server.app import create_app
    from piforge.server.overhang import glb_triangles

    proj = tmp_path / "real mech ~ż"
    build = proj / "build"
    (build / "parts").mkdir(parents=True)
    shell = hollow_box(60, 40, 20, wall=2.0, floor=2.0) - bd.Pos(30, 0, 9) * bd.Box(6, 8, 5)  # port in +X wall
    asm = Assembly("real")
    asm.add(PartSpec("shell", shell, kind="printed", material="PETG", color="#3b82f6"), id="shell")
    asm.to_scene(build)
    (build / "parts" / "index.json").write_text(json.dumps([{
        "name": "shell", "material": "PETG", "color": "#3b82f6", "quantity": 1, "files": {},
        "print_rotation": [0, 0, 0], "analysis": {}, "node_ids": ["shell"]}]), encoding="utf-8")
    (build / "manifest.json").write_text(json.dumps({"name": "real", "printer": "prusa_mk4"}), encoding="utf-8")
    with TestClient(create_app(proj)) as c:
        d = c.get("/api/parts/shell/overhang").json()
    _, faces, _ = glb_triangles(build / "meshes" / "shell.glb")
    assert d["face_count"] == len(d["mask"]) == len(d["bridge_mask"]) == len(faces)
    assert d["overhang_count"] == 0 and d["bridge_count"] >= 2
    assert d["bridge_area_mm2"] == pytest.approx(8.0 * 2.0, rel=0.01)  # roof 8 mm span × 2 mm wall


def test_overhang_unknown_part_404(client):
    r = client.get("/api/parts/brackett/overhang")
    assert r.status_code == 404
    assert "bracket" in r.json()["detail"]  # suggests the close match


def test_elec(client):
    e = client.get("/api/elec").json()
    assert e["available"] is True
    # elec.bom.bom_csv writes "Qty,Refs,Key,…": the API normalises column names to lower case
    assert e["bom_columns"] == ["qty", "refs", "key", "name", "value", "notes"]
    assert len(e["bom"]) == 6 and e["bom"][0]["refs"] == "U1" and e["bom"][0]["qty"] == "1"
    assert e["bom"][3]["name"] == "Resistor (axial, 0.25 W)"  # quoted comma survives
    assert len(e["wiring"]) == 7 and e["wiring"][0]["a_phys"] == "11"
    assert "GPIO17" in e["pinout_md"]
    assert "dtoverlay" in e["config_txt"]
    assert set(e["power"]["rails"]) == {"5V", "3V3"}
    assert [f["code"] for f in e["erc"]] == ["ERC.I2C_PULLUPS", "ERC.FLOATING_INPUT"]
    assert [f["code"] for f in e["power_findings"]] == ["POWER.RAIL_MARGIN"]
    assert e["files"]["wiring_svg"] == "/build/elec/wiring.svg"
    assert e["files"]["wiring_png"] is None


def test_elec_without_harness_is_graceful(client):
    """A build without 3D wires / cut list: empty lists, no dead links, scene ``connectors == []``."""
    e = client.get("/api/elec").json()
    assert e["wires"] == [] and e["cut_list"] == [] and e["cut_list_columns"] == []
    assert e["files"]["cut_list_csv"] is None and e["files"]["cut_list_md"] is None
    assert client.get("/api/scene").json()["connectors"] == []


def test_harness_wires_connectors_and_cut_list(tmp_path: Path):
    """Wire nodes + top-level connectors pass through ``/api/scene``; ``/api/elec`` lists the wires
    (flattened ``wire`` dicts with node id + colour) and the cut list with snake_case columns."""
    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    root = fb.make_fixture_project(tmp_path / "harness ~ż", wires=True)
    with TestClient(create_app(root)) as c:
        sc = c.get("/api/scene").json()
        assert [x["id"] for x in sc["connectors"]] == [x[0] for x in fb.CONNECTORS]
        assert sc["connectors"][0]["pos"] == fb.header_pin(1)
        wn = [n for n in sc["nodes"] if n["kind"] == "wire"]
        assert [n["wire"]["id"] for n in wn] == [w[0] for w in fb.WIRES]
        assert c.get("/build/" + wn[0]["mesh"]).status_code == 200
        e = c.get("/api/elec").json()
        w1 = e["wires"][0]
        assert (w1["id"], w1["node"], w1["color"]) == ("W1", fb.wire_node_id("W1"), "#16a34a")
        assert w1["from"] == {"ref": "U1", "pin": "11", "label": "GPIO17", "connector": "U1.11"}
        assert w1["to"]["connector"] == "D1.A" and w1["net"] == "GPIO17" and w1["gauge_awg"] == 24
        assert [w["cable"] for w in e["wires"]].count("SERVO lead") == 3
        assert e["cut_list_columns"] == ["wire_id", "from", "to", "net", "colour", "gauge", "length_mm"]
        assert len(e["cut_list"]) == len(fb.WIRES)
        assert e["cut_list"][0]["wire_id"] == "W1" and e["cut_list"][0]["from"] == "U1.11"
        assert float(e["cut_list"][0]["length_mm"]) == pytest.approx(w1["length_mm"], abs=0.1)
        assert e["files"]["cut_list_csv"] == "/build/elec/cut_list.csv"
        assert e["files"]["cut_list_md"] == "/build/elec/cut_list.md"


def test_cut_list_in_harness_layout(tmp_path: Path):
    """``mech.harness.Harness.cut_list_csv`` columns pass through unchanged (already snake_case)."""
    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    root = fb.make_fixture_project(tmp_path / "harness csv", wires=True)
    (root / "build" / "elec" / "cut_list.csv").write_text(
        "wire,cable,from,from_connector,to,to_connector,net,signal,color,gauge_awg,route_mm,length_mm,factory_lead\n"
        "W1,,U1.11,U1.11,D1.A,D1.A,GPIO17,LED drive,green,24,80.0,92.5,False\n", encoding="utf-8")
    with TestClient(create_app(root)) as c:
        e = c.get("/api/elec").json()
        assert e["cut_list_columns"][:3] == ["wire", "cable", "from"]
        assert e["cut_list"][0]["length_mm"] == "92.5" and e["cut_list"][0]["factory_lead"] == "False"


def test_partial_wire_nodes_are_tolerated(tmp_path: Path):
    """A wire node without a ``wire`` dict, junk connectors: the API still answers."""
    import json

    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    root = fb.make_fixture_project(tmp_path / "partial", wires=True)
    sp = root / "build" / "scene.json"
    scene = json.loads(sp.read_text(encoding="utf-8"))
    scene["nodes"].append({"id": "wire_X", "kind": "wire", "mesh": "meshes/W1.glb", "color": "#000000"})
    scene["connectors"] = {"not": "a list"}
    sp.write_text(json.dumps(scene), encoding="utf-8")
    with TestClient(create_app(root)) as c:
        assert c.get("/api/scene").json()["connectors"] == []
        w = c.get("/api/elec").json()["wires"][-1]
        assert (w["id"], w["node"], w["from"], w["to"]) == ("wire_X", "wire_X", {}, {})


def test_no_build_directory_is_graceful(tmp_path: Path):
    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    proj = tmp_path / "empty project"
    proj.mkdir()
    with TestClient(create_app(proj)) as c:
        p = c.get("/api/project").json()
        assert p["has_build"] is False and p["name"] == "empty project"
        assert c.get("/api/scene").json()["nodes"] == []
        assert c.get("/api/report").json()["findings"] == []
        assert c.get("/api/parts").json() == []
        assert c.get("/api/elec").json()["available"] is False
        assert c.get("/api/elec").json()["wires"] == [] and c.get("/api/scene").json()["connectors"] == []
        assert c.get("/api/spice/project").json() == []
        assert c.get("/").status_code == 200


# ------------------------------------------------------------------------------- SPICE
def test_spice_benches_shape(client):
    data = client.get("/api/spice/benches").json()
    assert set(data) >= {"available", "error", "benches"}
    if not data["available"]:
        assert data["benches"] == [] and data["error"]
        return
    keys = [b["key"] for b in data["benches"]]
    assert "rc_filter" in keys and "led_driver" in keys
    for b in data["benches"]:
        assert b["title"]
        for spec in b["params"].values():
            assert set(spec) >= {"default", "min", "max", "unit", "choices", "label"}


def test_spice_project_results(client):
    rows = client.get("/api/spice/project").json()
    assert rows[0]["label"] == "rc_debounce"
    assert rows[0]["result_url"] == "/build/sim/rc_debounce.json"


def test_spice_unknown_bench_404(client):
    pytest.importorskip("piforge.spice.benches")
    r = client.post("/api/spice/run", json={"key": "rc_filtre", "params": {}})
    assert r.status_code == 404
    assert "rc_filter" in r.json()["detail"]


@pytest.mark.spice
def test_spice_run_ok_and_invalid(client):
    if not _spice_ready():
        pytest.skip("piforge.spice not available yet or ngspice missing")
    r = client.post("/api/spice/run", json={"key": "rc_filter", "params": {}})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["key"] == "rc_filter"
    assert res["x_label"]
    assert res["traces"] and all(len(v) > 2 for v in res["traces"].values())
    assert isinstance(res["measures"], dict) and isinstance(res["analytic"], dict)
    assert set(res["report"]) >= {"ok", "counts", "findings"}
    # invalid: first numeric parameter with a lower bound, pushed below it
    benches = {b["key"]: b for b in client.get("/api/spice/benches").json()["benches"]}
    name, spec = next((k, s) for k, s in benches["rc_filter"]["params"].items()
                      if s["min"] is not None)
    bad = client.post("/api/spice/run", json={"key": "rc_filter",
                                              "params": {name: spec["min"] - abs(spec["min"]) - 1}})
    assert bad.status_code == 400
    assert bad.json()["detail"]


def test_spice_run_errors_become_400(client, monkeypatch):
    """BenchParamError/SpiceError from run_bench → HTTP 400 with the message (fake bench module)."""
    from piforge.core.errors import PiForgeError

    class BenchParamError(PiForgeError):
        pass

    def run_bench(key, **params):
        raise BenchParamError(f"r={params.get('r')} Ω is below the minimum 1 Ω")

    fake = types.SimpleNamespace(BENCHES={"rc_filter": object()}, run_bench=run_bench,
                                 BenchParamError=BenchParamError)
    monkeypatch.setattr("piforge.server.api_spice._load_benches", lambda: fake)
    r = client.post("/api/spice/run", json={"key": "rc_filter", "params": {"r": 0}})
    assert r.status_code == 400
    assert "below the minimum" in r.json()["detail"]


def test_spice_run_bad_body_422(client):
    assert client.post("/api/spice/run", json={"params": {}}).status_code == 422


def test_spice_run_unknown_param_400(client):
    """Unknown names are rejected before run_bench — "timeout" must not become its keyword."""
    pytest.importorskip("piforge.spice.benches")
    for name in ("timeout", "v_stp"):
        r = client.post("/api/spice/run", json={"key": "rc_filter", "params": {name: 0.001}})
        assert r.status_code == 400, r.text
        assert f"unknown parameter '{name}'" in r.json()["detail"]
    assert "did you mean v_step" in client.post("/api/spice/run", json={
        "key": "rc_filter", "params": {"v_stp": 1}}).json()["detail"]
    assert "parameters: r, c, analysis, v_step" in client.post("/api/spice/run", json={
        "key": "rc_filter", "params": {"timeout": 1}}).json()["detail"]


# ------------------------------------------------------------------------------- twin
def test_twin_devices(client):
    d = client.get("/api/twin/devices").json()
    assert d["firmware"] == "firmware/main.py" and d["firmware_exists"] is True
    assert [x["id"] for x in d["devices"]] == ["SW1", "D1", "SERVO1"]
    assert [s["name"] for s in d["scenarios"]] == ["button_lights_led"]
    if d["available"]:
        sw = d["devices"][0]
        assert "pressed" in sw["inputs"]
        assert sw["inputs"]["pressed"]["widget"] == "momentary"  # GUI hint from the device catalogue
        assert sw["label"] == "Push button" and d["types"]["servo"]["label"] == "Servo"
        assert "button" in d["types"] and "led" in d["types"]
        led = d["devices"][1]
        assert "brightness" in led["outputs"] and led["display"] is False
        assert d["types"]["ssd1306"]["display"] is True and d["types"]["lcd1602_pcf8574"]["display"] is True
        assert d["types"]["rotary_encoder"]["inputs"]["steps"]["widget"] == "stepper"


def test_twin_devices_splitflap_contract(fixture_project):
    """What the GUI's split-flap widget and Amount control read from ``/api/twin/devices``:
    ``params.position``/``comma_after`` pass through; ``splitflap`` outputs digit/next_digit/flip/hall/
    angle, ``ws_feed`` inputs amount/online and outputs clients/port (money-counter spec §2)."""
    import json

    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    types_mod = pytest.importorskip("piforge.twin.devices")
    if not {"splitflap", "ws_feed"} <= set(getattr(types_mod, "DEVICE_TYPES", {})):
        pytest.skip("splitflap/ws_feed twin devices not available yet")
    cfg = {"board": "rpizero2w", "pulls": {}, "devices": [
        {"id": "F2", "type": "splitflap", "pins": {}, "bus": None, "params": {"stepper": "M2", "position": 1}},
        {"id": "F1", "type": "splitflap", "pins": {}, "bus": None,
         "params": {"stepper": "M1", "position": 0, "comma_after": 0}},
        {"id": "WS1", "type": "ws_feed", "pins": {}, "bus": None, "params": {"port": 0}}]}
    (fixture_project / "build" / "twin" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    with TestClient(create_app(fixture_project)) as c:
        d = c.get("/api/twin/devices").json()
    by_id = {x["id"]: x for x in d["devices"]}
    assert by_id["F1"]["params"] == {"stepper": "M1", "position": 0, "comma_after": 0}
    assert by_id["F2"]["params"]["position"] == 1
    assert {"angle", "digit", "next_digit", "flip", "hall"} <= set(by_id["F1"]["outputs"])
    assert {"amount", "online"} <= set(by_id["WS1"]["inputs"])
    assert {"clients", "port"} <= set(by_id["WS1"]["outputs"])
    assert by_id["WS1"]["inputs"]["amount"]["type"] == "float" and by_id["WS1"]["inputs"]["online"]["type"] == "bool"


def test_twin_scenarios_run(client):
    pytest.importorskip("piforge.twin.scenario")
    r = client.post("/api/twin/scenarios/run", json={})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert set(rep) >= {"title", "ok", "counts", "findings"}
    assert rep["ok"] is True, rep
    assert any(f["code"] == "TWIN.SCENARIO_OK" for f in rep["findings"])
    assert all(f["source"] == "twin:button_lights_led" for f in rep["findings"])


def test_twin_scenarios_unknown_name_404(client):
    pytest.importorskip("piforge.twin.scenario")
    r = client.post("/api/twin/scenarios/run", json={"names": ["nope"]})
    assert r.status_code == 404


# ------------------------------------------------------------------------------- rebuild
def test_build_status_idle(client):
    st = client.get("/api/build/status").json()
    assert st["state"] == "idle" and st["error"] is None


def test_rebuild_single_flight(client, app):
    gate = threading.Event()
    calls: list[tuple] = []

    def fake_build(project_dir, *, out_dir=None, progress=None, **kw):
        calls.append((project_dir, out_dir))
        if progress:
            progress("parts")
        gate.wait(10)

    app.state.piforge.build_fn = fake_build
    r1 = client.post("/api/build")
    assert r1.status_code == 202 and r1.json()["started"] is True
    r2 = client.post("/api/build")
    assert r2.json()["started"] is False
    assert client.get("/api/build/status").json()["state"] == "running"
    gate.set()
    st = _wait_status(client, {"done", "failed"})
    assert st["state"] == "done", st
    assert len(calls) == 1
    assert Path(calls[0][1]) == app.state.piforge.build_dir
    assert "parts" in st["log"]


def test_rebuild_failure_reported(client, app):
    def broken_build(project_dir, **kw):
        raise RuntimeError("project.py line 12: NameError: name 'pi' is not defined")

    app.state.piforge.build_fn = broken_build
    client.post("/api/build")
    st = _wait_status(client, {"done", "failed"})
    assert st["state"] == "failed"
    assert "NameError" in st["error"]


@pytest.mark.timeout(60)
def test_events_ws_reports_build(client, app):
    app.state.piforge.build_fn = lambda project_dir, **kw: time.sleep(0.2)
    with client.websocket_connect("/ws/events") as ws:
        client.post("/api/build")
        seen = []
        while not seen or seen[-1] not in ("done", "failed"):
            msg = ws.receive_json()
            if msg.get("op") == "build":
                seen.append(msg["state"])
        assert seen == ["started", "done"]


def test_build_on_start_uses_lazy_build_project(fixture_project: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from piforge.server.app import create_app

    calls = []

    def build_project(project_dir, *, out_dir=None, progress=None, **kw):
        calls.append((Path(project_dir), Path(out_dir)))

    monkeypatch.setitem(sys.modules, "piforge.build", types.SimpleNamespace(build_project=build_project))
    with TestClient(create_app(fixture_project, build_on_start=True)) as c:
        st = _wait_status(c, {"done", "failed"})
    assert st["state"] == "done", st
    assert calls == [(fixture_project.resolve(), (fixture_project / "build").resolve())]


def test_spice_run_unexpected_errors_become_500(client, monkeypatch):
    """TypeError/ValueError from a bench are bugs, not bad input: 500, not 400."""
    for exc in (TypeError("boom"), ValueError("kaboom")):
        def run_bench(key, _e=exc, **params):
            raise _e

        fake = types.SimpleNamespace(BENCHES={"rc_filter": object()}, run_bench=run_bench)
        monkeypatch.setattr("piforge.server.api_spice._load_benches", lambda f=fake: f)
        r = client.post("/api/spice/run", json={"key": "rc_filter", "params": {}})
        assert r.status_code == 500, r.text
        assert type(exc).__name__ in r.json()["detail"]


def test_overhang_bed_fillets_are_not_bridges(tmp_path: Path):
    """30x20x10 box with 1 mm bottom fillets: fillet faces, no bridges, no overhang."""
    import numpy as np
    from piforge.server.overhang import compute_overhang

    # a 30x20x10 box whose bottom ring is inset by 1 mm over 0.5 mm of height (63 deg from vertical)
    pts = []
    for x, y, z, inset in [(15, 10, 0, 1), (15, 10, 0.5, 0), (15, 10, 10, 0)]:
        for sx in (-1, 1):
            for sy in (-1, 1):
                pts.append((sx * (x - inset), sy * (y - inset), z))
    hull = trimesh.convex.convex_hull(np.array(pts))
    glb = tmp_path / "chamfer.glb"
    scene = trimesh.Scene()
    scene.add_geometry(hull, node_name="b_0", geom_name="b_0")
    glb.write_bytes(scene.export(file_type="glb"))
    res = compute_overhang(glb, None, 45.0, 10.0, 0.2)
    assert res["bridge_count"] == 0 and res["overhang_count"] == 0
    assert res["fillet_count"] > 0 and res["fillet_area_mm2"] > 0
    assert not any(a and b for a, b in zip(res["fillet_mask"], res["bridge_mask"], strict=True))
