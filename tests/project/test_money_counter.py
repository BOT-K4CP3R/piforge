"""End-to-end build of ``projects/money_counter`` (split-flap money counter) + its firmware unit tests.

The project is copied to a temporary folder (the repo's ``build/`` stays untouched) and built with
renders, SPICE and all twin scenarios: no ERROR or WARNING findings, every scenario passes, the
modules sit in one closed housing whose windows show only the flap faces, the spools are driven by
the twin's split-flap devices and every window has a display face bound to its device.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from piforge.build import build_project
from piforge.project import load_project
from piforge.twin.session import TwinSession

pytestmark = pytest.mark.slow

PROJECT = Path(__file__).resolve().parents[2] / "projects" / "money_counter"
SCENARIOS = {"homing_to_zero", "count_up_1234_56", "last_digit_1234_57", "big_jump_999999_99", "overflow_clamps",
             "invalid_ignored", "feed_reconnect", "rapid_updates", "slip_resync"}


@pytest.fixture(scope="module")
def mc_build(tmp_path_factory: pytest.TempPathFactory):
    dst = tmp_path_factory.mktemp("money ~ counter") / "money_counter"
    shutil.copytree(PROJECT, dst, ignore=shutil.ignore_patterns("build", "__pycache__", ".pytest_cache"))
    return build_project(dst, scenarios=True)


def test_firmware_unit_tests_pass():
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(PROJECT / "firmware" / "tests")],
                         capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]


def test_money_counter_builds_without_errors(mc_build):
    rep = mc_build.report
    assert not rep.errors, "\n".join(str(f) for f in rep.errors)
    assert not rep.warnings, "\n".join(str(f) for f in rep.warnings)
    assert rep.has("MC.CONSISTENT")
    for code in ("HOUSING.WINDOW_CLEAN", "HOUSING.SWING_CLEAR", "HOUSING.BED_FIT"):
        assert rep.has(code), code
    out = mc_build.out_dir
    for rel in ("scene.json", "renders/assembly_iso.png", "elec/wiring.png", "elec/bom.md",
                "parts/flap.stl", "parts/spool.stl", "parts/frame_right.stl", "parts/front_0.stl",
                "parts/floor_0.stl", "parts/top_0.stl", "parts/back_0.stl", "parts/side_left.stl",
                "parts/comma_inlay.stl"):
        assert (out / rel).is_file(), rel
    index = json.loads((out / "parts" / "index.json").read_text(encoding="utf-8"))
    rows = index["parts"] if isinstance(index, dict) else index
    qty = {r["name"]: r.get("quantity") for r in rows}
    assert qty["flap"] == 160 and qty["spool"] == 8 and qty["pillar"] >= 2


def test_money_counter_scenarios_pass(mc_build):
    results = json.loads((mc_build.out_dir / "twin" / "results.json").read_text(encoding="utf-8"))
    by_name = {s["name"]: s for s in results["scenarios"]}
    assert set(by_name) == SCENARIOS
    assert all(s["ok"] for s in by_name.values()), by_name


def test_money_counter_scene_and_twin(mc_build):
    out = mc_build.out_dir
    scene = json.loads((out / "scene.json").read_text(encoding="utf-8"))
    spools = {n["id"]: n for n in scene["nodes"] if n["id"].endswith("_spool")}
    assert len(spools) == 8
    for i in range(8):
        drv = spools[f"m{i}_spool"]["joint"]["driven_by"]
        assert drv["device"] == f"SF{i + 1}" and drv["prop"] == "angle"
    nodes = {n["id"]: n for n in scene["nodes"]}
    for i in range(8):
        assert nodes[f"face_{i}"]["display_from"] == {"device": f"SF{i + 1}", "kind": "splitflap"}
    twin = json.loads((out / "twin" / "config.json").read_text(encoding="utf-8"))
    devs = {d["id"]: d for d in twin["devices"]}
    assert devs["U2"]["type"] == "shift_register_74hc595" and devs["U2"]["params"]["length"] == 4
    assert devs["FEED1"]["type"] == "ws_feed"
    assert sorted(devs[f"SF{i + 1}"]["params"]["position"] for i in range(8)) == list(range(8))
    assert devs["M8"]["params"]["coil_source"] == {"device": "U2", "bits": [28, 29, 30, 31]}
    sim = {row["label"]: row for row in json.loads((out / "sim" / "index.json").read_text(encoding="utf-8"))}
    if sim:
        assert set(sim) == {"coil_flyback", "eight_motors_start"} and all(r["ok"] for r in sim.values())


def test_money_counter_harness(mc_build):
    """Every wire is in the 3D scene: Pi header → perfboard → driver ribbons, motor leads, Hall bus."""
    out = mc_build.out_dir
    rep = mc_build.report
    assert rep.has("WIRE.SUMMARY") and not rep.by_code("WIRE.COLLISION") and not rep.by_code("WIRE.UNROUTED")
    assert len(rep.by_code("WIRE.LEAD_SLACK")) == 8  # every 28BYJ-48 lead reaches its driver
    scene = json.loads((out / "scene.json").read_text(encoding="utf-8"))
    wires = [n for n in scene["nodes"] if n["kind"] == "wire"]
    recs = [n["wire"] for n in wires]
    cables = {r["cable"] for r in recs}
    assert {"W-SPI", "W-PWR", "W-HALLBUS", "28BYJ-48 lead"} | {f"W-DRV{i}" for i in range(1, 9)} <= cables
    conns = {c["id"]: c for c in scene["connectors"]}
    assert conns["U1.19"]["label"] == "GPIO10 / SPI0 MOSI" and conns["U1.19"]["node"] == "board"
    spi = {r["from"]["connector"]: r for r in recs if r["cable"] == "W-SPI"}
    assert spi["U1.19"]["to"]["connector"] == "PB1.JP-1" and spi["U1.19"]["color_name"] == "blue"
    hall_out = [r for r in recs if r["to"]["ref"].startswith("H") and r["to"]["pin"] == "OUT"]
    assert len(hall_out) == 8 and all(r["from"]["ref"] == "U1" and r["color_name"] == "white" for r in hall_out)
    lead = [r for r in recs if r["cable"] == "28BYJ-48 lead"]
    assert len(lead) == 40 and all(r["length_mm"] == 230.0 for r in lead)
    assert all(r["from"]["connector"] in conns and r["to"]["connector"] in conns for r in recs)
    cut = (out / "elec" / "cut_list.csv").read_text(encoding="utf-8").splitlines()
    assert len(cut) == 1 + len(wires)
    assert (out / "elec" / "cut_list.md").is_file()


def test_money_counter_motors_have_the_stall_model(mc_build):
    twin = json.loads((mc_build.out_dir / "twin" / "config.json").read_text(encoding="utf-8"))
    devs = {d["id"]: d for d in twin["devices"]}
    fw = (PROJECT / "firmware" / "config.toml").read_text(encoding="utf-8")
    for i in range(1, 9):
        prm = devs[f"M{i}"]["params"]
        assert prm["stall_model"] is True and prm["max_pps"] == 950.0 and prm["start_pps"] == 500.0
    assert "max_pps = 850" in fw and "start_pps = 450" in fw       # firmware ≈ 10 % below both limits
    results = json.loads((mc_build.out_dir / "twin" / "results.json").read_text(encoding="utf-8"))
    codes = {f["code"] for f in results["findings"]}
    assert "TWIN.STEPPER_LOST_STEPS" not in codes and all(s["counts"]["warning"] == 0 for s in results["scenarios"])


def _wait_log(s: TwinSession, needle: str, timeout: float, start: int = 0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if any(needle in line for line in s.logs()[start:]):
            return True
        time.sleep(0.05)
    return False


def _digits(s: TwinSession) -> list[int]:
    time.sleep(0.5)                                      # flaps finish falling, state is published
    dev = s.latest_state()["devices"]
    return [dev[f"SF{i}"]["digit"] for i in range(1, 9)]


def test_overspeed_is_caught_by_the_closed_loop(tmp_path):
    """Not the shipped config: firmware at 1100 half-steps/s, above the twin motors' 950 pull-out. The
    motors really lose steps; the power-up self-test finds it (RESYNC/DERATE/re-home), and every amount
    afterwards still lands exactly."""
    cfg = (PROJECT / "firmware" / "config.toml").read_text(encoding="utf-8")
    assert "max_pps = 850" in cfg
    fast = tmp_path / "config fast.toml"
    fast.write_text(cfg.replace("max_pps = 850", "max_pps = 1100"), encoding="utf-8")
    p = load_project(PROJECT)
    with TwinSession(p.twin_config(), p.firmware_path, env={"MONEY_COUNTER_CONFIG": str(fast)}) as s:
        assert s.wait_ready(60)
        assert _wait_log(s, "SHOW 000000,00", 60), s.logs()[-20:]
        logs = s.logs()
        lost = [s.latest_state()["devices"][f"M{i}"]["lost_steps"] for i in range(1, 9)]
        assert sum(lost) > 0, "1100 half-steps/s must exceed the twin motors' pull-out"
        assert any(line.startswith("DERATE m") for line in logs)
        assert any(line.startswith("RESYNC m") for line in logs)
        assert sum("SELFTEST" in line and "no lost steps" in line for line in logs) == 8
        assert _digits(s) == [0] * 8
        for amount, text in ((1234.56, "001234,56"), (999999.99, "999999,99"), (5678.12, "005678,12")):
            n = len(s.logs())
            s.send_input("FEED1", "amount", amount)
            assert _wait_log(s, f"SHOW {text}", 15, n), s.logs()[-20:]
            assert _digits(s) == [int(c) for c in text.replace(",", "")]
        assert not any("FAILED" in line for line in s.logs())
