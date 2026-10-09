"""Browser end-to-end tests of the PiForge GUI (Playwright + headless Chromium).

One server + one browser page for the whole module (8 GB host): tests run in file order and
restore any state they change. Screenshots of every tab go to ``build/_review/gui/``.
"""

from __future__ import annotations

import importlib.util
import json
import math
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..server.fixture_build import NEEDLE_ANGLE, NODE_IDS, live_server, make_fixture_project, make_leaves

pytestmark = [pytest.mark.gui, pytest.mark.timeout(600)]

READY = "window.piforge && window.piforge.ready === true"


def _spice_ready() -> bool:
    return (importlib.util.find_spec("piforge.spice.benches") is not None
            and (shutil.which("ngspice") is not None or Path("/opt/homebrew/bin/ngspice").exists()))


@pytest.fixture(scope="module")
def gui(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> Iterator[SimpleNamespace]:
    """Fixture project served by uvicorn + one headless Chromium page (1280×800)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from piforge.server.app import create_app

    root = make_fixture_project(tmp_path_factory.mktemp("gui") / "gui project ~ż")
    shots = repo_root / "build" / "_review" / "gui"
    shots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    app = create_app(root)
    with live_server(app) as base, sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(base + "/")
            page.wait_for_function(READY, timeout=120_000)
            yield SimpleNamespace(page=page, base=base, errors=errors, root=root, shots=shots, app=app)
        finally:
            browser.close()


@pytest.fixture
def clean_print_views(gui):
    """Leave no overhang heat-map / bed view behind, even when the test body fails half-way."""
    yield
    page = gui.page
    try:
        for kind, getter in (("overhang", "heat"), ("bed", "bed")):
            part = page.evaluate(f"window.piforge.debug.{getter}()")
            if part:
                page.click(f"[data-testid={kind}-{part}]")
                page.wait_for_function(f"window.piforge.debug.{getter}() === null", timeout=10_000)
        page.click("[data-action=view-iso]")
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the test's own failure
        print(f"clean_print_views: {exc}")


def _node(page, node_id: str) -> dict:
    return page.evaluate("(id) => window.piforge.debug.node(id)", node_id)


def _set_explode(page, percent: int) -> None:
    page.evaluate("(v) => { const e = document.querySelector('#explode'); e.value = String(v);"
                  " e.dispatchEvent(new Event('input')); }", percent)


def _tab(page, name: str) -> None:
    page.click(f"[data-testid=tab-{name}]")
    page.wait_for_selector(f"#tab-{name}:not([hidden])")


def test_page_loads_without_console_errors(gui):
    assert gui.page.evaluate("window.piforge.debug.frames()") > 0
    assert gui.errors == []


def test_scene_contains_all_fixture_meshes(gui):
    page = gui.page
    assert sorted(page.evaluate("window.piforge.debug.meshNodes()")) == sorted(NODE_IDS)
    leaves = make_leaves()
    for nid in NODE_IDS:
        stem = "screw_m2_5" if nid.startswith("screw") else nid
        assert _node(page, nid)["meshes"] == len(leaves[stem]), nid
    assert page.locator("[data-testid^=tree-item-]").count() == len(NODE_IDS)


def test_world_matrices_follow_scene_contract(gui):
    """world = parent_world · matrix · T(o)·R(axis, value)·T(−o) for every node (parents, column-major)."""
    scene = json.loads((gui.root / "build" / "scene.json").read_text(encoding="utf-8"))
    for n in scene["nodes"]:
        got = _node(gui.page, n["id"])["world"]
        assert got == pytest.approx(n["world_matrix"], abs=1e-6), n["id"]
    assert _node(gui.page, "needle")["jointValue"] == pytest.approx(NEEDLE_ANGLE)


def test_glb_leaf_colours_are_used(gui):
    colours = _node(gui.page, "pi")["colors"]
    want = ["15803d", "c3c9d2"]
    assert len(colours) == 2
    for got, exp in zip(colours, want, strict=True):
        assert all(abs(int(got[1 + 2 * k:3 + 2 * k], 16) - int(exp[2 * k:2 + 2 * k], 16)) <= 2 for k in range(3)), colours


def test_unchecking_tree_item_hides_mesh(gui):
    page = gui.page
    box = page.locator("[data-testid=tree-vis-lid]")
    assert _node(page, "lid")["visible"] is True
    box.uncheck()
    assert _node(page, "lid")["visible"] is False
    assert _node(page, "dial")["visible"] is True  # children keep their own visibility
    box.check()
    assert _node(page, "lid")["visible"] is True


def test_view_buttons_grid_and_isolate(gui):
    page = gui.page
    view_dir = ("() => { const v = window.piforge.viewer;"
                " const d = v.camera.position.clone().sub(v.controls.target).normalize(); return d.toArray(); }")
    for action, want in (("view-top", [0, 0, 1]), ("view-front", [0, -1, 0]), ("view-right", [1, 0, 0])):
        page.click(f"[data-action={action}]")
        # camera animation (380 ms, rAF-driven: slow on a loaded host) — wait until it has finished
        page.wait_for_function("() => !window.piforge.viewer._anim", timeout=10_000)
        assert page.evaluate(view_dir) == pytest.approx(want, abs=0.01), action
    page.click("[data-action=view-iso]")
    page.wait_for_timeout(600)
    page.click("[data-action=grid]")
    assert page.evaluate("window.piforge.viewer.gridOn") is False
    page.click("[data-action=grid]")
    assert page.evaluate("window.piforge.viewer.gridOn") is True
    page.hover("[data-testid=tree-item-lid]")
    page.click("[data-testid=tree-item-lid] .iso")
    assert [n for n in NODE_IDS if _node(page, n)["visible"]] == ["lid"]
    page.click("[data-testid=tree-item-lid] .iso")
    assert all(_node(page, n)["visible"] for n in NODE_IDS)
    page.click("[data-action=fit]")
    page.wait_for_timeout(500)


def test_check_finding_selects_parts_and_xray(gui):
    """ASM.INTERFERENCE (subject "node:bracket/base", as mech writes it) selects both parts; X-ray
    shows them through the enclosure; findings that point at nothing in 3D are not clickable."""
    page = gui.page
    _tab(page, "checks")
    row = page.locator(".finding[data-code='ASM.INTERFERENCE']")
    assert "clickable" in row.get_attribute("class")
    assert "clickable" in page.locator(".finding[data-code='PRINT.ESTIMATE']").get_attribute("class")
    for code in ("ERC.FLOATING_INPUT", "THERMAL.OK"):
        assert "clickable" not in page.locator(f".finding[data-code='{code}']").get_attribute("class")
    row.click()
    assert sorted(page.evaluate("window.piforge.debug.selected()")) == ["base", "bracket"]
    assert "2 parts selected" in page.inner_text("#vp-info")
    page.click("[data-action=xray]")
    assert page.evaluate("window.piforge.debug.xray()") is True
    assert _node(page, "lid")["opacity"] < 0.5 and _node(page, "bracket")["opacity"] == 1
    page.wait_for_timeout(500)  # camera animation
    page.screenshot(path=str(gui.shots / "01b_finding_xray.png"))
    page.click("[data-action=xray]")
    assert _node(page, "lid")["opacity"] == 1
    page.keyboard.press("Escape")
    assert page.evaluate("window.piforge.debug.selected()") == []


def test_explode_slider_moves_nodes(gui):
    page = gui.page
    lid0, pi0, br0 = (_node(page, i)["position"] for i in ("lid", "pi", "bracket"))
    _set_explode(page, 100)
    lid1, pi1, br1 = (_node(page, i)["position"] for i in ("lid", "pi", "bracket"))
    assert lid1[2] == pytest.approx(lid0[2] + 45)            # explode = offset in mm at 100 %
    assert br1 == pytest.approx([br0[0] + 45, br0[1], br0[2] + 8])
    assert pi1 == pytest.approx(pi0)                          # no explode vector → stays
    _set_explode(page, 50)
    assert _node(page, "lid")["position"][2] == pytest.approx(lid0[2] + 22.5)
    _set_explode(page, 0)
    assert _node(page, "lid")["position"] == pytest.approx(lid0)


def test_spice_stored_run_draws_chart(gui):
    page = gui.page
    _tab(page, "spice")
    page.select_option("[data-testid=spice-bench]", "run:rc_debounce")
    page.wait_for_selector("[data-testid=spice-chart] canvas", timeout=20_000)
    assert page.locator(".spice-result table.measures tbody tr").count() == 2


def test_spice_live_run_draws_chart(gui):
    if not _spice_ready():
        pytest.skip("piforge.spice.benches not available yet or ngspice missing")
    page = gui.page
    _tab(page, "spice")
    page.select_option("[data-testid=spice-bench]", "bench:rc_filter")
    page.click("[data-testid=spice-run]")
    page.wait_for_selector(".result-head:has-text('simulated in')", timeout=90_000)
    assert page.locator("[data-testid=spice-chart] canvas").count() >= 1
    # out-of-range input is caught client-side before any request
    first = page.locator(".param-form input[type=text]").first
    first.fill("-1e9")
    page.click("[data-testid=spice-run]")
    assert page.locator(".field-error:not(:empty)").count() >= 1
    page.click(".form-actions button:has-text('Defaults')")


def test_overhang_heatmap_and_bed_view(gui, clean_print_views):
    page = gui.page
    _tab(page, "print")
    page.click("[data-testid=overhang-bracket]")
    page.wait_for_function("window.piforge.debug.heat() === 'bracket'", timeout=20_000)
    assert _node(page, "bracket")["vertexColors"] is True
    assert _node(page, "bracket")["heat"] == {"overhang": 96, "bridge": 0}  # ring under the cap
    assert _node(page, "lid")["opacity"] < 0.5  # the other parts become ghosts
    assert page.locator("#vp-legend").is_visible()
    assert "300.7 mm²" in page.inner_text("[data-testid=legend-overhang]")
    page.click("[data-testid=bed-bracket]")
    page.wait_for_function("window.piforge.debug.bed() === 'bracket'")
    page.wait_for_function("window.piforge.viewer._anim === null", timeout=10_000)  # camera animation done
    page.screenshot(path=str(gui.shots / "08_overhang_bed_view.png"))
    page.click("[data-testid=bed-bracket]")
    page.click("[data-testid=overhang-bracket]")
    page.wait_for_function("window.piforge.debug.heat() === null && window.piforge.debug.bed() === null")


def test_bridges_painted_apart_from_overhangs(gui, clean_print_views):
    """The base's port-window roof spans 8 mm ≤ 10 mm: amber bridge faces, no red overhang."""
    page = gui.page
    _tab(page, "print")
    page.click("[data-testid=overhang-base]")
    page.wait_for_function("window.piforge.debug.heat() === 'base'", timeout=20_000)
    assert _node(page, "base")["heat"] == {"overhang": 0, "bridge": 2}
    assert "none" in page.inner_text("[data-testid=legend-overhang]")
    assert "16.0 mm² · 2 faces" in page.inner_text("[data-testid=legend-bridge]")
    assert page.locator("[data-testid=legend-overhang]").is_disabled()  # nothing to zoom to
    page.click("[data-testid=legend-bridge]")  # zoom onto the bridge faces (the window roof)
    page.wait_for_function("window.piforge.viewer._anim === null", timeout=10_000)  # camera animation done
    cam = page.evaluate("() => { const v = window.piforge.viewer;"
                        " return [v.camera.position.distanceTo(v.controls.target), ...v.controls.target.toArray()]; }")
    assert cam[0] < 80  # the whole 124 mm base would be ≈ 250 mm away
    assert cam[1:] == pytest.approx([0.0, -35.0, 14.5], abs=1.5)  # roof of the port window
    page.screenshot(path=str(gui.shots / "08b_bridge_heatmap.png"))
    page.click("[data-action=view-iso]")
    page.click("[data-testid=overhang-base]")
    page.wait_for_function("window.piforge.debug.heat() === null")
    assert _node(page, "lid")["opacity"] == 1


def test_section_plane_and_measure(gui):
    page = gui.page
    page.click("[data-action=section]")
    assert page.evaluate("window.piforge.debug.section().enabled") is True
    page.screenshot(path=str(gui.shots / "07_section.png"))
    page.click("[data-action=section]")
    page.click("[data-action=measure]")
    # click two points on the lid top (projected from world space)
    pts = page.evaluate("""() => {
      const v = window.piforge.viewer, c = v.canvas.getBoundingClientRect(), out = [];
      for (const [x, y] of [[-45, -25], [45, 20]]) {
        const p = v.camera.position.clone().set(x, y, 35).project(v.camera);
        out.push([c.left + (p.x + 1) / 2 * c.width, c.top + (1 - p.y) / 2 * c.height]);
      }
      return out; }""")
    for x, y in pts:
        page.mouse.click(x, y)
    page.wait_for_selector("#vp-measure strong", timeout=10_000)
    assert page.locator("#vp-info").is_hidden()  # the selection card makes room for the measure hint
    page.screenshot(path=str(gui.shots / "07b_measure.png"))
    dist = float(page.inner_text("#vp-measure strong").split()[0])
    assert 80 < dist < 110  # ≈ √(90² + 45²) = 100.6 mm (corner snapping may shift it a little)
    page.keyboard.press("Escape")
    assert page.locator("#vp-measure").is_hidden()


def test_twin_button_lights_led_in_panel_and_3d(gui):
    pytest.importorskip("piforge.twin.session")
    page = gui.page
    _tab(page, "twin")
    page.click("[data-testid=twin-start]")
    page.wait_for_function("(() => { const t = window.piforge.debug.twin(); return t.running && t.t !== null; })()",
                           timeout=60_000)
    btn = page.locator("[data-testid=btn-SW1-pressed]")
    btn.hover()
    page.mouse.down()
    try:
        page.wait_for_selector("[data-testid=led-D1].lit", timeout=15_000)
        page.wait_for_function("window.piforge.debug.node('led').emissive > 0", timeout=10_000)
        # the lamp itself must show the LED's red at once (not just carry the .lit class)
        rgba = page.evaluate("""() => { const l = document.querySelector('[data-testid=led-D1]');
            const c = document.createElement('canvas').getContext('2d');
            c.fillStyle = getComputedStyle(l).backgroundColor; c.fillRect(0, 0, 1, 1);
            return [...c.getImageData(0, 0, 1, 1).data]; }""")
        assert rgba[0] > 220 and rgba[1] < 120, rgba
        page.screenshot(path=str(gui.shots / "04_twin_running.png"))
    finally:
        page.mouse.up()
    page.wait_for_selector("[data-testid=led-D1]:not(.lit)", timeout=15_000)
    assert _node(page, "led")["emissive"] == 0
    # the firmware sweeps SERVO1 → the needle joint follows it in 3D
    page.wait_for_function(f"Math.abs(window.piforge.debug.node('needle').jointValue - {NEEDLE_ANGLE}) > 1",
                           timeout=15_000)
    snap = page.evaluate("""() => { const st = window.piforge.panels.twin.lastState;
        const n = window.piforge.debug.node('needle');
        return {angle: st.devices.SERVO1.angle, joint: n.jointValue, world: n.world}; }""")
    assert snap["joint"] == pytest.approx(max(-90.0, min(90.0, snap["angle"])), abs=1e-6)
    # world rotation of the needle about +Z (lid and dial are not rotated) = the servo angle
    assert math.degrees(math.atan2(snap["world"][1], snap["world"][0])) == pytest.approx(snap["joint"], abs=1e-3)
    page.click("[data-testid=twin-stop]")
    page.wait_for_function("!window.piforge.debug.twin().running", timeout=10_000)
    # stopping resets the 3D view to the scene's rest values and logs why the firmware ended
    assert _node(page, "needle")["jointValue"] == pytest.approx(NEEDLE_ANGLE)
    page.wait_for_function("document.querySelector('[data-testid=twin-console]').textContent.includes('firmware stopped')")


def test_twin_widget_gallery(gui):
    """Every device type of the twin catalogue gets a card from its PropSpecs; outputs render
    (synthetic hello/state/display messages — no runner needed)."""
    from piforge.server.api_twin import describe_type
    from piforge.twin.devices import DEVICE_TYPES

    page = gui.page
    _tab(page, "twin")
    ids = {"button": "SW2", "led": "D2", "rgb_led": "D3", "servo": "SERVO2", "stepper_28byj48": "M1",
           "dc_motor": "M2", "ssd1306": "OLED1", "lcd1602_pcf8574": "LCD1", "relay": "K1", "buzzer": "BZ1",
           "neopixel": "NP1", "rotary_encoder": "ENC1"}
    devices = []
    for key, cls in DEVICE_TYPES.items():
        ex = getattr(cls, "example", {}) or {}
        devices.append({**describe_type(cls), "id": ids.get(key, key.upper()), "pins": ex.get("pins", {}),
                        "bus": ex.get("bus"), "params": ex.get("params", {})})
    state = {"op": "state", "t": 12.5, "pins": {"5": 1, "6": 1, "13": 0, "19": 0, "18": 0.0849},
             "devices": {
                 "D2": {"brightness": 0.6, "on": True, "color": "green"},
                 "D3": {"red": 0.1, "green": 0.6, "blue": 1.0, "color": "#1a99ff"},
                 "SERVO2": {"angle": 35.0, "target_angle": 35.0, "pulse_us": 1694.4, "powered": True},
                 "M1": {"position": 1024, "angle": 540.0, "rpm": 12.0, "energized": True, "coils": "1100"},
                 "M2": {"speed": 0.75, "rpm": 7650.0, "angle": 1234.5, "direction": "forward"},
                 "LCD1": {"text": "Coins: 12\nTotal: 34.50 PLN", "backlight": True, "display_on": True},
                 "K1": {"on": True, "switch_count": 3}, "BZ1": {"on": True, "frequency": 2000.0},
                 "NP1": {"pixels": "#ff0000 #00ff00 #0000ff #ffffff #000000 #ff9900 #9900ff #00ffff", "shows": 4},
                 "OLED1": {"on": True, "inverted": False, "contrast": 127, "lit_pixels": 812, "frame": 3},
                 "ENC1": {"steps": 0, "pressed": False, "position": 7}}}
    display = {"op": "display", "device": "OLED1", "w": 128, "h": 64, "png_b64": _oled_png()}
    page.set_viewport_size({"width": 1280, "height": 2700})
    try:
        page.evaluate("""(msgs) => { const t = window.piforge.panels.twin;
            for (const m of msgs) t._onMessage(m); t.deviceGrid.classList.remove('stale'); }""",
                      [{"op": "hello", "devices": devices}, state, display])
        assert page.locator(".device-card").count() == len(DEVICE_TYPES)
        page.wait_for_function("(() => { const c = document.querySelector('[data-testid=display-OLED1]');"
                               " return c && !c.hidden && c.width === 128 && c.height === 64; })()")
        assert page.inner_text("[data-testid=rpm-M1]") == "12.0 rpm"
        assert page.inner_text("[data-testid=angle-M1]") == "180.0°"  # 540° = 1.5 turns
        assert page.inner_text("[data-testid=rpm-M2]") == "7650.0 rpm"
        assert "Coins: 12" in page.inner_text("[data-testid=lcd-LCD1]")
        assert page.locator("[data-testid=pixels-NP1] .px").count() == 8
        assert page.inner_text("[data-testid=out-K1-on]") == "ON"
        assert page.inner_text("[data-testid=out-K1-switch_count]") == "3"  # int outputs stay whole
        assert page.inner_text("[data-testid=out-ENC1-position]") == "7"
        assert page.inner_text("[data-testid=drive-M2]") == "75 %"
        assert page.inner_text("[data-testid=out-BZ1-frequency]") == "2.00 kHz"
        assert page.locator("[data-testid=led-D3].lit").count() == 1
        assert page.locator("[data-testid=btn-SW2-pressed]").inner_text() == "Press"
        assert page.locator("[data-testid=in-ENC1-steps-inc1]").count() == 1  # rotary encoder: −/+ stepper
        assert page.locator("[data-testid=in-BME280-temperature]").get_attribute("type") == "range"
        page.wait_for_timeout(300)
        page.screenshot(path=str(gui.shots / "09_twin_widgets.png"))
    finally:
        page.set_viewport_size({"width": 1280, "height": 800})
        page.evaluate("""() => { const t = window.piforge.panels.twin; t.lastState = null; t.clock.textContent = '';
            t._buildCards(t.info.devices); t.deviceGrid.classList.add('stale'); }""")
    assert page.locator(".device-card").count() == 3
    assert gui.errors == []


def _oled_png() -> str:
    """A 128×64 1-bit test frame (what the SSD1306 model sends), base64."""
    import base64
    import io

    from PIL import Image, ImageDraw

    img = Image.new("1", (128, 64), 0)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 127, 63], outline=1)
    d.text((8, 8), "PiForge twin", fill=1)
    d.text((8, 28), "coins: 12", fill=1)
    d.rectangle([8, 46, 8 + 70, 54], fill=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_rebuild_reloads_scene_keeping_the_camera(gui):
    """POST /api/build (button) → /ws/events "done" → the GUI reloads the scene; the camera stays."""
    page = gui.page
    scene_path = gui.root / "build" / "scene.json"
    original = scene_path.read_text(encoding="utf-8")

    def build_with_lid(color: str):
        def fake_build(project_dir, *, out_dir=None, progress=None, **kw):
            data = json.loads(original)
            next(n for n in data["nodes"] if n["id"] == "lid")["color"] = color
            (Path(out_dir) / "scene.json").write_text(json.dumps(data), encoding="utf-8")
            if progress:
                progress("scene")
        return fake_build

    lid_is = ("(hex) => { const c = window.piforge.debug.node('lid').colors[0];"
              " return [1, 3, 5].every((i) => Math.abs(parseInt(c.slice(i, i + 2), 16) - parseInt(hex.slice(i, i + 2), 16)) <= 2); }")
    cam = "() => { const v = window.piforge.viewer; return [...v.camera.position.toArray(), ...v.controls.target.toArray()]; }"
    page.wait_for_timeout(500)  # no camera animation in flight
    before = page.evaluate(cam)
    try:
        gui.app.state.piforge.build_fn = build_with_lid("#22c55e")
        page.click("[data-testid=rebuild]")
        page.wait_for_function(lid_is, arg="#22c55e", timeout=30_000)
        assert page.evaluate(cam) == pytest.approx(before, abs=1e-6)
        assert sorted(page.evaluate("window.piforge.debug.meshNodes()")) == sorted(NODE_IDS)
    finally:
        gui.app.state.piforge.build_fn = build_with_lid("#93c5fd")
        page.click("[data-testid=rebuild]")
        page.wait_for_function(lid_is, arg="#93c5fd", timeout=30_000)
        gui.app.state.piforge.build_fn = None


def test_screenshots_of_every_tab(gui):
    """Visual review artefacts (build/_review/gui/*.png) — no console errors on any tab."""
    page = gui.page
    for i, tab in enumerate(["checks", "elec", "spice", "twin", "print"], start=1):
        _tab(page, tab)
        page.wait_for_timeout(600)
        page.screenshot(path=str(gui.shots / f"{i:02d}_{tab}.png"))
    _set_explode(page, 60)
    page.wait_for_timeout(300)
    page.screenshot(path=str(gui.shots / "06_explode.png"))
    _set_explode(page, 0)
    page.click("#btn-theme")
    _tab(page, "checks")
    page.wait_for_timeout(500)
    page.screenshot(path=str(gui.shots / "10_light_theme.png"))
    for name, tab in (("11_light_twin", "twin"), ("12_light_spice", "spice"), ("13_light_print", "print")):
        _tab(page, tab)
        page.wait_for_timeout(400)
        page.screenshot(path=str(gui.shots / f"{name}.png"))
    page.click("#btn-theme")
    _tab(page, "checks")
    for f in gui.shots.glob("*.png"):
        assert f.stat().st_size > 10_000, f
    assert gui.errors == []
