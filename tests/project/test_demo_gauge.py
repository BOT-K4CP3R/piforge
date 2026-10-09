"""End-to-end build of the example project ``projects/demo_gauge`` (all subsystems).

The project is copied to a temporary folder (so the repo's ``build/`` is untouched) and built
with renders, SPICE and the twin scenarios: zero ERROR findings and every scenario passes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from piforge.build import build_project

pytestmark = pytest.mark.slow

DEMO_DIR = Path(__file__).resolve().parents[2] / "projects" / "demo_gauge"
SCENARIOS = {"needle_at_22C", "fan_on_above_28C", "button_cycles_mode", "display_and_status_led"}


@pytest.fixture(scope="module")
def demo_build(tmp_path_factory: pytest.TempPathFactory):
    dst = tmp_path_factory.mktemp("demo ~ gauge") / "demo_gauge"
    shutil.copytree(DEMO_DIR, dst, ignore=shutil.ignore_patterns("build", "__pycache__"))
    return build_project(dst, scenarios=True)


def test_demo_gauge_builds_without_errors(demo_build):
    rep = demo_build.report
    assert not rep.errors, "\n".join(str(f) for f in rep.errors)
    # the only expected warning is the deliberate flyback-off what-if SPICE run
    assert {(f.code, f.source) for f in rep.warnings} <= {("SPICE.FLYBACK_OVERVOLTAGE", "spice:fan_switch_no_flyback")}
    thermal = json.loads((demo_build.out_dir / "thermal.json").read_text(encoding="utf-8"))
    assert set(thermal) == {"fan off", "fan on"}
    assert thermal["fan on"]["internal_c"] < thermal["fan off"]["internal_c"]
    out = demo_build.out_dir
    for rel in ("scene.json", "renders/assembly_iso.png", "elec/wiring.png", "thermal.json",
                "parts/enclosure_base.stl", "parts/enclosure_lid.stl", "parts/dial.stl", "parts/needle.stl"):
        assert (out / rel).is_file(), rel


def test_demo_gauge_scenarios_pass(demo_build):
    if not any(f.code.startswith("TWIN.") for f in demo_build.report):
        pytest.skip("twin scenarios were not run")
    results = json.loads((demo_build.out_dir / "twin" / "results.json").read_text(encoding="utf-8"))
    by_name = {s["name"]: s for s in results["scenarios"]}
    assert set(by_name) == SCENARIOS
    assert all(s["ok"] for s in by_name.values()), by_name


def test_demo_gauge_spice_and_scene(demo_build):
    out = demo_build.out_dir
    sim = {row["label"]: row for row in json.loads((out / "sim" / "index.json").read_text(encoding="utf-8"))}
    if sim:  # ngspice installed
        assert set(sim) == {"status_led", "fan_switch", "fan_switch_no_flyback", "i2c_bus", "servo_stall"}
        assert all(row["ok"] for row in sim.values()), sim
    scene = json.loads((out / "scene.json").read_text(encoding="utf-8"))
    needle = next(n for n in scene["nodes"] if n["id"] == "needle")
    assert needle["joint"]["driven_by"]["device"] == "M1"
    led = next(n for n in scene["nodes"] if n["id"] == "led")
    assert led["emissive_from"]["device"] == "D1"
