"""Build pipeline (``piforge.build``): layout contract, report merging, failure isolation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from piforge.core.errors import PiForgeError
from piforge.core.report import Report

from .conftest import FIRMWARE, MECH_PROJECT, elec_source, write_project

ELEC_FILES = ("circuit.json", "bom.csv", "bom.md", "wiring.json", "wiring.md", "wiring.svg", "wiring.png",
              "config.txt", "netlist.net", "power.json", "pinout.md")
DURATION_KEYS = {"load", "elec", "parts", "scene", "render", "spice", "twin"}
SOURCE_RE = re.compile(r"^(erc|power|assembly|thermal|thermal:.+|project|print:.+|check:.+|spice:.+|twin:.+)$")
INDEX_KEYS = {"name", "material", "color", "quantity", "files", "print_rotation", "analysis", "node_ids"}


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _codes(report: Report) -> set[str]:
    return {f.code for f in report.findings}


# ------------------------------------------------------------------------------ check_project
def test_check_project_flags_5v_echo(hcsr04_project):
    from piforge.build import check_project

    rep = check_project(hcsr04_project)
    assert rep.title == "check"
    hits = rep.by_code("ERC.LEVEL_MISMATCH")
    assert hits and hits[0].severity.name == "ERROR" and hits[0].source == "erc"
    assert not rep.ok


def test_check_project_clean_project_is_ok(elec_project):
    from piforge.build import check_project

    rep = check_project(elec_project)
    assert rep.ok, rep.to_markdown()
    assert {f.source for f in rep.findings} <= {"erc", "power", "project"} | {
        f.source for f in rep.findings if f.source.startswith("spice:")}
    assert not (elec_project / "build").exists()  # check never writes files


def test_check_of_electronics_project_does_not_load_cad_kernel(elec_project):
    code = ("import sys\nfrom pathlib import Path\nfrom piforge.build import check_project\n"
            f"rep = check_project(Path({str(elec_project)!r}))\n"
            "print('OK' if rep.ok else 'FAIL', 'CAD' if 'build123d' in sys.modules else 'NOCAD')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-2000:]
    assert res.stdout.split()[-2:] == ["OK", "NOCAD"]


# ------------------------------------------------------------------------------ build (fast)
def test_build_electronics_project(elec_project):
    from piforge.build import BuildResult, build_project

    messages: list[str] = []
    res = build_project(elec_project, progress=messages.append, scenarios=False)
    assert isinstance(res, BuildResult)
    out = elec_project / "build"
    assert res.out_dir == out.resolve() and res.report.ok, res.report.to_markdown()
    for name in ELEC_FILES:
        assert (out / "elec" / name).is_file(), name
    assert (out / "elec" / "wiring.png").stat().st_size > 10_000
    rows = _json(out / "elec" / "wiring.json")
    assert {"net", "a_ref", "a_pin", "a_phys", "b_ref", "b_pin", "b_phys", "color", "signal"} <= set(rows[0])
    assert any(r["a_phys"] == "11" and r["b_ref"] == "SW1" for r in rows)
    pinout = (out / "elec" / "pinout.md").read_text(encoding="utf-8")
    assert "GPIO17" in pinout and "SW1" in pinout and "pull-up" in pinout
    power = _json(out / "elec" / "power.json")
    assert "5V" in power["rails"] and power["report"]["title"] == "power"
    assert _json(out / "elec" / "circuit.json")["parts"][0]["ref"] == "U1"
    assert "(export" in (out / "elec" / "netlist.net").read_text(encoding="utf-8")

    scene = _json(out / "scene.json")
    assert scene["nodes"] == [] and scene["units"] == "mm" and scene["up"] == "Z"
    assert _json(out / "parts" / "index.json") == []
    twin = _json(out / "twin" / "config.json")
    assert {d["id"]: d["pins"] for d in twin["devices"]} == {"SW1": {"pin": 17}, "D1": {"pin": 27}}
    scen = _json(out / "twin" / "scenarios.json")
    assert scen[0]["name"] == "press_lights_led" and len(scen[0]["steps"]) == 3
    assert not (out / "twin" / "results.json").exists()  # scenarios not requested

    manifest = _json(out / "manifest.json")
    assert manifest["name"] == "elec project" and manifest["firmware"] == "firmware/main.py"
    assert manifest["project_dir"] == str(elec_project.resolve())
    assert manifest["piforge_version"] == "0.1.0" and manifest["board"] == "rpi4b"
    assert (manifest["printer"], manifest["material"]) == ("prusa_mk4", "PETG")
    assert DURATION_KEYS <= set(manifest["durations_s"])
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", manifest["built_at"])
    for group in ("parts", "renders", "elec", "sim", "twin"):
        for rel in manifest["files"][group]:
            assert (out / rel).is_file(), rel
    assert "elec/bom.csv" in manifest["files"]["elec"] and "twin/config.json" in manifest["files"]["twin"]
    assert res.manifest == manifest and DURATION_KEYS <= set(res.durations_s)

    data = _json(out / "report.json")
    assert data["title"] == "build" and Report.from_dict(data).to_dict() == data
    assert (out / "report.md").read_text(encoding="utf-8").startswith("## build")
    assert all(SOURCE_RE.match(f["source"]) for f in data["findings"])
    assert any("elec" in m for m in messages) and any("twin" in m for m in messages)


def test_subsystem_failures_become_findings(spaced_tmp):
    extra = """
