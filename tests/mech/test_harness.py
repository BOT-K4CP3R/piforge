"""3D wiring harness: anchors, routing, colours/gauges, lengths, collisions, scene contract."""

import json

import numpy as np
import pytest

from piforge.mech.anchors import PinAnchor
from piforge.mech.boards import BOARDS, HEADER_PITCH, get_board
from piforge.mech.modules import get_module


# --------------------------------------------------------------------------------- anchors (fast)
@pytest.mark.parametrize("key", sorted(BOARDS))
def test_pi_header_anchor_positions(key):
    b = get_board(key)
    cx, cy, top = b.port("gpio").center
    a = {x.number: x for x in b.anchors}
    assert len(a) == 40
    half = 9.5 * HEADER_PITCH
    # pin 1 at the SD-card end (−x) on the inner row, even pins on the board-edge row
    assert a["1"].pos == pytest.approx((cx - half, cy - HEADER_PITCH / 2, top))
    assert a["2"].pos == pytest.approx((cx - half, cy + HEADER_PITCH / 2, top))
    assert a["39"].pos == pytest.approx((cx + half, cy - HEADER_PITCH / 2, top))
    assert a["40"].pos == pytest.approx((cx + half, cy + HEADER_PITCH / 2, top))
    assert a["2"].pos[1] < b.width  # inside the PCB
    assert (a["1"].pin, a["2"].pin, a["39"].pin, a["40"].pin) == ("3V3", "5V", "GND", "GPIO21")
    assert a["19"].label == "GPIO10 / SPI0 MOSI"
    assert all(x.dir == (0.0, 0.0, 1.0) and x.kind == "dupont" for x in b.anchors)


def test_module_anchors():
    uln = get_module("uln2003_board")
    assert {a.pin for a in uln.anchors} == {"IN1", "IN2", "IN3", "IN4", "+", "-", "A", "B", "C", "D", "M+"}
    assert uln.anchor("IN1").pos[2] == pytest.approx(1.6 + 8.5)
    assert uln.anchor("M+").kind == "jst_xh"
    mot = get_module("stepper_28byj48")
    lead = {a.pin: a for a in mot.anchors}
    assert lead["COM"].color == "red" and lead["D"].color == "blue"
    assert all(a.lead_mm == 230.0 and a.cable == "28BYJ-48 lead" for a in mot.anchors)
    jack = get_module("dc_jack")
    assert {a.pin for a in jack.anchors} == {"V+", "GND"}
    hall = get_module("hall_a3144")
    assert [a.pin for a in hall.anchors] == ["VCC", "GND", "OUT"]


def test_anchor_transform():
    a = PinAnchor("X", (1, 2, 3), (0, 0, 1))
    m = np.eye(4)
    m[:3, :3] = [[1, 0, 0], [0, -1, 0], [0, 0, -1]]  # 180° about X
    m[:3, 3] = (10, 20, 30)
    w = a.transformed(m)
    assert w.pos == pytest.approx((11, 18, 27)) and w.dir == pytest.approx((0, 0, -1))


def test_perfboard_layout():
    from piforge.mech.harness import Perfboard

    pb = Perfboard("perfboard_50x70")
    assert (pb.cols, pb.rows) == (27, 19)
    pb.header("J1", 0, 0, [("U2", "QA"), None, ("U2", "VCC", "U11")])
    an = pb.anchors()["U2"]
    assert [a.id for a in an] == ["PB1.J1-1", "PB1.J1-3"]
    assert an[0].pos == pytest.approx((-13 * 2.54, -9 * 2.54, 1.6 + 8.5))
    assert an[1].peer == "U11"
    with pytest.raises(Exception):
        pb.header("J2", 26, 18, [("U2", "QB"), ("U2", "QC")])  # runs off the grid


