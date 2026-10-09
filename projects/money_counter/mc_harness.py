"""Wiring harness of the money counter (imported by ``project.py``): every wire in 3D.

Physical topology (the circuit stays the same — this decides where each wire really goes):

* **Perfboard = power hub.** The DC jack's two lugs (20 AWG) are soldered to the perfboard's 5 V/GND
  rail (pads ``JDC``); everything else draws 5 V/GND from the perfboard.
* **Pi ↔ perfboard: ``W-SPI``** — 5 DuPont wires from header pins 19 (MOSI → SER), 23 (SCLK), 24 (CE0
  → RCLK), 2 (5 V) and 6 (GND) to the perfboard header ``JP``.
* **Perfboard → driver i: ``W-DRV<i>``** — one 6-wire DuPont ribbon per ULN2003 board from header
  ``J<i>`` (IN1…IN4 = the chain outputs, 5 V, GND) to the board's IN1–IN4 and − + pins. The eight
  ribbons drop to the floor trunk on the cable-tie anchors behind the drivers and leave it at their
  driver.
* **Driver → motor: the 28BYJ-48's own 230 mm lead** with its JST XH plug in the driver's socket.
* **Hall sensors:** each OUT wire goes straight to its GPIO on the Pi header; 5 V and GND run as a
  soldered daisy-chain bus (perfboard ``JH`` → H1 → H2 … → H8). All Hall wires leave the frame
  through the lead slot, pass the gap between two modules and join the upper trunk.

Channels (world frame, see ``mc_mech``): ``floor_trunk`` along x on the cable-tie anchors behind
the driver boards (between their back edge and the back pillars), ``upper_trunk`` along x half-way
up the compartment, under the Pi and the perfboard.
"""

from __future__ import annotations

from mc_circuit import DRIVER_REF, N, QS, SR_REFS

from piforge.mech import get_board, get_module
from piforge.mech.harness import Channel, Perfboard, route_harness

PERFBOARD = "perfboard_50x70"
# driver-ribbon wires: 26 AWG DuPont jumpers (ULN2003 inputs + one driver's 5 V); bus/power 22–20 AWG
GAUGES = {**{f"U{11 + i}": 26 for i in range(N)}, **{f"H{i + 1}": 26 for i in range(N)}}
COLORS = {"IN1": "green", "IN2": "purple", "IN3": "grey", "IN4": "cyan", "OUT": "white"}


def perfboard_layout() -> Perfboard:
    """Pads/headers of the 74HCT595 perfboard (27 × 19 holes; module frame, component side +Z).

    The board hangs component side DOWN under the top, so module +y points to the front (modules)
    and row 0 is at the back. J1…J8 (1 × 6) along the back edge, JP (Pi, 1 × 5) and JH (Hall bus,
    1 × 2) at the right edge, the DC-in pads JDC at the back right corner, the four DIP-16s between.
    """
    pb = Perfboard(PERFBOARD, ref="PB1")
    for i in range(N):
        sr = SR_REFS[i // 2]
        qs = QS[4 * (i % 2):4 * (i % 2) + 4]
        drv = DRIVER_REF.format(11 + i)
        pins = [(sr, "Q" + q) for q in qs] + [(sr, "VCC", drv), (sr, "GND", drv)]
        pb.header(f"J{i + 1}", 1 + 2 * i, 0, pins, along="y")
    head = SR_REFS[0]
    pb.header("JP", 25, 8, [(head, "SER"), (head, "SRCLK"), (head, "RCLK"), (head, "VCC", "U1"),
                            (head, "GND", "U1")], along="y")
    pb.header("JH", 25, 14, [(head, "VCC", "H1"), (head, "GND", "H1")], along="y")
    pb.pads("JDC", 25, 2, [(head, "VCC", "PS1"), (head, "GND", "PS1")], along="y")
    return pb


def channels(h) -> list[Channel]:
    """Cable channels in the electronics compartment of housing ``h`` (world frame)."""
    s = h.module.spec
    drv = get_module("uln2003_board")
    y_drv_back = h.electronics_layout["drivers"][0][1] + drv.pcb[1] / 2      # 72.0: back edge of the boards
    y_pillar = h._pillar_y[0]                                                  # 78.0: front face of the pillars
    x0, x1 = h.module_x(0) - 12.0, h.module_x(N - 1) + 8.0
    # loom on the tie anchors: between the drivers' back edge and the pillars, lying on the anchors (z 6)
    floor = Channel("floor_trunk", (x0, (y_drv_back + y_pillar) / 2, 6.6 + 12.0),
                    (x1, (y_drv_back + y_pillar) / 2, 6.6 + 12.0),
                    width=y_pillar - y_drv_back - 1.2, height=24.0, stack="bottom", only=("W-DRV*",))
    # upper trunk: under the Pi / perfboard headers, above the motors' lead exits
    z_up = h.axis_z + 12.0
    upper = Channel("upper_trunk", (h.module_x(0) + 20.0, s.frame_back + 14.0, z_up),
                    (h.module_x(N - 1) + 36.0, s.frame_back + 14.0, z_up), width=10.0, height=10.0,
                    only=("W-H*", "W-SPI", ""))                                  # "" = single wires
    return [floor, upper]


def add_harness(p, h) -> object:
    """Route the harness in ``p.assembly`` (built by ``mc_mech.add_mechanics``) and register it."""
    asm = p.assembly
    pb = perfboard_layout()
    asm.add(pb.part(), (0, 0, 0), id="perfboard_headers", parent="perfboard")
    m = h.module
    anchors = {"U1": ("board", get_board("rpizero2w").anchors),
               "PS1": ("dc_jack", get_module("dc_jack_panel_55x21").anchors)}
    anchors.update(pb.anchors_by_ref("perfboard"))
    for ref in ("C1", "C2", "C3", "C4", "C5"):           # on the perfboard, no off-board wire
        anchors[ref] = ("perfboard", [])
    for i in range(N):
        anchors[DRIVER_REF.format(11 + i)] = (f"m{i}_driver", get_module("uln2003_board").anchors)
        anchors[f"M{i + 1}"] = (f"m{i}_motor", get_module("stepper_28byj48").anchors)
        anchors[f"H{i + 1}"] = (f"m{i}_hall", m.hall_anchors())
    cables = {"W-SPI": ("U1", SR_REFS[0]), "W-PWR": ("PS1", SR_REFS[0])}
    cables.update({f"W-DRV{i + 1}": (SR_REFS[0], DRIVER_REF.format(11 + i)) for i in range(N)})
    cables["W-HALLBUS"] = (SR_REFS[0], "H1")
    harness = route_harness(
        p.circuit, asm, anchors, channels=channels(h), hubs={"5V": SR_REFS[0], "GND": SR_REFS[0]},
        chains=[(("5V", "GND"), [f"H{i + 1}" for i in range(N)])], cables=cables, colors=COLORS,
        gauges=GAUGES, via={"W-DRV*": ["floor_trunk"]},
        max_length=700.0,  # a 632 mm wide display: the far driver ribbon is ≈ 0.6 m
        name="money_counter")
    return p.harness(harness)