def boom():
    raise RuntimeError("check exploded")

def failing_check():
    from piforge.core.report import Report
    r = Report("anything")
    r.add("MY.PROBLEM", "error", "Something is wrong.")
    return r

p.add_check(boom)
p.add_check(failing_check)
p.add_check(lambda: "not a report")
"""
    root = write_project(spaced_tmp / "flaky", elec_source(extra), {"firmware/main.py": "print('x')\n"})
    from piforge.build import build_project

    res = build_project(root, render=False, spice=False)
    rep = res.report
    boom = [f for f in rep.findings if f.source == "project" and "check exploded" in f.message]
    assert boom and boom[0].severity.name == "ERROR" and "RuntimeError" in boom[0].message
    mine = rep.by_code("MY.PROBLEM")
    assert mine and mine[0].source == "check:failing_check"
    assert any(f.source == "project" and "Report" in f.message for f in rep.errors)  # lambda result
    assert (root / "build" / "elec" / "bom.csv").is_file()  # the build went on
    assert not rep.ok


def test_ngspice_missing_is_an_info(monkeypatch, spaced_tmp):
    import piforge.spice.runner as runner

    root = write_project(spaced_tmp / "spice", elec_source('p.spice("led_driver", label="led")'),
                         {"firmware/main.py": "print('x')\n"})
    monkeypatch.setattr(runner, "find_ngspice", lambda: None)
    from piforge.build import build_project

    res = build_project(root, render=False)
    assert res.report.ok, res.report.to_markdown()
    info = [f for f in res.report.findings if f.source == "project" and "ngspice" in f.message.lower()]
    assert info and info[0].severity.name == "INFO"
    assert _json(root / "build" / "sim" / "index.json") == []


def test_spice_failure_is_a_finding(monkeypatch, spaced_tmp):
    """Non-convergence (SpiceError) must not abort the build (Review Focus #5)."""
    import piforge.spice as spice_pkg
    import piforge.spice.runner as runner
    from piforge.spice import SpiceError

    def explode(key, **params):
        raise SpiceError("timestep too small at t=1.2e-06 s", log="...")

    root = write_project(spaced_tmp / "diverges", elec_source('p.spice("rc_filter", label="rc", r=1000)'),
                         {"firmware/main.py": "print('x')\n"})
    monkeypatch.setattr(runner, "find_ngspice", lambda: "/usr/bin/true")
    monkeypatch.setattr(spice_pkg, "run_bench", explode)
    from piforge.build import build_project

    res = build_project(root, render=False)
    hits = res.report.by_code("PROJECT.SPICE_FAILED")
    assert hits and hits[0].source == "project" and "timestep too small" in hits[0].message
    index = _json(root / "build" / "sim" / "index.json")
    assert index[0]["label"] == "rc" and index[0]["ok"] is False and "SpiceError" in index[0]["error"]
    assert (root / "build" / "twin" / "config.json").is_file()  # later stages still ran


@pytest.mark.spice
def test_spice_bench_outputs(ngspice_path, spaced_tmp):
    root = write_project(spaced_tmp / "spice ok", elec_source(
        'p.spice("led_driver", label="status led", r_series=330, led="led_red")'),
        {"firmware/main.py": "print('x')\n"})
    from piforge.build import build_project

    res = build_project(root, render=False)
    sim = root / "build" / "sim"
    index = _json(sim / "index.json")
    assert [r["label"] for r in index] == ["status_led"] and index[0]["bench"] == "led_driver"
    assert set(index[0]) >= {"label", "bench", "params", "measures", "analytic", "ok"} and index[0]["ok"]
    assert index[0]["measures"]["i_led"] == pytest.approx(3.9e-3, rel=0.15)
    full = _json(sim / "status_led.json")
    assert full["key"] == "led_driver" and full["traces"] and full["x_label"] in full["traces"]
    assert (sim / "status_led.png").stat().st_size > 10_000
    assert any(f.source == "spice:status_led" for f in res.report.findings)
    assert "sim/status_led.png" in res.manifest["files"]["sim"]


def test_rebuild_replaces_old_outputs(elec_project):
    from piforge.build import build_project

    build_project(elec_project, render=False, spice=False)
    stale = elec_project / "build" / "stale_artifact.txt"
    stale.write_text("old", encoding="utf-8")
    build_project(elec_project, render=False, spice=False)
    assert not stale.exists() and (elec_project / "build" / "manifest.json").is_file()
    assert not [p for p in elec_project.iterdir() if p.name.startswith(".build")]  # no staging left


def test_build_refuses_foreign_out_dir(elec_project, spaced_tmp):
    from piforge.build import build_project

    foreign = spaced_tmp / "my documents"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(PiForgeError, match="manifest"):
        build_project(elec_project, out_dir=foreign, render=False, spice=False)
    assert (foreign / "precious.txt").read_text(encoding="utf-8") == "keep me"


def test_build_into_explicit_out_dir(elec_project, spaced_tmp):
    from piforge.build import build_project

    out = spaced_tmp / "elsewhere" / "out ż"
    res = build_project(elec_project, out_dir=out, render=False, spice=False)
    assert res.out_dir == out.resolve() and (out / "manifest.json").is_file()
    assert not (elec_project / "build").exists()


def test_broken_project_aborts_with_project_load_error(spaced_tmp):
    from piforge.build import build_project
    from piforge.project import ProjectLoadError

    root = write_project(spaced_tmp / "broken", "def build(p):\n    raise ValueError('nope')\n")
    with pytest.raises(ProjectLoadError, match="nope"):
        build_project(root)
    assert not (root / "build").exists()


def test_unknown_format_is_rejected_before_loading(spaced_tmp):
    from piforge.build import build_project
    from piforge.core.errors import ValidationError

    with pytest.raises(ValidationError, match="obj"):
        build_project(spaced_tmp / "not even a project", formats=("stl", "obj"))


def test_build_refuses_project_dir_as_out_dir(elec_project):
    from piforge.build import build_project

    with pytest.raises(PiForgeError):
        build_project(elec_project, out_dir=elec_project, render=False, spice=False)
    with pytest.raises(PiForgeError):
        build_project(elec_project, out_dir=elec_project.parent, render=False, spice=False)
    assert (elec_project / "project.py").is_file() and (elec_project / "firmware" / "main.py").is_file()


def test_rebuild_into_existing_build_dir_keeps_working(elec_project):
    """The GUI server rebuilds into the existing build dir (out_dir given explicitly)."""
    from piforge.build import build_project

    first = build_project(elec_project, render=False, spice=False)
    second = build_project(elec_project, out_dir=first.out_dir, render=False, spice=False)
    assert second.out_dir == first.out_dir and (first.out_dir / "report.json").is_file()


def test_firmware_syntax_error_is_reported(spaced_tmp):
    from piforge.build import build_project

    root = write_project(spaced_tmp / "bad firmware", elec_source(),
                         {"firmware/main.py": "print('a')\nif True\n    print('b')\n"})
    res = build_project(root, render=False, spice=False)
    hits = res.report.by_code("PROJECT.FIRMWARE_SYNTAX")
    assert hits and hits[0].severity.name == "ERROR" and hits[0].source == "project"
    assert "line 2" in hits[0].message and (root / "build" / "twin" / "config.json").is_file()


def test_build_runs_scenarios(elec_project):
    from piforge.build import build_project

    res = build_project(elec_project, render=False, spice=False, scenarios=True)
    assert res.report.ok, res.report.to_markdown()
    results = _json(elec_project / "build" / "twin" / "results.json")
    assert results["ok"] is True and results["title"] == "twin scenarios"
    assert [(s["name"], s["ok"]) for s in results["scenarios"]] == [("press_lights_led", True)]
    ok = [f for f in res.report.findings if f.code == "TWIN.SCENARIO_OK"]
    assert ok and ok[0].source == "twin:press_lights_led"
    assert "twin/results.json" in res.manifest["files"]["twin"]


def test_scenarios_not_run_are_mentioned(elec_project):
    from piforge.build import build_project

    res = build_project(elec_project, render=False, spice=False)
    assert res.report.has("PROJECT.SCENARIOS_NOT_RUN", "info")


def test_thermal_output(spaced_tmp):
    from piforge.build import build_project

    extra = ("from piforge.analysis.thermal import ThermalInputs\n"
             "p.thermal(ThermalInputs(power_w=3.0, outer_mm=(110, 80, 40), vent_in_mm2=400, "
             "vent_out_mm2=400, vent_height_mm=25))")
    root = write_project(spaced_tmp / "warm", elec_source(extra), {"firmware/main.py": "print('x')\n"})
    res = build_project(root, render=False, spice=False)
    thermal = _json(root / "build" / "thermal.json")
    assert 25.0 < thermal["internal_c"] < 60.0 and thermal["report"]["findings"]
    assert any(f.source == "thermal" for f in res.report.findings)


def test_thermal_output_multiple_cases(spaced_tmp):
    from piforge.build import build_project

    extra = ("from piforge.analysis.thermal import ThermalInputs\n"
             "inp = dict(power_w=3.0, outer_mm=(110, 80, 40), vent_in_mm2=400, vent_out_mm2=400, vent_height_mm=25)\n"
             "p.thermal(ThermalInputs(**inp), label='fan off')\n"
             "p.thermal(ThermalInputs(**inp, fan_cfm=3.0), label='fan on')")
    root = write_project(spaced_tmp / "warm2", elec_source(extra), {"firmware/main.py": "print('x')\n"})
    res = build_project(root, render=False, spice=False)
    thermal = _json(root / "build" / "thermal.json")
    assert set(thermal) == {"fan off", "fan on"}
    assert thermal["fan on"]["internal_c"] < thermal["fan off"]["internal_c"]
    sources = {f.source for f in res.report.findings}
    assert {"thermal:fan off", "thermal:fan on"} <= sources and "thermal" not in sources


def test_twin_override_of_unknown_device_is_a_finding(spaced_tmp):
    from piforge.build import build_project

    root = write_project(spaced_tmp / "override", elec_source('p.twin_override("D9", color="blue")'),
                         {"firmware/main.py": "print('x')\n"})
    res = build_project(root, render=False, spice=False)
    hits = [f for f in res.report.errors if f.source == "project" and "D9" in f.message]
    assert hits, res.report.to_markdown()
    assert (root / "build" / "elec" / "bom.csv").is_file()


def test_check_flags_scenario_with_unknown_device(spaced_tmp):
    from piforge.build import check_project

    extra = ('p.scenario(Scenario("typo", duration=1.0, steps=[\n'
             '    Step(at=0.2, action="input", device="SW9", prop="pressed", value=True)]))')
    root = write_project(spaced_tmp / "typo", elec_source(extra), {"firmware/main.py": FIRMWARE})
    rep = check_project(root)
    hits = rep.by_code("PROJECT.SCENARIO_INVALID")
    assert hits and hits[0].severity.name == "ERROR" and "SW9" in hits[0].message


def test_euler_angles_round_trip():
    from piforge.build import euler_xyz
    from piforge.fab.meshutil import rotation_matrix

    rng = np.random.default_rng(1)
    for angles in [(0, 0, 0), (90, 0, 0), (180, 0, 0), (0, 90, 0), (0, -90, 0), (30, 45, 60)] + [
            tuple(rng.uniform(-180, 180, 3)) for _ in range(20)]:
        r = rotation_matrix(*angles)
        assert np.allclose(rotation_matrix(*euler_xyz(r)), r, atol=1e-9), angles


# ------------------------------------------------------------------------------ mechanics (slow)
@pytest.mark.slow
def test_parts_export_analysis_and_placement(spaced_tmp):
    from piforge.build import build_project
    from piforge.fab.meshutil import rotation_matrix

    root = write_project(spaced_tmp / "mech ż", MECH_PROJECT)
    res = build_project(root, spice=False, formats=("stl",))
    out, rep = res.out_dir, res.report
    index = {p["name"]: p for p in _json(out / "parts" / "index.json")}
    assert set(index) == {"box", "spare"}  # ghost failed and is not listed
    for entry in index.values():
        assert set(entry) == INDEX_KEYS and set(entry["files"]) == {"stl"}
        assert (out / entry["files"]["stl"]).is_file()
    box, spare = index["box"], index["spare"]
    assert box["node_ids"] == ["box"] and spare["node_ids"] == []
    assert box["print_rotation"] == [0.0, 0.0, 0.0] and box["color"] == "#22aa55"
    assert box["analysis"]["watertight"] and box["analysis"]["fits_bed"]
    assert spare["material"] == "PETG" and len(spare["print_rotation"]) == 3
    r = rotation_matrix(*spare["print_rotation"])
    assert np.allclose(r, np.asarray(spare["analysis"]["rotation"]), atol=1e-6)

    failed = rep.by_code("PROJECT.PART_FAILED")
    assert failed and failed[0].severity.name == "ERROR" and "ghost" in failed[0].message
    assert failed[0].source == "project"
    assert any("spare" in f.message for f in rep.by_code("PROJECT.PART_NOT_PLACED"))
    not_exported = rep.by_code("PROJECT.PART_NOT_EXPORTED")
    assert not_exported and not_exported[0].severity.name == "WARNING" and "loose" in not_exported[0].message
    assert {"print:box", "print:spare", "assembly"} <= {f.source for f in rep.findings}

    scene = _json(out / "scene.json")
    assert [n["id"] for n in scene["nodes"]] == ["box", "loose"]
    for rel in ("renders/assembly_iso.png", "renders/assembly_sheet.png", "renders/part_box.png",
                "renders/part_spare.png"):
        assert (out / rel).is_file(), rel
    assert not (out / "renders" / "part_ghost.png").exists()


# ------------------------------------------------------------------------------ template (slow)
@pytest.mark.slow
def test_template_builds_without_errors(template_build):
    rep = template_build.report
    assert rep.ok, rep.to_markdown()
    assert all(SOURCE_RE.match(f.source) for f in rep.findings), {f.source for f in rep.findings}
    sources = {f.source for f in rep.findings}
    assert {"erc", "power", "assembly", "print:pi_plate"} <= sources
    assert any(s.startswith("twin:") for s in sources)


@pytest.mark.slow
def test_template_layout_complete(template_build):
    out = template_build.out_dir
    root = template_build.project.root
    assert out == (root / "build").resolve()
    for rel in ["manifest.json", "report.json", "report.md", "scene.json", "parts/index.json",
                "parts/pi_plate.stl", "parts/pi_plate.3mf", "parts/pi_plate.step",
                "renders/assembly_iso.png", "renders/assembly_sheet.png", "renders/part_pi_plate.png",
                "sim/index.json", "twin/config.json", "twin/scenarios.json", "twin/results.json",
                *[f"elec/{n}" for n in ELEC_FILES]]:
        assert (out / rel).is_file(), rel
    from PIL import Image

    for rel, size in (("renders/assembly_iso.png", (1200, 900)), ("renders/assembly_sheet.png", (1600, 1200)),
                      ("renders/part_pi_plate.png", (800, 600))):
        with Image.open(out / rel) as img:  # right size and really drawn (not a blank canvas)
            assert img.size == size and len(img.convert("RGB").getcolors(1 << 20)) > 50, rel

    scene = _json(out / "scene.json")
    nodes = {n["id"]: n for n in scene["nodes"]}
    assert {"plate", "pi", "button", "led"} <= set(nodes)
    for n in scene["nodes"]:
        assert (out / n["mesh"]).is_file() and len(n["matrix"]) == 16
    assert nodes["plate"]["kind"] == "printed" and nodes["pi"]["kind"] == "pcb"
    assert nodes["led"]["emissive_from"]["device"] == "D1"

    index = _json(out / "parts" / "index.json")
    assert len(index) == 1 and set(index[0]) == INDEX_KEYS
    part = index[0]
    assert part["name"] == "pi_plate" and part["material"] == "PETG" and part["quantity"] == 1
    assert part["files"] == {"stl": "parts/pi_plate.stl", "3mf": "parts/pi_plate.3mf",
                             "step": "parts/pi_plate.step"}
    assert part["print_rotation"] == [0.0, 0.0, 0.0] and part["node_ids"] == ["plate"]
    assert part["analysis"]["watertight"] is True and part["analysis"]["fits_bed"] is True
    assert part["analysis"]["report"]["counts"]["error"] == 0

    manifest = _json(out / "manifest.json")
    assert manifest["firmware"] == "firmware/main.py" and manifest["board"] == "rpi4b"
    assert "renders/assembly_sheet.png" in manifest["files"]["renders"]
    assert "parts/pi_plate.stl" in manifest["files"]["parts"]
    for group, rels in manifest["files"].items():
        for rel in rels:
            assert (out / rel).is_file(), (group, rel)
    assert DURATION_KEYS <= set(manifest["durations_s"])
    assert all(v >= 0 for v in manifest["durations_s"].values())

    data = _json(out / "report.json")
    assert Report.from_dict(data).to_dict() == data


@pytest.mark.slow
def test_template_twin_and_scenario_results(template_build):
    out = template_build.out_dir
    cfg = _json(out / "twin" / "config.json")
    assert {d["id"]: (d["type"], d["pins"]) for d in cfg["devices"]} == {
        "SW1": ("button", {"pin": 17}), "D1": ("led", {"pin": 27})}
    results = _json(out / "twin" / "results.json")
    assert results["ok"] is True
    assert [s["name"] for s in results["scenarios"]] == ["button_toggles_led"]
    assert any(f["code"] == "TWIN.SCENARIO_OK" and f["source"] == "twin:button_toggles_led"
               for f in results["findings"])


@pytest.mark.slow
def test_template_build_is_served_by_the_gui_api(template_build):
    """Contract check with Task 10: the web server reads a real build directory."""
    try:
        from fastapi.testclient import TestClient

        from piforge.server.app import create_app
    except ImportError as exc:  # pragma: no cover - server package missing
        pytest.skip(f"piforge.server is not available: {exc}")
    with TestClient(create_app(template_build.project.root)) as client:
        project = client.get("/api/project").json()
        assert project["has_build"] and project["name"] == "template copy" and project["counts"]["error"] == 0
        assert any(u.endswith("/part_pi_plate.png") for u in project["renders"])
        scene = client.get("/api/scene").json()
        assert {n["id"] for n in scene["nodes"]} == {"plate", "pi", "button", "led"}
        assert all(client.get(f"/build/{n['mesh']}").status_code == 200 for n in scene["nodes"])
        parts = client.get("/api/parts").json()
        assert [(p["name"], p["status"]) for p in parts] == [("pi_plate", "ok")]
        assert set(parts[0]["urls"]) == {"stl", "3mf", "step"}
        overhang = client.get("/api/parts/pi_plate/overhang").json()
        assert overhang["node_id"] == "plate" and overhang["face_count"] == len(overhang["mask"]) > 100
        assert overhang["overhang_count"] == 0  # flat plate with vertical standoffs prints support-free
        elec = client.get("/api/elec").json()
        assert elec["available"] and len(elec["wiring"]) == 5 and "5V" in elec["power"]["rails"]
        assert "GPIO17" in elec["pinout_md"] and elec["files"]["wiring_svg"]
        assert client.get("/api/twin/devices").json()["firmware_exists"] is True
        spice = client.get("/api/spice/project").json()
        assert spice[0]["label"] == "status_led" and spice[0]["plot_url"]


@pytest.mark.slow
@pytest.mark.spice
def test_template_spice_results(template_build, ngspice_path):
    index = _json(template_build.out_dir / "sim" / "index.json")
    assert [(r["label"], r["bench"], r["ok"]) for r in index] == [("status_led", "led_driver", True)]
    assert index[0]["measures"]["i_led"] == pytest.approx(3.9e-3, rel=0.15)
    assert (template_build.out_dir / "sim" / "status_led.png").is_file()