# ------------------------------------------------------------------------------- routing (slow)
def _setup(motor_at=(100.0, 70.0, 0.0), wall=False):
    import build123d as bd

    from piforge.elec import Circuit
    from piforge.mech import Assembly, PartSpec

    c = Circuit("t")
    pi = c.add("rpi4b", "U1")
    drv = c.add("uln2003_board", "U11")
    mot = c.add("stepper_28byj48", "M1")
    c.connect(pi["5V"], drv["+"])
    c.connect(pi["GND"], drv["-"])
    for k in range(4):
        c.connect(pi[f"GPIO{(17, 27, 22, 23)[k]}"], drv[f"IN{k + 1}"])
    c.connect(drv["M+"], mot["COM"])
    for coil in "ABCD":
        c.connect(drv[coil], mot[coil])
    asm = Assembly("t")
    b = get_board("rpi4b")
    asm.add(b.part(), (0, 0, 0), id="pi")
    asm.add(get_module("uln2003_board").part("drv"), (130, 20, 0), id="drv")
    asm.add(get_module("stepper_28byj48").part("mot"), motor_at, id="mot")
    if wall:
        asm.add(PartSpec("wall", bd.Box(4, 140, 80).moved(bd.Location((95, -30, 40))), kind="reference"), id="wall")
    anchors = {"U1": ("pi", b.anchors), "U11": ("drv", get_module("uln2003_board").anchors),
               "M1": ("mot", get_module("stepper_28byj48").anchors)}
    return c, asm, anchors


@pytest.mark.slow
def test_route_connects_anchor_to_anchor(tmp_path):
    from piforge.mech.harness import AWG_OD, route_harness

    c, asm, anchors = _setup()
    h = route_harness(c, asm, anchors, slack=0.1, slack_mm=20.0, direct_max=400.0)
    assert len(h.wires) == 6 + 5  # 4 IN + 5 V + GND, 5 motor lead wires
    world = {}
    for ref, (node, an) in anchors.items():
        m = asm.world_matrix(node)
        world[ref] = [a.transformed(m) for a in an]
    for w in h.wires:
        p = [np.asarray(x) for x in w.points]
        assert np.allclose(p[0], w.a.anchor.pos) and np.allclose(p[-1], w.b.anchor.pos)
        assert any(np.allclose(w.a.anchor.pos, a.pos) for a in world[w.a.ref])
        for s, e in zip(p[:-1], p[1:]):  # orthogonal polyline
            assert int(np.sum(np.abs(e - s) > 1e-6)) == 1
        # first segment leaves the contact along its exit direction
        d = (p[1] - p[0]) / np.linalg.norm(p[1] - p[0])
        assert np.allclose(d, w.a.anchor.dir, atol=1e-6)
        assert w.route_mm == pytest.approx(sum(np.linalg.norm(e - s) for s, e in zip(p[:-1], p[1:])))
        if w.lead_mm:
            assert w.length_mm == 230.0 and w.cable == "28BYJ-48 lead" and w.gauge_awg == 26
        else:
            assert w.length_mm == pytest.approx(w.route_mm * 1.1 + 20.0, abs=0.05)
            assert w.gauge_awg == 22 and w.od == AWG_OD[22]
    by = {(w.b.ref, w.b.pin): w for w in h.wires}
    assert by[("U11", "+")].color_name == "red" and by[("U11", "-")].color_name == "black"
    assert by[("U11", "+")].a.connector == "U1.2"
    assert by[("M1", "COM")].a.pin == "M+" and by[("M1", "COM")].color_name == "red"
    assert by[("M1", "D")].color_name == "blue"
    rep = h.checks()
    assert not rep.errors, rep.to_markdown()
    assert rep.has("WIRE.SUMMARY") and rep.has("WIRE.LEAD_SLACK")
    # cut list
    csv = h.cut_list_csv().splitlines()
    assert csv[0].startswith("wire,cable,from") and len(csv) == 1 + len(h.wires)
    assert "| W1 |" in h.cut_list_markdown()
    # scene contract
    ids = h.add_to(asm)
    assert h.add_to(asm) == ids  # idempotent
    asm.to_scene(tmp_path)
    scene = json.loads((tmp_path / "scene.json").read_text())
    conns = {x["id"]: x for x in scene["connectors"]}
    assert {"U1.19", "U11.1", "M1.5"} <= set(conns)
    c19 = conns["U1.19"]
    assert c19["ref"] == "U1" and c19["pin"] == "19" and c19["label"] == "GPIO10 / SPI0 MOSI" and c19["node"] == "pi"
    assert len(c19["pos"]) == 3 and len(c19["dir"]) == 3
    wn = [n for n in scene["nodes"] if n["kind"] == "wire"]
    assert len(wn) == len(h.wires)
    for n in wn:
        assert n["matrix"] == pytest.approx(list(np.eye(4).flatten(order="F")))
        rec = n["wire"]
        assert {"id", "from", "to", "net", "signal", "color_name", "gauge_awg", "length_mm", "cable"} <= set(rec)
        assert {"ref", "pin", "label", "connector"} <= set(rec["from"])
        assert rec["from"]["connector"] in conns and rec["to"]["connector"] in conns
        assert n["color"].startswith("#") and (tmp_path / n["mesh"]).exists()
    # wires are not part of the default interference check
    assert not asm.check_interference().errors


