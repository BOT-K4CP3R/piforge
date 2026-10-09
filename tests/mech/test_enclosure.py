"""Enclosure generator: base + lid around a Raspberry Pi with port cutouts, panel modules, vents."""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError, ValidationError

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def pi4_case():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec

    return Enclosure(EnclosureSpec(board="rpi4b"))


def _watertight(part):
    from piforge.mech.export import to_trimesh

    return to_trimesh(part.shape).is_watertight


def test_spec_validation():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem, VentSpec

    with pytest.raises(ValidationError):
        Enclosure(EnclosureSpec(wall=-1))
    with pytest.raises(ValidationError):
        Enclosure(EnclosureSpec(vents=(VentSpec("side", (20, 10)),)))
    with pytest.raises(ValidationError):
        Enclosure(EnclosureSpec(lid_fastening="glue"))
    with pytest.raises(NotFoundError):
        Enclosure(EnclosureSpec(ports=("hdmi7",)))
    with pytest.raises(NotFoundError):
        Enclosure(EnclosureSpec(panel_items=(PanelItem("oled_xyz", "top"),)))


def test_enclosure_default_pi4(pi4_case):
    enc = pi4_case
    pi = enc.board
    for part in (enc.base, enc.lid):
        assert part.kind == "printed" and part.material == "PETG"
        assert part.shape.is_valid, part.name
        assert _watertight(part), part.name
    assert enc.base.print_rotation == (0.0, 0.0, 0.0)
    assert enc.lid.print_rotation == (180.0, 0.0, 0.0)
    il, iw, ih = enc.inner_size
    assert il >= pi.length + 2 * enc.spec.side_clearance
    assert iw >= pi.width + 2 * enc.spec.side_clearance
    assert ih >= enc.spec.standoff_height + pi.thickness + pi.top_height + enc.spec.top_clearance - 1e-6
    ol, ow, oh = enc.outer_size
    assert ol == pytest.approx(il + 2 * enc.spec.wall)
    assert oh == pytest.approx(ih + enc.spec.floor + enc.spec.lid_thickness)
    lo, hi = enc.base.bounds()
    assert lo[2] == pytest.approx(0.0, abs=1e-6)
    assert hi[0] - lo[0] == pytest.approx(ol, abs=1e-3)

    rep = enc.checks()
    assert rep.ok, rep.to_markdown()
    blocked = rep.by_code("ACCESS.PORT_BLOCKED")
    assert not blocked
    assert len(rep.by_code("ACCESS.PORT_OK")) == len(pi.edge_ports())
    assert rep.has("ENCL.LID_SCREW") and rep.by_code("ENCL.LID_SCREW")[0].data["engagement_mm"] >= 4


def test_board_location_inside_cavity(pi4_case):
    import numpy as np

    from piforge.mech.assembly import location_to_matrix

    enc = pi4_case
    m = location_to_matrix(enc.board_location)
    il, iw, _ = enc.inner_size
    x0, y0, z0 = m[:3, 3]
    assert -il / 2 < x0 and x0 + enc.board.length < il / 2
    assert -iw / 2 < y0 and y0 + enc.board.width < iw / 2
    assert z0 == pytest.approx(enc.spec.floor + enc.spec.standoff_height)
    assert np.allclose(m[:3, :3], np.eye(3))


def test_enclosure_assembly(pi4_case):
    asm = pi4_case.assembly()
    kinds = {n.part.kind for n in asm.nodes}
    assert {"printed", "pcb", "fastener"} <= kinds
    assert "lid" in asm and "board" in asm
    assert asm.node("lid").explode is not None
    bare = pi4_case.assembly(include_board=False, include_screws=False)
    assert {n.part.kind for n in bare.nodes} == {"printed"}


