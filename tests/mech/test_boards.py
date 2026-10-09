"""Golden-data and geometry tests for the Raspberry Pi board models.

Golden values come from the official Raspberry Pi mechanical drawings:
RP-008343-DS (Pi 4 B), RP-008347-DS (Pi 5), RP-008337-DS (Pi 3 B+), RP-008358-DS (Zero 2 W).
Data checks are fast; ``shape()`` checks need the OCC kernel (slow).
"""

from __future__ import annotations

import math

import pytest

from piforge.core.errors import NotFoundError

STANDARD_HOLES = [(3.5, 3.5), (3.5, 52.5), (61.5, 3.5), (61.5, 52.5)]


def _edge_plane(board, direction):
    dx, dy, _ = direction
    if dx > 0.5:
        return 0, board.length
    if dx < -0.5:
        return 0, 0.0
    if dy > 0.5:
        return 1, board.width
    return 1, 0.0


def test_board_golden_dims():
    from piforge.mech.boards import BOARDS, get_board

    assert set(BOARDS) == {"rpi5", "rpi4b", "rpi3bp", "rpizero2w"}

    pi4 = get_board("rpi4b")
    assert (pi4.length, pi4.width) == (85, 56)  # src: RP-008343-DS "85", "56"
    assert sorted(pi4.holes) == STANDARD_HOLES  # src: 3.5 / 58 / 49
    assert pi4.hole_d == pytest.approx(2.7)  # src: drawing "2.7"
    assert pi4.corner_radius == pytest.approx(3.0)  # src: "CORNER RADIUS = 3.0mm"

    pi5 = get_board("rpi5")
    assert (pi5.length, pi5.width) == (85, 56)
    assert sorted(pi5.holes) == STANDARD_HOLES
    assert pi5.hole_d == pytest.approx(2.7)  # src: RP-008347-DS "ø2.7"

    pi3 = get_board("rpi3bp")
    assert (pi3.length, pi3.width) == (85, 56)
    assert sorted(pi3.holes) == STANDARD_HOLES
    assert pi3.hole_d == pytest.approx(2.75)  # src: RP-008337-DS "2.75"

    zero = get_board("rpizero2w")
    assert (zero.length, zero.width) == (65, 30)
    xs = sorted({h[0] for h in zero.holes})
    ys = sorted({h[1] for h in zero.holes})
    assert len(zero.holes) == 4
    assert xs == [3.5, 61.5] and ys == [3.5, 26.5]  # 58 x 23 spacing, 3.5 from every edge
    assert xs[1] - xs[0] == pytest.approx(58) and ys[1] - ys[0] == pytest.approx(23)

    for b in BOARDS.values():
        assert 1.0 <= b.thickness <= 1.7
        assert b.source and "drawing" in b.source.lower()


def test_board_known_connector_positions():
    from piforge.mech.boards import get_board

    pi4 = get_board("rpi4b")
    # bottom edge, from the drawing chain 3.5 + 7.7 / +14.8 / +13.5 / +7 +7.5
    assert pi4.port("power").center[0] == pytest.approx(11.2)
    assert pi4.port("hdmi0").center[0] == pytest.approx(26.0)
    assert pi4.port("hdmi1").center[0] == pytest.approx(39.5)
    assert pi4.port("audio").center[0] == pytest.approx(54.0)
    assert pi4.port("usb2").center[1] == pytest.approx(9.0)
    assert pi4.port("usb3").center[1] == pytest.approx(27.0)
    assert pi4.port("ethernet").center[1] == pytest.approx(45.75)
    assert pi4.port("gpio").center[:2] == pytest.approx((32.5, 52.5))

    pi5 = get_board("rpi5")
    assert pi5.port("power").center[0] == pytest.approx(11.2)
    assert pi5.port("hdmi0").center[0] == pytest.approx(25.8)
    assert pi5.port("hdmi1").center[0] == pytest.approx(39.2)
    assert pi5.port("ethernet").center[1] == pytest.approx(10.2)
    assert pi5.port("usb3").center[1] == pytest.approx(29.1)
    assert pi5.port("usb2").center[1] == pytest.approx(47.0)
    assert pi5.port("power_button").center[1] == pytest.approx(18.4)

    pi3 = get_board("rpi3bp")
    assert pi3.port("power").center[0] == pytest.approx(10.6)
    assert pi3.port("hdmi").center[0] == pytest.approx(32.0)
    assert pi3.port("audio").center[0] == pytest.approx(53.5)
    assert pi3.port("ethernet").center[1] == pytest.approx(10.25)

    zero = get_board("rpizero2w")
    assert zero.port("hdmi").center[0] == pytest.approx(12.4)
    assert zero.port("usb").center[0] == pytest.approx(41.4)
    assert zero.port("power").center[0] == pytest.approx(54.0)


def test_board_ports_on_edges():
    from piforge.mech.boards import BOARDS

    for b in BOARDS.values():
        names = [p.name for p in b.ports]
        assert len(names) == len(set(names)), b.key
        for p in b.ports:
            assert math.isclose(math.hypot(*p.direction), 1.0, abs_tol=1e-9), (b.key, p.name)
            assert all(v > 0 for v in p.opening), (b.key, p.name)
            assert all(v > 0 for v in p.plug), (b.key, p.name)
            assert all(v > 0 for v in p.body_size), (b.key, p.name)
            horizontal = abs(p.direction[2]) < 1e-9
            if horizontal:
                axis, plane = _edge_plane(b, p.direction)
                assert abs(p.center[axis] - plane) <= 3.0 + 1e-6, (b.key, p.name, p.center)
                other = 1 - axis
                limit = b.width if other == 1 else b.length
                assert 0 < p.center[other] < limit, (b.key, p.name)
            else:
                assert 0 < p.center[0] < b.length and 0 < p.center[1] < b.width
                assert p.center[2] > b.thickness