@pytest.mark.slow
def test_routing_avoids_a_wall_and_collisions_are_reported():
    from piforge.mech.harness import route_harness

    c, asm, anchors = _setup(wall=True)
    h = route_harness(c, asm, anchors, direct_max=600.0)
    assert not h.checks().errors  # the router went around the wall (it ends at y = 40)
    # force one wire straight through the wall: WIRE.COLLISION
    w = next(x for x in h.wires if x.b.pin == "IN1")
    a, b = np.asarray(w.points[0]), np.asarray(w.points[-1])
    w.points = [tuple(a), (a[0], a[1], 40.0), (a[0], 0.0, 40.0), (b[0], 0.0, 40.0), (b[0], b[1], 40.0), tuple(b)]
    h._collisions = None
    rep = h.checks()
    hits = rep.by_code("WIRE.COLLISION")
    assert hits and any(f.data["node"] == "wall" for f in hits)


@pytest.mark.slow
def test_motor_lead_too_short():
    from piforge.mech.harness import route_harness

    c, asm, anchors = _setup(motor_at=(-250.0, 200.0, 0.0))
    h = route_harness(c, asm, anchors, direct_max=2000.0, avoid_collisions=False)
    rep = h.checks(collisions=False)
    assert rep.has("WIRE.LEAD_TOO_SHORT")


@pytest.mark.slow
def test_channels_bundle_and_hub():
    from piforge.mech.harness import Channel, route_harness

    c, asm, anchors = _setup()
    ch = Channel("trunk", (20, 80, 30), (160, 80, 30), width=6, height=12, only=("W-*",))
    h = route_harness(c, asm, anchors, channels=[ch], via={"W-*": ["trunk"]}, avoid_collisions=False)
    rib = [w for w in h.wires if not w.lead_mm]
    assert all(w.channels == ["trunk"] for w in rib)
    # each wire has its own slot in the trunk: distinct (y, z) along the trunk run
    def trunk_yz(w):
        pts = np.asarray(w.points)
        for s, e in zip(pts[:-1], pts[1:]):
            if abs(e[0] - s[0]) > 50:
                return round(s[1], 3), round(s[2], 3)
    slots = [trunk_yz(w) for w in rib]
    assert None not in slots and len(set(slots)) == len(slots)
    # leads keep their own cable, not routed through a W-* only channel
    assert all(w.channels == [] for w in h.wires if w.lead_mm)
    with pytest.raises(Exception):
        route_harness(c, asm, anchors, channels=[ch], via={"W-*": ["nope"]})