def test_enclosure_print_orientation_no_port_overhangs(pi4_case):
    """Port cutout roofs are bridges, not overhangs; the lid prints outside-face down."""
    from piforge.fab.analyze import analyze_mesh
    from piforge.mech.export import to_trimesh

    for part in (pi4_case.base, pi4_case.lid):
        res = analyze_mesh(to_trimesh(part.shape), "generic", "PETG", name=part.name,
                           rotation=part.print_rotation, wall_samples=300)
        assert res.watertight
        assert not res.report.has("PRINT.OVERHANG", "warning"), res.report.to_markdown()
        assert not res.report.has("PRINT.THIN_WALL", "error")
    base = analyze_mesh(to_trimesh(pi4_case.base.shape), rotation=pi4_case.base.print_rotation,
                        wall_samples=0)
    assert base.bridge_count >= len(pi4_case.board.edge_ports())


def test_enclosure_port_blocked_detected():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec

    enc = Enclosure(EnclosureSpec(board="rpi4b", ports=()))
    rep = enc.checks()
    blocked = {f.data["port"] for f in rep.by_code("ACCESS.PORT_BLOCKED")}
    assert blocked == {p.name for p in enc.board.edge_ports()}
    assert all(f.severity.name == "ERROR" for f in rep.by_code("ACCESS.PORT_BLOCKED"))


def test_enclosure_panel_item():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem
    from piforge.mech.modules import get_module

    oled = get_module("ssd1306_096_i2c")
    plain = Enclosure(EnclosureSpec(board="rpi4b", inner_height=40.0))
    bare = Enclosure(EnclosureSpec(board="rpi4b", inner_height=40.0,
                                   panel_items=(PanelItem("ssd1306_096_i2c", "top", (10, 5), mount=False),)))
    w, h = oled.window[2]
    c = 0.3
    expected = (w + 2 * c) * (h + 2 * c) * plain.spec.lid_thickness
    assert plain.lid.volume - bare.lid.volume == pytest.approx(expected, rel=0.02)

    mounted = Enclosure(EnclosureSpec(board="rpi4b", inner_height=40.0,
                                      panel_items=(PanelItem("ssd1306_096_i2c", "top", (10, 5)),)))
    assert mounted.lid.volume > bare.lid.volume  # screw bosses added
    assert _watertight(mounted.lid)
    rep = mounted.checks()
    assert rep.ok, rep.to_markdown()
    asm = mounted.assembly()
    assert any(n.part.kind == "reference" for n in asm.nodes)


def test_enclosure_auto_height_for_top_module():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem

    plain = Enclosure(EnclosureSpec(board="rpi4b"))
    lcd = Enclosure(EnclosureSpec(board="rpi4b", panel_items=(PanelItem("lcd1602_i2c", "top"),)))
    assert lcd.inner_size[2] > plain.inner_size[2]
    assert lcd.checks().ok


@pytest.mark.parametrize("board", ["rpi5", "rpizero2w", "rpi3bp"])
def test_enclosure_pi5_and_zero(board):
    from piforge.mech.enclosure import Enclosure, EnclosureSpec

    enc = Enclosure(EnclosureSpec(board=board))
    assert _watertight(enc.base) and _watertight(enc.lid)
    rep = enc.checks()
    assert rep.ok, rep.to_markdown()


def test_enclosure_options():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem, VentSpec

    enc = Enclosure(EnclosureSpec(
        board="rpizero2w", board_fastening="insert", lid_fastening="snap", label="PiForge",
        vents=(VentSpec("top", (30, 12)), VentSpec("+y", (30, 8), style="hex"), VentSpec("-x", (12, 8))),
        panel_items=(PanelItem("led_5mm", "-x", (-8, 3)), PanelItem("tact_6x6", "-x", (8, 0))),
        extra_space=(0, 15, 0, 0)))
    assert _watertight(enc.base) and _watertight(enc.lid)
    assert enc.inner_size[0] >= enc.board.length + 15
    rep = enc.checks()
    assert not rep.has("ENCL.LID_SCREW")
    assert rep.has("ENCL.SNAP")
    assert rep.ok, rep.to_markdown()


