"""Command line interface (``piforge.cli``) via typer's CliRunner and real subprocesses."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import trimesh
from typer.testing import CliRunner

from piforge.cli import app

from .conftest import FIRMWARE, REPO_ROOT, elec_source, write_project

runner = CliRunner()


def _run(*args: str):
    return runner.invoke(app, [str(a) for a in args], catch_exceptions=False)


# ------------------------------------------------------------------------------ info
def test_info_boards_lists_rpi5():
    res = _run("info", "boards")
    assert res.exit_code == 0, res.output
    assert "rpi5" in res.output and "rpi4b" in res.output and "rpizero2w" in res.output


@pytest.mark.parametrize("topic, expect", [
    ("printers", "bambu_p1s"), ("materials", "PETG"), ("benches", "rc_filter"), ("devices", "hcsr04"),
    ("parts", "pushbutton"), ("modules", "module"),
])
def test_info_topics(topic, expect):
    res = _run("info", topic)
    assert res.exit_code == 0, res.output
    assert expect in res.output


def test_info_unknown_topic_suggests():
    res = _run("info", "printer")
    assert res.exit_code == 2
    assert "printers" in res.output


def test_info_printers_does_not_import_cad_kernel():
    code = ("import sys\nfrom piforge.cli import main\nsys.argv = ['piforge', 'info', 'printers']\n"
            "try:\n    main()\nexcept SystemExit as exc:\n    rc = exc.code\nelse:\n    rc = 0\n"
            "bad = [m for m in ('build123d', 'OCP') if m in sys.modules]\n"
            "print('RESULT', rc, bad or 'NOCAD')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-2000:]
    assert "prusa_mk4" in res.stdout
    assert res.stdout.strip().splitlines()[-1] == "RESULT 0 NOCAD"


def test_python_dash_m_piforge():
    res = subprocess.run([sys.executable, "-m", "piforge", "info", "materials"], capture_output=True,
                         text=True, timeout=300, cwd=REPO_ROOT)
    assert res.returncode == 0, res.stderr[-2000:]
    assert "PETG" in res.stdout and "TPU95A" in res.stdout


# ------------------------------------------------------------------------------ check / build
def test_check_exit_codes(elec_project, hcsr04_project):
    good = _run("check", elec_project)
    assert good.exit_code == 0, good.output
    bad = _run("check", hcsr04_project)
    assert bad.exit_code == 1, bad.output
    assert "ERC.LEVEL_MISMATCH" in bad.output and "GPIO24" in bad.output


def test_check_missing_project_dir(spaced_tmp):
    res = _run("check", spaced_tmp / "does not exist")
    assert res.exit_code == 1
    assert "project.py" in res.output


def test_build_command_electronics_project(elec_project):
    res = _run("build", elec_project, "--no-render", "--no-spice")
    assert res.exit_code == 0, res.output
    assert (elec_project / "build" / "manifest.json").is_file()
    assert "error" in res.output.lower() and "build" in res.output.lower()


def test_build_command_exit_code_on_errors(hcsr04_project):
    res = _run("build", hcsr04_project, "--no-render", "--no-spice")
    assert res.exit_code == 1
    assert "ERC.LEVEL_MISMATCH" in res.output


def test_build_rejects_unknown_format(elec_project):
    res = _run("build", elec_project, "--formats", "stl,obj")
    assert res.exit_code == 2
    assert "obj" in res.output


# ------------------------------------------------------------------------------ spice
@pytest.mark.spice
def test_spice_rc_filter_prints_f3db(ngspice_path, spaced_tmp):
    plot = spaced_tmp / "rc plot.png"
    res = _run("spice", "rc_filter", "-p", "r=1000", "--plot", plot)
    assert res.exit_code == 0, res.output
    assert "f_3db" in res.output and "159" in res.output  # 1 / (2π · 1 kΩ · 1 µF) = 159.2 Hz
    assert plot.stat().st_size > 10_000


def test_spice_bad_parameter_is_a_usage_error():
    res = _run("spice", "rc_filter", "-p", "r=0")
    assert res.exit_code == 2
    assert "r" in res.output and "outside" in res.output
    res = _run("spice", "rc_filter", "-p", "r1000")
    assert res.exit_code == 2 and "key=value" in res.output
    res = _run("spice", "rc_filtr")
    assert res.exit_code == 2 and "rc_filter" in res.output


# ------------------------------------------------------------------------------ render
def _box_stl(path: Path) -> Path:
    trimesh.creation.box(extents=(40, 20, 10)).export(path)
    return path


def test_render_stl(spaced_tmp):
    stl = _box_stl(spaced_tmp / "box ż.stl")
    res = _run("render", stl)
    assert res.exit_code == 0, res.output
    assert (spaced_tmp / "box ż_iso.png").stat().st_size > 2_000
    out = spaced_tmp / "sheet.png"
    res = _run("render", stl, "--sheet", "--out", out)
    assert res.exit_code == 0, res.output
    assert out.stat().st_size > 10_000


def test_render_3mf(spaced_tmp):
    mesh = trimesh.creation.box(extents=(10, 10, 10))
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in mesh.vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}'
             '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
    path = spaced_tmp / "cube.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", model)
    path.write_bytes(buf.getvalue())
    out = spaced_tmp / "cube.png"
    res = _run("render", path, "--view", "top", "--out", out)
    assert res.exit_code == 0, res.output
    assert out.stat().st_size > 1_000


def test_render_bad_inputs(spaced_tmp):
    stl = _box_stl(spaced_tmp / "b.stl")
    assert _run("render", stl, "--view", "sideways").exit_code == 2
    assert _run("render", spaced_tmp / "missing.stl").exit_code == 2
    (spaced_tmp / "notes.txt").write_text("x", encoding="utf-8")
    assert _run("render", spaced_tmp / "notes.txt").exit_code == 2


# ------------------------------------------------------------------------------ new
def test_new_copies_template_and_sets_board(spaced_tmp):
    res = _run("new", "bench_tool", "--dir", spaced_tmp, "--board", "pi5")
    assert res.exit_code == 0, res.output
    root = spaced_tmp / "bench_tool"
    src = (root / "project.py").read_text(encoding="utf-8")
    assert 'BOARD = "rpi5"' in src
    assert (root / "firmware" / "main.py").is_file() and (root / "README.md").is_file()
    assert (root / "README.md").read_text(encoding="utf-8").startswith("# bench_tool")
    assert not (root / "build").exists()
    again = _run("new", "bench_tool", "--dir", spaced_tmp)
    assert again.exit_code == 2 and "exists" in again.output
    assert _run("new", "bad/name", "--dir", spaced_tmp).exit_code == 2
    assert _run("new", "other", "--dir", spaced_tmp, "--board", "arduino").exit_code == 2


@pytest.mark.slow
def test_new_creates_buildable_project(spaced_tmp):
    res = _run("new", "my device", "--dir", spaced_tmp)
    assert res.exit_code == 0, res.output
    root = spaced_tmp / "my device"
    built = _run("build", root, "--no-render", "--no-spice", "--formats", "stl")
    assert built.exit_code == 0, built.output
    manifest = json.loads((root / "build" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "my device"
    assert manifest["files"]["parts"] and all(p.endswith((".stl", ".json")) for p in manifest["files"]["parts"])
    printed = _run("print", root)
    assert printed.exit_code == 0, printed.output
    assert "pi_plate" in printed.output and "g" in printed.output
    assert _run("print", root, "--part", "pi_plat").exit_code == 2  # unknown part → suggestion
    rendered = _run("render", root, "--view", "top")
    assert rendered.exit_code == 0, rendered.output
    assert (root / "render_top.png").stat().st_size > 10_000


# ------------------------------------------------------------------------------ twin
def test_twin_test_runs_scenarios(elec_project):
    res = _run("twin", "test", elec_project)
    assert res.exit_code == 0, res.output
    assert "press_lights_led" in res.output and "PASS" in res.output


def test_twin_run_streams_firmware_log(elec_project):
    res = _run("twin", "run", elec_project, "--duration", "1.5")
    assert res.exit_code == 0, res.output
    assert "fixture firmware ready" in res.output


def test_twin_run_reports_firmware_crash(spaced_tmp):
    root = write_project(spaced_tmp / "crash", elec_source(), {
        "firmware/main.py": "print('starting', flush=True)\nraise RuntimeError('sensor not found')\n"})
    res = _run("twin", "run", root, "--duration", "3")
    assert res.exit_code == 1
    assert "starting" in res.output and "sensor not found" in res.output


def test_twin_test_failing_scenario(spaced_tmp):
    extra = ('p.scenario(Scenario("never_pressed", duration=1.0, steps=[\n'
             '    Step(at=0.3, action="expect", device="D1", prop="brightness", value=1.0, tol=0.01)]))')
    root = write_project(spaced_tmp / "fails", elec_source(extra), {"firmware/main.py": FIRMWARE})
    res = _run("twin", "test", root, "-s", "never_pressed")
    assert res.exit_code == 1
    assert "FAIL" in res.output and "never_pressed" in res.output and "TWIN.EXPECT_FAILED" in res.output
    assert _run("twin", "test", root, "-s", "nope").exit_code == 2


def test_twin_needs_firmware(hcsr04_project):
    res = _run("twin", "run", hcsr04_project, "--duration", "1")
    assert res.exit_code == 1
    assert "firmware" in res.output.lower()


# ------------------------------------------------------------------------------ serve
def test_serve_calls_server_lazily(monkeypatch, elec_project):
    import piforge.server.app as server_app

    calls = []
    monkeypatch.setattr(server_app, "serve", lambda project_dir, **kw: calls.append((project_dir, kw)))
    res = _run("serve", elec_project, "--port", "9999", "--no-browser")
    assert res.exit_code == 0, res.output
    assert calls == [(elec_project.resolve(), {"port": 9999, "open_browser": False})]


def test_serve_without_server_package(monkeypatch, elec_project):
    monkeypatch.setitem(sys.modules, "piforge.server.app", None)  # import → ImportError
    res = _run("serve", elec_project, "--no-browser")
    assert res.exit_code == 1
    assert "server" in res.output.lower()
