"""3D wiring harness in the GUI (Playwright + headless Chromium): wires in their insulation colour,
connector pins, hover tooltip, wire details, net highlight, X-ray parts, Electronics tables ↔ 3D.

The fixture project gets ``wires=True``: 7 tube wires (``kind: "wire"`` nodes, ids ``wire_W<n>``)
inside the closed enclosure, 15 connectors and ``elec/cut_list.csv``. One server + one page for the
module; tests run in file order. Screenshots go to ``build/_review/gui/wires_*.png``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..server.fixture_build import CONNECTORS, WIRES, live_server, make_fixture_project, wire_node_id

pytestmark = [pytest.mark.gui, pytest.mark.timeout(600)]

READY = "window.piforge && window.piforge.ready === true"
CONN = {c[0]: c for c in CONNECTORS}


def _end(cid: str) -> str:
    _id, ref, pin, label, *_ = CONN[cid]
    return f"{ref} pin {pin}" + (f" ({label})" if label and label != pin else "") if pin.isdigit() else f"{ref} {pin}"


def tooltip(root: Path, wid: str) -> str:
    """The hover text the GUI must show for a fixture wire (length rounded to whole mm)."""
    w = next(x for x in WIRES if x[0] == wid)
    _wid, a, b, net, _sig, cname, _hex, awg, cable, _z = w
    scene = json.loads((root / "build" / "scene.json").read_text(encoding="utf-8"))
    length = next(n["wire"]["length_mm"] for n in scene["nodes"] if n["id"] == wire_node_id(wid))
    return " · ".join(filter(None, [wid, f"{_end(a)} → {_end(b)}", f"net {net}", f"{cname} {awg} AWG",
                                    f"{round(length)} mm", f"cable {cable}" if cable else None]))


@pytest.fixture(scope="module")
def gui(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> Iterator[SimpleNamespace]:
    sync_api = pytest.importorskip("playwright.sync_api")
    from piforge.server.app import create_app

    root = make_fixture_project(tmp_path_factory.mktemp("wires") / "wired project ~ż", wires=True)
    shots = repo_root / "build" / "_review" / "gui"
    shots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with live_server(create_app(root)) as base, sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.add_init_script("try { localStorage.clear(); } catch (e) {}")
            page.goto(base + "/")
            page.wait_for_function(READY, timeout=120_000)
            yield SimpleNamespace(page=page, base=base, errors=errors, root=root, shots=shots)
        finally:
            browser.close()


def _wires(page) -> dict:
    return page.evaluate("window.piforge.debug.wires()")


def _node(page, node_id: str) -> dict:
    return page.evaluate("(id) => window.piforge.debug.node(id)", node_id)


def _screen(page, **q) -> dict:
    p = page.evaluate("(q) => window.piforge.debug.wireScreen(q)", q)
    assert p, q
    return p


def _settle(page) -> None:
    page.wait_for_function("() => !window.piforge.viewer._anim", timeout=10_000)
    page.wait_for_timeout(150)


def _tab(page, name: str) -> None:
    page.click(f"[data-testid=tab-{name}]")
    page.wait_for_selector(f"#tab-{name}:not([hidden])")


def _hover_at(page, p: dict) -> None:
    page.mouse.move(p["x"] - 3, p["y"] - 3)
    page.mouse.move(p["x"], p["y"])
    page.wait_for_timeout(250)  # hover picking is throttled (60 ms)


def _xray(page, on: bool) -> None:
    if _wires(page)["xray"] != on:
        page.click("[data-testid=wire-xray]")
    assert _wires(page)["xray"] is on


def test_wires_and_connectors_load(gui):
    page = gui.page
    d = _wires(page)
    assert d["wires"] == [w[0] for w in WIRES]
    assert d["connectors"] == [c[0] for c in CONNECTORS]
    assert d["on"] is True and d["markersVisible"] is True
    assert page.is_visible("#wire-tools")
    for w in WIRES:
        n = _node(page, wire_node_id(w[0]))
        assert n["kind"] == "wire" and n["meshes"] == 1 and n["visible"] is True
        got, want = n["colors"][0], w[6]
        assert all(abs(int(got[1 + 2 * k:3 + 2 * k], 16) - int(want[1 + 2 * k:3 + 2 * k], 16)) <= 2 for k in range(3)), (got, want)
    # tree: a WIRES group with the servo lead as a cable and loose wires by net
    assert page.locator(".tree-group[data-kind=wire] .tree-row").count() == len(WIRES)
    assert page.locator(".tree-sub[data-group='cable SERVO lead'] .tree-row").count() == 3
    assert page.locator(".tree-sub[data-group='net GND'] .tree-row").count() == 2
    assert "U1 pin 13 → SW1 pin 1" in page.inner_text(f"[data-testid=tree-item-{wire_node_id('W2')}]")
    page.screenshot(path=str(gui.shots / "wires_01_overview.png"))
    assert gui.errors == []


def test_tooltip_format_of_a_harness_record(gui):
    """``mech.harness`` records carry pin name + physical number + label: the header end reads
    "pin N (label)", a named pin reached through a perfboard header reads "U2 SER"."""
    w = {"id": "W12", "net": "MOSI", "signal": "SPI MOSI", "color_name": "blue", "gauge_awg": 22,
         "length_mm": 85.3, "cable": "W-SPI",
         "from": {"ref": "U1", "pin": "GPIO10", "number": "19", "label": "GPIO10 / SPI0 MOSI", "connector": "U1.19"},
         "to": {"ref": "U2", "pin": "SER", "number": "14", "label": "JP.1 (U2.SER)", "connector": "PB1.JP-1"}}
    got = gui.page.evaluate("(w) => import('/static/js/wires.js').then((m) => [m.wireTooltip(w), m.endVia(w.to)])", w)
    assert got == ["W12 · U1 pin 19 (GPIO10 / SPI0 MOSI) → U2 SER · net MOSI · blue 22 AWG · 85 mm · cable W-SPI",
                   "JP.1 (U2.SER)"]


def test_wires_toggle(gui):
    page = gui.page
    nid = wire_node_id("W1")
    page.click("[data-testid=wires-toggle]")
    assert _wires(page)["on"] is False and _wires(page)["markersVisible"] is False
    assert _node(page, nid)["visible"] is False
    assert _node(page, "lid")["visible"] is True
    page.keyboard.press("w")  # shortcut
    assert _wires(page)["on"] is True and _node(page, nid)["visible"] is True
    assert "on" in page.get_attribute("[data-testid=wires-toggle]", "class")


def test_xray_parts_keeps_wires_solid(gui):
    page = gui.page
    _xray(page, True)
    assert _node(page, "lid")["opacity"] < 0.3 and _node(page, "base")["opacity"] < 0.3
    assert _node(page, wire_node_id("W5"))["opacity"] == 1  # solid (drawn after the ghosts)
    page.wait_for_timeout(200)
    page.screenshot(path=str(gui.shots / "wires_02_xray.png"))
    _xray(page, False)
    assert _node(page, "lid")["opacity"] == 1


def test_hover_wire_shows_tooltip(gui):
    page = gui.page
    _xray(page, True)  # the wires run inside the closed box
    page.click("[data-action=view-top]")
    _settle(page)
    nid = wire_node_id("W6")
    _hover_at(page, _screen(page, node=nid))
    tip = page.locator("#vp-hover")
    tip.wait_for(state="visible", timeout=5_000)
    assert tip.inner_text() == tooltip(gui.root, "W6")
    assert tip.inner_text().startswith("W6 · U1 pin 2 (5V) → M1 VCC · net 5V · red 26 AWG · ")
    assert tip.inner_text().endswith(" mm · cable SERVO lead")
    assert _wires(page)["hover"] == nid
    assert _node(page, nid)["emissive"] > 0  # hover highlight
    page.screenshot(path=str(gui.shots / "wires_03_hover.png"))
    page.mouse.move(5, 790)
    page.wait_for_timeout(250)
    assert _wires(page)["hover"] is None


def test_click_wire_shows_details_and_dims_the_rest(gui):
    page = gui.page
    nid = wire_node_id("W1")
    p = _screen(page, node=nid)
    page.mouse.click(p["x"], p["y"])
    assert page.evaluate("window.piforge.debug.selected()") == [nid]
    card = page.locator("#vp-info")
    assert card.get_attribute("data-kind") == "wire"
    text = card.inner_text()
    assert "W1" in text and "U1 pin 11 (GPIO17)" in text and "D1 A · anode" in text and "GPIO17" in text
    assert "green 24 AWG" in text
    assert _wires(page)["focus"] == [nid]
    assert _node(page, wire_node_id("W5"))["opacity"] < 0.2  # other wires dim
    assert _node(page, "lid")["opacity"] < 0.3  # parts ghost
    assert _node(page, nid)["opacity"] == 1
    page.screenshot(path=str(gui.shots / "wires_04_selected.png"))
    # click an end: the camera flies to that connector
    page.click("[data-testid=wire-end-to]")
    _settle(page)
    target = page.evaluate("window.piforge.viewer.controls.target.toArray()")
    assert target == pytest.approx(CONN["D1.A"][5], abs=0.5)
    page.screenshot(path=str(gui.shots / "wires_05_fly_to_end.png"))


def test_connector_click_highlights_the_net(gui):
    page = gui.page
    page.click("[data-testid=wire-end-from]")  # fly to U1 pin 11: the header pins are big enough to click
    _settle(page)
    p = _screen(page, connector="U1.14")
    page.mouse.move(p["x"], p["y"])
    page.wait_for_timeout(250)
    assert "U1 pin 14 · GND" in page.inner_text("#vp-hover")
    page.mouse.click(p["x"], p["y"])
    d = _wires(page)
    assert d["connector"] == "U1.14"
    gnd = sorted(wire_node_id(w[0]) for w in WIRES if w[3] == "GND")
    assert sorted(d["focus"]) == gnd
    card = page.locator("#vp-info")
    assert card.get_attribute("data-kind") == "connector"
    assert page.locator(".net-wire").count() == len(gnd)
    assert "net GND · 3 wires" in card.inner_text()
    page.screenshot(path=str(gui.shots / "wires_06_net.png"))
    # a wire of the list selects that wire
    page.click("[data-testid=net-wire-W7]")
    assert page.evaluate("window.piforge.debug.selected()") == [wire_node_id("W7")]
    page.keyboard.press("Escape")
    assert _wires(page)["focus"] == [] and _wires(page)["connector"] is None
    page.click("[data-action=view-iso]")
    _settle(page)


def test_elec_tables_select_wires_and_follow_3d(gui):
    page = gui.page
    _tab(page, "elec")
    page.wait_for_selector("[data-testid=wire-row-W5]")
    assert page.locator("tr[data-wire]").count() == len(WIRES)
    page.click("[data-testid=wire-row-W5]")
    assert page.evaluate("window.piforge.debug.selected()") == [wire_node_id("W5")]
    assert "sel" in page.get_attribute("[data-testid=wire-row-W5]", "class")
    page.wait_for_timeout(500)
    page.screenshot(path=str(gui.shots / "wires_07_elec_wiring.png"))
    # 3D/tree → table
    page.click(f"[data-testid=tree-item-{wire_node_id('W3')}]")
    assert "sel" in page.get_attribute("[data-testid=wire-row-W3]", "class")
    assert "sel" not in page.get_attribute("[data-testid=wire-row-W5]", "class")
    # cut list
    page.click("#tab-elec .segmented button:has-text('Cut list')")
    page.wait_for_selector("#tab-elec table.wires")
    rows = page.locator("#tab-elec tr[data-wire]")
    assert rows.count() == len(WIRES)
    assert "sel" in page.get_attribute("[data-testid=wire-row-W3]", "class")  # selection survives the view switch
    assert "mm" in page.text_content("#tab-elec thead")
    page.click("[data-testid=wire-row-W6]")
    assert page.evaluate("window.piforge.debug.selected()") == [wire_node_id("W6")]
    page.wait_for_timeout(500)
    page.screenshot(path=str(gui.shots / "wires_08_cut_list.png"))
    page.click("#tab-elec .segmented button:has-text('Wiring')")
    page.keyboard.press("Escape")
    _tab(page, "checks")


def test_light_theme_and_no_errors(gui):
    page = gui.page
    page.click("#btn-theme")
    _xray(page, True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(gui.shots / "wires_09_light_xray.png"))
    _xray(page, False)
    page.click("#btn-theme")
    for f in gui.shots.glob("wires_*.png"):
        assert f.stat().st_size > 10_000, f
    assert gui.errors == []