# --- fix round 1 ------------------------------------------------------------------------------
def _mouth_depth(enc, port):
    """Reviewer probe p1: distance from the port mouth to the outer wall face along the port axis."""
    import numpy as np

    from piforge.mech.assembly import location_to_matrix

    c = location_to_matrix(enc.board_location) @ np.array([*port.center, 1.0])
    il, iw, _ = enc.inner_size
    inner = il / 2 if port.edge in ("+x", "-x") else iw / 2
    pos = abs(c[0]) if port.edge in ("+x", "-x") else abs(c[1])
    return inner + enc.spec.wall - pos


@pytest.mark.parametrize("board", ["rpi4b", "rpi5", "rpizero2w"])
def test_default_port_mouths_near_outer_face(board):
    from piforge.mech.enclosure import FINGER_REACH, Enclosure, EnclosureSpec

    enc = Enclosure(EnclosureSpec(board=board))
    depths = enc.port_depths()
    for p in enc.board.edge_ports():
        d = depths[p.name]
        assert d["depth"] == pytest.approx(_mouth_depth(enc, p), abs=1e-6)
        if d["finger"]:
            assert d["notch"]
        elif p.kind == "fpc":  # ribbon slot: no plug, only the cable passes
            assert d["depth"] <= 4.0, (p.name, d)
        else:
            assert d["depth"] <= 3.4, (p.name, d)  # Pi 4/5 ≤ 2.7, Zero 2 W mini-HDMI 3.35
    rep = enc.checks()
    assert rep.ok, rep.to_markdown()
    assert not rep.has("ENCL.PORT_RECESS", "warning"), rep.to_markdown()
    gaps = {f.data["port"]: f.data for f in rep.by_code("ENCL.PORT_DEPTH")}
    for p in enc.board.edge_ports():
        if gaps[p.name]["finger"]:
            assert gaps[p.name]["finger_gap"] <= FINGER_REACH, gaps[p.name]


def test_pillars_clear_board_outline():
    """The board must drop in vertically: nothing of the base overhangs its rounded outline above
    the standoffs, and the side clearances grow by ≤ 0.8 mm for the lid-screw pillars (was up to 3.5)."""
    import build123d as bd

    from piforge.mech.assembly import location_to_matrix
    from piforge.mech.enclosure import Enclosure, EnclosureSpec

    for board in ("rpi4b", "rpi5", "rpizero2w"):
        enc = Enclosure(EnclosureSpec(board=board))
        b, m = enc.board, location_to_matrix(enc.board_location)
        x0, y0, z0 = m[:3, 3]
        prism = bd.extrude(bd.RectangleRounded(b.length, b.width, b.corner_radius, align=(bd.Align.MIN, bd.Align.MIN)),
                           enc.outer_size[2]).moved(bd.Location((x0, y0, z0)))
        inter = enc.base.shape & prism
        vol = sum(sl.volume for sl in inter.solids()) if inter is not None else 0.0
        assert vol < 0.01, (board, vol)
        assert all(1.5 <= v <= 1.5 + 0.8 for v in enc.clearances.values()), (board, enc.clearances)


def test_port_recess_and_finger_notch_off():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec

    deep = Enclosure(EnclosureSpec(board="rpi4b", side_clearance=6.0)).checks()
    recessed = {f.data["port"] for f in deep.by_code("ENCL.PORT_RECESS") if f.severity.name == "WARNING"}
    assert {"power", "hdmi0", "hdmi1"} <= recessed
    assert "sdcard" not in recessed  # the fingertip notch lets the finger into the cavity
    plain = Enclosure(EnclosureSpec(board="rpi4b", finger_notches=False))
    rep = plain.checks()
    assert {f.data["port"] for f in rep.by_code("ENCL.PORT_RECESS")} == {"sdcard"}


def test_excluded_ports_are_info():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec

    enc = Enclosure(EnclosureSpec(board="rpi4b", ports=("power", "usb2", "usb3", "ethernet", "hdmi0")))
    rep = enc.checks()
    assert rep.ok, rep.to_markdown()
    excluded = {f.data["port"] for f in rep.by_code("ENCL.PORT_EXCLUDED")}
    assert excluded == {"hdmi1", "audio", "sdcard"}
    assert all(f.severity.name == "INFO" for f in rep.by_code("ENCL.PORT_EXCLUDED"))
    assert not rep.by_code("ACCESS.PORT_BLOCKED")


