"""_template — a Raspberry Pi on a printed mounting plate, with a push button that toggles an LED.

``piforge new NAME`` copies this folder (and sets BOARD below). Then::

    piforge check projects/_template      # ERC, power, printability, assembly, twin wiring (fast)
    piforge build projects/_template      # build/: parts, renders, electronics docs, SPICE, report
    piforge twin test projects/_template  # run firmware/main.py on the digital twin with the scenarios
    piforge serve projects/_template      # 3D viewer, checks, SPICE and the interactive twin

``build(p)`` describes the whole device: metadata, the circuit (``piforge.elec``), printed parts and
the assembly (``piforge.mech``; mm, Z up, print bed at z = 0), firmware, twin scenarios and SPICE
benches. Everything a build reports lands in ``build/report.md``.
"""

from __future__ import annotations

from piforge.mech import EPS, PartSpec, get_board, rounded_box, standoff, to_location
from piforge.project import Project
from piforge.twin.scenario import Scenario, Step

BOARD = "rpi4b"  # rpi5, rpi4b, rpi3bp or rpizero2w — `piforge new --board` rewrites this line
PRINTER = "generic"  # printer profile for printability checks (`piforge info printers`)
MATERIAL = "PETG"  # filament of the printed parts (`piforge info materials`)

# Plate design parameters (mm) — choices, not measurements
PLATE_T = 3.0  # plate thickness
MARGIN = 5.0  # free border around the board
PANEL = 22.0  # strip beyond the GPIO-header edge that carries the button and the LED
STANDOFF_GAP = 3.0  # air gap below the board's lowest bottom-side part
SCREW = "M2.5"  # src: Raspberry Pi mechanical drawings — 2.7 mm mounting holes take M2.5 screws


def board_origin(board) -> tuple[float, float, float]:
    """Where the board frame (PCB lower-left corner, PCB bottom) sits in the plate frame."""
    width = board.width + 2 * MARGIN + PANEL
    return (-board.length / 2, -width / 2 + MARGIN, PLATE_T + board.bottom_clearance + STANDOFF_GAP)


def mounting_plate(board) -> PartSpec:
    """Plate under the board with an M2.5 tap standoff on each of the board's mounting holes."""
    width = board.width + 2 * MARGIN + PANEL
    plate = rounded_box(board.length + 2 * MARGIN, width, PLATE_T, radius=4.0, bottom_chamfer=0.4)
    x0, y0, z0 = board_origin(board)
    height = z0 - PLATE_T
    for hx, hy in board.holes:  # standoffs reach EPS into the plate: no coplanar faces in the union
        boss = standoff(SCREW, height + EPS, hole="tap", printer=PRINTER)
        plate = plate + boss.moved(to_location((x0 + hx, y0 + hy, PLATE_T - EPS)))
    return PartSpec("pi_plate", plate, material=MATERIAL, color="#3b82f6", print_rotation=(0, 0, 0))


def tact_button() -> PartSpec:
    """6 × 6 mm tact switch (reference model, no pins)."""
    # src: 6x6 mm THT tact switch outline (e.g. Omron B3F-1000 family): 6.0 x 6.0 x 3.5 mm body, Ø3.5 mm actuator
    body = rounded_box(6.0, 6.0, 3.5, radius=0.3)
    cap = rounded_box(3.5, 3.5, 1.5 + EPS, radius=1.7).moved(to_location((0, 0, 3.5 - EPS)))
    return PartSpec("button", body + cap, kind="reference", material="ABS", color="#2b2b2b")


def led_5mm() -> PartSpec:
    """5 mm LED (reference model, no legs); glows in the 3D view when the twin turns D1 on."""
    # src: 5 mm (T-1 3/4) LED outline, e.g. Kingbright L-53 series: Ø5.8 mm x 1.0 mm flange, Ø5.0 mm body, 8.6 mm tall
    flange = rounded_box(5.8, 5.8, 1.0, radius=2.85)
    dome = rounded_box(5.0, 5.0, 7.6 + EPS, radius=2.45, top_radius=2.4).moved(to_location((0, 0, 1.0 - EPS)))
    return PartSpec("led", flange + dome, kind="reference", material="epoxy", color="#ff3b30")


def build(p: Project) -> None:
    """Describe the device (called by ``piforge build`` / ``check`` / ``serve``)."""
    p.meta(description="Raspberry Pi on a printed mounting plate; a button toggles an LED.",
           board=BOARD, printer=PRINTER, material=MATERIAL)

    # -- electronics: GPIO17 ← button → GND (pull-up), GPIO27 → 330 Ω → LED → GND ---------------
    c = p.circuit
    pi = c.add(BOARD, "U1")
    sw = c.add("pushbutton", "SW1")
    led = c.add("led", "D1", color="red")
    r1 = c.add("resistor", "R1", value=330)  # ≈ (3.3 V − 2.0 V) / 330 Ω ≈ 3.9 mA through the LED
    c.connect(pi["GPIO17"], sw["A"])
    c.connect(sw["B"], pi["GND"])
    c.configure(pi, pulls={"GPIO17": "up"})  # gpiozero's Button(17) enables the internal pull-up
    c.connect(pi["GPIO27"], r1["1"])
    c.connect(r1["2"], led["A"])
    c.connect(led["K"], pi["GND"])

    # -- mechanics: printed plate + reference models placed in the assembly ---------------------
    board = get_board(BOARD)
    plate = mounting_plate(board)
    p.add_printed(plate)  # → parts/pi_plate.stl|3mf|step + printability report
    asm = p.assembly
    asm.add(plate, id="plate")
    asm.add(board.part(), board_origin(board), id="pi", explode=(0, 0, 40))
    panel_y = (board.width + 2 * MARGIN + PANEL) / 2 - PANEL / 2
    asm.add(tact_button(), (-12, panel_y, PLATE_T), id="button", explode=(0, 0, 25))
    asm.add(led_5mm(), (12, panel_y, PLATE_T), id="led", explode=(0, 0, 25),
            emissive_from={"device": "D1", "prop": "brightness", "color": "#ff2200"})

    # -- firmware, digital-twin scenario, SPICE bench --------------------------------------------
    p.firmware("firmware/main.py")
    p.scenario(Scenario("button_toggles_led", duration=2.4, steps=[
        Step(at=0.5, action="input", device="SW1", prop="pressed", value=True),
        Step(at=0.7, action="input", device="SW1", prop="pressed", value=False),
        Step(at=0.8, action="expect", device="D1", prop="brightness", value=1.0, tol=0.01),
        Step(at=0.8, action="expect_log", pattern="LED on"),
        Step(at=1.4, action="input", device="SW1", prop="pressed", value=True),
        Step(at=1.6, action="input", device="SW1", prop="pressed", value=False),
        Step(at=1.8, action="expect", device="D1", prop="brightness", value=0.0, tol=0.01),
    ]))
    p.spice("led_driver", label="status_led", r_series=330, led="led_red")