def test_board_heights_and_edge_ports():
    from piforge.mech.boards import get_board

    pi4 = get_board("rpi4b")
    assert pi4.top_height == pytest.approx(16.0)  # src: drawing "Z=16.0" (USB stacks)
    assert pi4.bottom_clearance >= 1.3
    assert {p.name for p in pi4.edge_ports()} == {
        "power", "hdmi0", "hdmi1", "audio", "usb2", "usb3", "ethernet", "sdcard"}
    assert get_board("rpi5").top_height == pytest.approx(16.0)
    assert get_board("rpizero2w").top_height == pytest.approx(8.5)  # GPIO header fitted


def test_board_lookup_aliases_and_errors():
    from piforge.mech.boards import BOARDS, get_board

    assert get_board("pi4") is BOARDS["rpi4b"]
    assert get_board("Raspberry Pi 5") is BOARDS["rpi5"]
    assert get_board("rpi3b+") is BOARDS["rpi3bp"]
    assert get_board("zero2w") is BOARDS["rpizero2w"]
    assert get_board(BOARDS["rpi5"]) is BOARDS["rpi5"]
    with pytest.raises(NotFoundError) as exc:
        get_board("rpi4c")
    assert "rpi4b" in str(exc.value)
    with pytest.raises(NotFoundError) as exc:
        BOARDS["rpi4b"].port("ethernt")
    assert "ethernet" in str(exc.value)


@pytest.mark.slow
def test_board_shape_valid():
    from piforge.mech.boards import BOARDS

    for b in BOARDS.values():
        shape = b.shape()
        assert shape.is_valid, b.key
        bb = shape.bounding_box()
        assert bb.max.Z == pytest.approx(b.thickness + b.top_height, abs=0.01), b.key
        assert bb.min.Z <= 0 and bb.min.Z >= -b.bottom_clearance - 1e-6, b.key

    pi4 = BOARDS["rpi4b"].shape().bounding_box()
    assert -0.5 <= pi4.min.X <= 0.01  # nothing sticks out on the SD-card side
    assert 87.0 <= pi4.max.X <= 88.5  # USB/Ethernet overhang 2–3 mm
    assert -3.0 <= pi4.min.Y <= -1.0  # audio jack barrel / USB-C mouth
    assert pi4.max.Y == pytest.approx(56.0, abs=0.01)


@pytest.mark.slow
def test_board_pcb_has_mounting_holes():
    from piforge.mech.boards import BOARDS

    b = BOARDS["rpi4b"]
    pcb = b.shape(components=False)
    assert pcb.is_valid
    r, t = b.corner_radius, b.thickness
    hole_area = 4 * math.pi * (b.hole_d / 2) ** 2
    expected = (b.length * b.width - (4 - math.pi) * r * r - hole_area) * t
    assert pcb.volume == pytest.approx(expected, rel=1e-4)

    b5 = BOARDS["rpi5"]
    extra = sum(math.pi * (d / 2) ** 2 for _, _, d in b5.extra_holes)
    assert extra > 0  # the two ø3 holes next to the mounting holes
    area5 = (b5.length * b5.width - (4 - math.pi) * b5.corner_radius**2
             - 4 * math.pi * (b5.hole_d / 2) ** 2 - extra)
    assert b5.shape(components=False).volume == pytest.approx(area5 * b5.thickness, rel=1e-4)


@pytest.mark.slow
def test_board_part_exports_coloured_scene(tmp_path):
    import json
    import struct

    from piforge.mech.assembly import Assembly
    from piforge.mech.boards import BOARDS

    board = BOARDS["rpi4b"]
    part = board.part()
    assert part.kind == "pcb" and part.meta["board"] == "rpi4b" and part.volume > 0
    asm = Assembly("pi")
    asm.add(part, (0, 0, 5), id="pi")
    scene = json.loads(asm.to_scene(tmp_path).read_text(encoding="utf-8"))
    lo, hi = scene["bounds"]
    assert lo[2] == pytest.approx(5 - 1.3, abs=0.01) and hi[2] == pytest.approx(5 + 1.4 + 16.0, abs=0.01)
    raw = (tmp_path / scene["nodes"][0]["mesh"]).read_bytes()
    doc = json.loads(raw[20:20 + struct.unpack("<I", raw[12:16])[0]])
    colours = {tuple(round(c, 2) for c in m["pbrMetallicRoughness"]["baseColorFactor"]) for m in doc["materials"]}
    assert len(doc["meshes"]) == 1 + len(board.ports) + len(board.components)  # one per child box
    assert len(colours) >= 3  # PCB green + connector metal + dark parts


def test_rj45_plug_envelope_matches_jack_width():
    from piforge.mech.boards import BOARDS

    for b in BOARDS.values():
        for p in b.ports:
            if p.kind == "rj45":
                assert p.plug[:2] == (16.0, 14.0), b.key


def test_pi5_low_internal_parts_are_modelled():
    from piforge.mech.boards import get_board

    comps = {name: (c, s) for name, c, s in get_board("rpi5").components}
    c, s = comps["rtc_battery"]
    assert c[:2] == pytest.approx((19.0, 4.7)) and s == pytest.approx((4.0, 2.9, 3.0))
    c, s = comps["aux_conn_67_44"]
    assert c[:2] == pytest.approx((67.5, 44.0)) and s == pytest.approx((4.3, 7.3, 4.4))
    assert c[2] == pytest.approx(get_board("rpi5").thickness + 2.2)  # sits on the top face