def test_no_vents_warning():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem, VentSpec

    assert Enclosure(EnclosureSpec(board="rpi5")).checks().has("ENCL.NO_VENTS", "warning")
    vented = Enclosure(EnclosureSpec(board="rpi4b", vents=(VentSpec("top", (40, 20)),))).checks()
    assert not vented.has("ENCL.NO_VENTS")
    fan = Enclosure(EnclosureSpec(board="rpi4b", inner_height=40.0,
                                  panel_items=(PanelItem("fan_30mm", "top", (-10, 0)),))).checks()
    assert not fan.has("ENCL.NO_VENTS")
    assert not Enclosure(EnclosureSpec(board="rpizero2w")).checks().has("ENCL.NO_VENTS")


def test_feature_overlap_warning():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem, VentSpec

    enc = Enclosure(EnclosureSpec(board="rpi4b", inner_height=40.0, vents=(
        VentSpec("-y", (10, 8), offset=(-31, -6)),  # over the USB-C power cutout
        VentSpec("top", (30, 20), offset=(10, 5)),  # over the OLED window
        VentSpec("top", (20, 10), offset=(-30, -20)),  # clear
    ), panel_items=(PanelItem("ssd1306_096_i2c", "top", (10, 5)),), label="PI", label_face="+y"))
    rep = enc.checks()
    hits = rep.by_code("ENCL.FEATURE_OVERLAP")
    assert all(f.severity.name == "WARNING" for f in hits)
    pairs = {(f.data["face"], tuple(sorted(f.data["features"]))) for f in hits}
    assert any(face == "-y" and any("power" in s for s in feats) for face, feats in pairs), pairs
    assert any(face == "top" and any("ssd1306" in s for s in feats) for face, feats in pairs), pairs
    assert len(hits) == 2, pairs


def test_round_wall_opening_teardrop_clipped_below_rim():
    """A 30 mm fan on a 34 mm wall: the teardrop tip would cut through the rim — it is flattened."""
    import build123d as bd

    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem

    enc = Enclosure(EnclosureSpec(board="rpi4b", extra_space=(36.0, 0, 0, 0), inner_height=34.0,
                                  panel_items=(PanelItem("fan_30mm", "-y", (-41.0, -1.5)),), printer="prusa_mk4"))
    base = enc.base
    assert base.shape.is_valid and _watertight(base)
    assert len(base.shape.solids()) == 1
    s = enc.spec
    il, iw, _ih = enc.inner_size
    top = enc.outer_size[2] - s.lid_thickness  # base wall rim
    # the full wall cross-section above the fan, within one wall thickness of the rim, is solid
    probe = bd.Box(20.0, s.wall - 0.02, s.wall - 0.02, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MAX)).moved(
        bd.Location((-41.0, -iw / 2 - s.wall / 2, top - 0.01)))
    assert (base.shape & probe).volume == pytest.approx(probe.volume, rel=1e-3)
    # …and the opening is still there (centre of the fan)
    hole = bd.Box(10.0, s.wall - 0.02, 10.0).moved(
        bd.Location((-41.0, -iw / 2 - s.wall / 2, s.floor + _ih / 2 - 1.5)))
    assert (base.shape & hole).volume < 1e-3
    assert not enc.checks().has("ENCL.FEATURE_TOO_BIG", "error")


def test_round_wall_opening_too_big_for_wall_is_reported():
    from piforge.mech.enclosure import Enclosure, EnclosureSpec, PanelItem

    enc = Enclosure(EnclosureSpec(board="rpi4b", extra_space=(46.0, 0, 0, 0), inner_height=30.0,
                                  panel_items=(PanelItem("fan_40mm", "-y", (-45.0, 0.0), mount=False),)))
    assert enc.checks().has("ENCL.FEATURE_TOO_BIG", "error")
