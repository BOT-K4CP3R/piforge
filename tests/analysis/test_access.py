"""Port access: plug envelopes vs obstacles (enclosure walls, modules)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow


def _wall(x0, x1, y0, y1, z0, z1):
    from build123d import Align, Box, Location

    b = Box(x1 - x0, y1 - y0, z1 - z0, align=(Align.MIN, Align.MIN, Align.MIN))
    return b.moved(Location((x0, y0, z0)))


def test_plug_envelope_geometry():
    from build123d import Location

    from piforge.analysis.access import plug_envelope
    from piforge.mech.boards import get_board

    pi = get_board("rpi4b")
    port = pi.port("power")  # -y edge, mouth at y = -1.3
    env = plug_envelope(pi, port, Location((10, 20, 5)))
    bb = env.bounding_box()
    w, h, depth = port.plug
    assert bb.max.Y == pytest.approx(20 + port.center[1], abs=1e-6)
    assert bb.min.Y == pytest.approx(20 + port.center[1] - depth, abs=1e-6)
    assert bb.max.X - bb.min.X == pytest.approx(w)
    assert bb.max.Z - bb.min.Z == pytest.approx(h)
    assert (bb.min.Z + bb.max.Z) / 2 == pytest.approx(5 + port.center[2])

    grown = plug_envelope(pi, port, Location(), gap=0.5).bounding_box()
    assert grown.max.X - grown.min.X == pytest.approx(w + 1.0)

    usb = plug_envelope(pi, pi.port("usb3"), Location()).bounding_box()  # +x edge
    assert usb.min.X == pytest.approx(pi.port("usb3").center[0])
    gpio = plug_envelope(pi, pi.port("gpio"), Location()).bounding_box()  # +z
    assert gpio.min.Z == pytest.approx(pi.port("gpio").center[2])


def test_check_port_access():
    from build123d import Location

    from piforge.analysis.access import check_port_access
    from piforge.mech.boards import get_board

    pi = get_board("rpi4b")
    loc = Location()
    clear = check_port_access(pi, loc, [])
    assert clear.ok and len(clear.by_code("ACCESS.PORT_OK")) == len(pi.edge_ports())

    # a wall in front of the -y edge blocks power/hdmi/audio but nothing else
    wall = _wall(-5, 90, -8, -6, -5, 20)
    rep = check_port_access(pi, loc, [("front_wall", wall)])
    blocked = {f.data["port"] for f in rep.by_code("ACCESS.PORT_BLOCKED")}
    assert blocked == {"power", "hdmi0", "hdmi1", "audio"}
    f = rep.by_code("ACCESS.PORT_BLOCKED")[0]
    assert f.data["obstacle"] == "front_wall" and f.data["volume_mm3"] > 0

    only = check_port_access(pi, loc, [wall], ports=["usb2", "gpio"])
    assert only.ok and len(only.by_code("ACCESS.PORT_OK")) == 2


# --- fix round 1: fingertip access for cards / buttons ---------------------------------------
def test_finger_envelope_and_gap():
    from build123d import Location

    from piforge.analysis.access import FINGERTIP, finger_envelope, finger_gap
    from piforge.mech.boards import get_board

    pi = get_board("rpi4b")
    sd = pi.port("sdcard")  # -x edge, mouth at x = 2.0
    w, h, depth = FINGERTIP["microsd"]
    bb = finger_envelope(pi, sd, Location(), offset=1.0).bounding_box()
    assert bb.max.X == pytest.approx(sd.center[0] - 1.0)
    assert bb.max.X - bb.min.X == pytest.approx(depth)
    assert bb.max.Y - bb.min.Y == pytest.approx(w) and bb.max.Z - bb.min.Z == pytest.approx(h)

    assert finger_gap(pi, sd, Location(), []) == pytest.approx(0.0)
    wall = _wall(-6, -4, -10, 70, -10, 20)  # solid wall in front of the card: finger stops outside it
    assert finger_gap(pi, sd, Location(), [wall]) == pytest.approx(sd.center[0] + 6.0, abs=0.06)
    from build123d import Align, Box
    notch = Box(2.2, w + 1.0, h + 1.0, align=(Align.MAX, Align.CENTER, Align.CENTER)).moved(
        Location((-3.9, sd.center[1], sd.center[2])))
    notched = wall - notch  # a fingertip-sized notch lets the finger through
    assert finger_gap(pi, sd, Location(), [notched]) == pytest.approx(0.0, abs=0.06)
