"""demo_gauge — a desk climate gauge that exercises every PiForge subsystem end to end.

A Raspberry Pi 4B in a printed PETG enclosure. A BME280 measures temperature, humidity and
pressure; an SSD1306 OLED in a lid window shows them; an SG90 servo turns a printed needle over a
printed 0–40 °C dial on the lid; a push button cycles the display modes; a red status LED blinks on
every reading; a 30 mm 5 V intake fan, switched by an AO3400 low-side MOSFET with a 1N5819 flyback
diode, runs above 28 °C.

::

    piforge check projects/demo_gauge
    piforge build projects/demo_gauge --scenarios     # parts, renders, docs, SPICE, twin scenarios
    piforge serve projects/demo_gauge                 # 3D view + interactive twin (needle follows T)

Layout (enclosure frame, mm, Z up, front = −Y): the box is 36 mm longer than the Pi on the −X
side. That compartment holds the servo (hung from the lid), the intake fan behind a grille in the
front wall and the BME280 on the end wall next to inlet slots. The OLED, button and LED sit in the
lid over the Pi; warm air leaves through slots high in the back (+Y) wall.
"""

from __future__ import annotations

import math
from dataclasses import replace

from parts import (
    DIAL_SCREW_L, SPLINE_XY, bme280_module, dial, dial_screw_xy, dial_z, gauge_base, gauge_lid,
    m2_screw, needle, needle_angle, needle_z, servo_location, SG90,
)

from piforge.analysis.thermal import FAN_30MM_CFM, ThermalInputs
from piforge.mech import Enclosure, EnclosureSpec, Joint, PanelItem, VentSpec, get_module
from piforge.project import Project
from piforge.twin.config import DeviceConfig
from piforge.twin.scenario import Scenario, Step

BOARD = "rpi4b"
PRINTER = "prusa_mk4"
MATERIAL = "PETG"

# -- enclosure layout (design choices, mm) ---------------------------------------------------
COMPARTMENT = 36.0  # extra room on −X for the servo, the fan and the sensor
INNER_H = 34.0  # cavity height: the 30 mm fan stands on the front wall below the lid lip
OLED_XY = (12.0, -8.0)  # lid, over the SoC — clear of the GPIO header strip (jumper wires)
BUTTON_XY = (40.0, -14.0)
LED_XY = (40.0, 8.0)
FAN_UV = (-41.0, -1.5)  # front wall (−Y face: u = world x, v = z − wall centre)
BME_UV = (-16.0, -6.0)  # end wall (−X face: u = −world y, v = z − wall centre)
INLET = VentSpec("-x", size=(30.0, 12.0), offset=(10.0, -9.0))  # low, next to the BME280
EXHAUST = VentSpec("+y", size=(100.0, 14.0), offset=(0.0, 7.0))  # high on the back wall
LID_VENT = VentSpec("top", size=(26.0, 14.0), offset=(42.0, 22.0))  # over the Ethernet jack
PORTS = ("power", "hdmi0", "hdmi1", "audio", "usb2", "usb3", "ethernet")  # SD card: open the lid

# -- electrical / thermal operating point -------------------------------------------------------
# src: pidramble.com Pi 4B power: 2.7 W idle, 6.4 W `stress --cpu 4`. The firmware wakes twice a
# second (near idle): 2.7 W + ≈ 0.3 W OLED/servo hold/LED/sensor + 0.5 W headroom = 3.5 W typical.
TYPICAL_POWER_W = 3.5
SLOT_W, SLOT_PITCH = 2.0, 4.0  # src: piforge.mech.primitives.vent_slots defaults (wall slots, square ends)

_FAN = get_module("fan_30mm")


def slot_area(v: VentSpec) -> float:
    """Open area (mm²) of a slot vent: vertical square-ended slots on walls, round-ended slots along
    the longer side on the lid (the rules of piforge.mech.enclosure)."""
    w, h = v.size
    if v.face == "top":
        length, span = (w, h) if w >= h else (h, w)
        n = int(math.floor((span - SLOT_W) / SLOT_PITCH + 1e-9)) + 1
        return n * (SLOT_W * length - (4 - math.pi) * (SLOT_W / 2) ** 2)
    n = int(math.floor((w - SLOT_W) / SLOT_PITCH + 1e-9)) + 1
    return n * SLOT_W * h


def grille_area() -> float:
    """Open area (mm²) of the round fan opening minus the vertical grille bars (the flattened
    teardrop roof above the circle is ignored: a conservative estimate)."""
    from parts import GRILLE_BAR, GRILLE_PITCH

    d = _FAN.window[2][0] + 0.6  # window + the enclosure's 0.3 mm clearance per side
    r = d / 2
    n = int(r // GRILLE_PITCH)
    bars = sum(2 * math.sqrt(max(r * r - (k * GRILLE_PITCH) ** 2, 0.0)) * GRILLE_BAR for k in range(-n, n + 1))
    return math.pi * r * r - bars


def enclosure_spec() -> EnclosureSpec:
    return EnclosureSpec(
        board=BOARD, extra_space=(COMPARTMENT, 0, 0, 0), inner_height=INNER_H, ports=PORTS,
        panel_items=(
            PanelItem("ssd1306_096_i2c", "top", OLED_XY),
            PanelItem("pushbutton_12mm", "top", BUTTON_XY),
            PanelItem("led_5mm", "top", LED_XY),
            PanelItem("fan_30mm", "-y", FAN_UV),  # teardrop window, flattened below the rim
            PanelItem(bme280_module(), "-x", BME_UV),
        ),
        vents=(INLET, EXHAUST, LID_VENT), printer=PRINTER, material=MATERIAL)


def circuit(p: Project) -> None:
    c = p.circuit
    pi = c.add(BOARD, "U1")
    c.add("psu_usbc_5v3a", "PS1")  # official 15 W supply in the Pi's USB-C socket (no wiring)
    c.configure(pi, interfaces={"i2c": True}, pulls={"GPIO17": "up"})
    # I2C1: BME280 (0x76) and the OLED (0x3C) on the 3V3 rail
    env = c.add("bme280_breakout", "U2", i2c_address=0x76)
    oled = c.add("ssd1306_096_i2c", "U3", i2c_address=0x3C)
    for dev, vcc in ((env, "VIN"), (oled, "VCC")):
        c.connect(pi["3V3"], dev[vcc])
        c.connect(pi["GND"], dev["GND"])
        c.connect(pi["GPIO2"], dev["SDA"])
        c.connect(pi["GPIO3"], dev["SCL"])
    # SG90 on 5 V, signal on GPIO18
    servo = c.add("sg90_servo", "M1")
    c.connect(pi["5V"], servo["V+"])
    c.connect(pi["GND"], servo["GND"])
    c.connect(pi["GPIO18"], servo["SIG"])
    # mode button GPIO17 → GND (internal pull-up), status LED GPIO27 → 330 Ω → red LED → GND
    sw = c.add("pushbutton_12mm", "SW1")
    c.connect(pi["GPIO17"], sw["A"])
    c.connect(sw["B"], pi["GND"])
    led = c.add("led", "D1", color="red")
    r1 = c.add("resistor", "R1", value=330)  # (3.3 V − 2.0 V) / 330 Ω ≈ 3.9 mA
    c.connect(pi["GPIO27"], r1["1"])
    c.connect(r1["2"], led["A"])
    c.connect(led["K"], pi["GND"])
    # 5 V fan, low-side AO3400: gate GPIO22 via 100 Ω, 100 kΩ gate pull-down, 1N5819 across the fan
    fan = c.add("fan_5v", "M2")
    q = c.add("nmos_ao3400", "Q1")
    rg = c.add("resistor", "R2", value=100)  # limits the gate charging current spike
    rpd = c.add("resistor", "R3", value=100_000)  # keeps the fan off while GPIO22 floats at boot
    fly = c.add("schottky_1n5819", "D2")
    c.connect(pi["5V"], fan["+"], fly["K"])
    c.connect(fan["-"], q["D"], fly["A"], name="FAN_SW")
    c.connect(q["S"], pi["GND"])
    c.connect(pi["GPIO22"], rg["1"])
    c.connect(rg["2"], q["G"], rpd["1"], name="FAN_GATE")
    c.connect(rpd["2"], pi["GND"])


def mechanics(p: Project) -> Enclosure:
    enc = Enclosure(enclosure_spec())
    fan_c = (FAN_UV[0], -enc.inner_size[1] / 2, enc.spec.floor + enc.inner_size[2] / 2 + FAN_UV[1])
    base = gauge_base(enc, fan_c, _FAN.window[2][0] + 0.6)  # + grille bars in the fan window
    lid = gauge_lid(enc, PRINTER)
    dial_printed, dial_scene = dial(PRINTER)
    needle_part = needle()
    p.add_printed(base, lid, dial_printed, needle_part)

    asm = p.assembly
    src = enc.assembly()
    lift = enc.outer_size[2] + 15.0
    names = {f"module_{i}_{key}": new for i, (key, new) in enumerate(
        (("ssd1306_096_i2c", "oled"), ("pushbutton_12mm", "button"), ("led_5mm", "led"),
         ("fan_30mm", "fan"), ("bme280_breakout", "bme280")))}
    glow = {"led": {"device": "D1", "prop": "brightness", "color": "#ff2200"},
            "fan": {"device": "M2", "prop": "on", "color": "#38bdf8"}}  # fan glows blue while running
    labels = {"oled": "OLED 0.96in (U3)", "button": "mode button (SW1)", "led": "status LED (D1)",
              "fan": "30 mm fan (M2)", "bme280": "BME280 breakout (U2)"}
    for n in src.nodes:
        nid = names.get(n.id, n.id)
        part = {"base": base, "lid": lid}.get(n.id, n.part)
        if nid in labels:
            part = replace(part, name=labels[nid])
        asm.add(part, n.loc, id=nid, explode=n.explode, emissive_from=glow.get(nid))
    x, y = SPLINE_XY
    asm.add(SG90.part("SG90 servo (M1)"), servo_location(enc), id="servo", explode=(0, 0, lift))
    asm.add(dial_scene, (x, y, dial_z(enc)), id="dial", explode=(0, 0, lift * 1.3))
    for i, (sx, sy) in enumerate(dial_screw_xy()):
        asm.add(m2_screw(DIAL_SCREW_L), (sx, sy, dial_z(enc) + dial_printed.meta["filament_change_mm"]),
                id=f"dial_screw_{i}", explode=(0, 0, lift * 1.7))
    asm.add(needle_part, (x, y, needle_z(enc)), id="needle", explode=(0, 0, lift * 1.5),
            joint=Joint("revolute", axis=(0, 0, 1), min=-90.0, max=90.0, value=needle_angle(22.0),
                        driven_by={"device": "M1", "prop": "angle", "scale": 1.0, "offset": 0.0}))
    p.add_check(enc.checks, name="enclosure")  # port access, wall/standoff/screw checks
    return enc


def thermal(p: Project, enc: Enclosure) -> None:
    zc = enc.spec.floor + enc.inner_size[2] / 2
    z_lid = enc.spec.floor + enc.inner_size[2] + enc.spec.lid_thickness / 2
    # (area mm², height of the opening's centre mm); the idle fan's grille is an opening too
    inlets = [(grille_area(), zc + FAN_UV[1]), (slot_area(INLET), zc + INLET.offset[1])]
    outlets = [(slot_area(EXHAUST), zc + EXHAUST.offset[1]), (slot_area(LID_VENT), z_lid)]
    vents_in, vents_out = sum(a for a, _ in inlets), sum(a for a, _ in outlets)
    height = (sum(a * z for a, z in outlets) / vents_out) - (sum(a * z for a, z in inlets) / vents_in)
    vented = ThermalInputs(power_w=TYPICAL_POWER_W, outer_mm=enc.outer_size, wall_mm=enc.spec.wall,
                           material=MATERIAL, vent_in_mm2=vents_in, vent_out_mm2=vents_out,
                           vent_height_mm=height)
    # build/thermal.json: {"fan off": …, "fan on": …}; findings "thermal:fan off" / "thermal:fan on"
    p.thermal(vented, label="fan off")  # stack-effect ventilation only (below 28 °C)
    p.thermal(ThermalInputs(**{**vented.__dict__, "fan_cfm": FAN_30MM_CFM}), label="fan on")


# The bench's fan_5v load: 30 mm 5 V BLDC fan, 50 Ω (100 mA) + 2 mH + 100 nF driver input capacitance.
FAN_SWITCH = dict(mosfet="nmos_ao3400", v_supply=5.0, load="fan_5v", diode="d1n5819",
                  r_gate=100.0, r_pulldown=100_000.0)


def build(p: Project) -> None:
    """Describe the device (called by ``piforge build`` / ``check`` / ``serve``)."""
    p.meta(description="Desk climate gauge: BME280 + OLED + servo needle on a dial, mode button, status LED, "
                       "temperature-controlled fan, Pi 4B in a printed PETG enclosure.",
           board=BOARD, printer=PRINTER, material=MATERIAL)
    circuit(p)
    enc = mechanics(p)
    thermal(p, enc)

    # -- firmware and digital twin ---------------------------------------------------------------
    p.firmware("firmware/main.py")
    # SG90: 0.5–2.4 ms ≈ 180°, mapped to ±90° — the same values firmware/main.py gives AngularServo
    p.twin_override("M1", min_pulse=0.0005, max_pulse=0.0024, min_angle=-90.0, max_angle=90.0)
    # the fan has no twin model: a relay-type output on GPIO22 shows whether the MOSFET is driven
    p.twin_device(DeviceConfig("M2", "relay", pins={"pin": 22}, params={"active_high": True}))

    t22, t31 = needle_angle(22.0), needle_angle(31.0)
    p.scenario(Scenario("needle_at_22C", duration=3.5, steps=[
        Step(at=0.0, action="input", device="U2", prop="temperature", value=22.0),
        Step(at=2.5, action="expect", device="M1", prop="angle", value=t22, op="~=", tol=3.0),
        Step(at=2.5, action="expect", device="M2", prop="on", value=False),
        Step(at=2.5, action="expect_log", pattern=r"T=22\.\d H=\d+ P=\d+\.\d fan=0 mode=0"),
    ]))
    p.scenario(Scenario("fan_on_above_28C", duration=4.5, settle=1.5, steps=[
        Step(at=0.0, action="input", device="U2", prop="temperature", value=22.0),
        Step(at=1.5, action="expect", device="M2", prop="on", value=False),
        Step(at=2.5, action="input", device="U2", prop="temperature", value=31.0),
        Step(at=2.5, action="expect", device="M2", prop="on", value=True),  # within 1.5 s (settle)
        Step(at=2.5, action="expect", device="M1", prop="angle", value=t31, op="~=", tol=3.0),
        Step(at=2.5, action="expect_log", pattern=r"T=31\.\d .*fan=1"),
    ]))
    p.scenario(Scenario("button_cycles_mode", duration=4.5, steps=[
        Step(at=1.5, action="expect_log", pattern=r"mode=0"),
        Step(at=2.0, action="input", device="SW1", prop="pressed", value=True),
        Step(at=2.15, action="input", device="SW1", prop="pressed", value=False),
        Step(at=2.2, action="expect_log", pattern=r"mode=1"),
        Step(at=3.2, action="input", device="SW1", prop="pressed", value=True),
        Step(at=3.35, action="input", device="SW1", prop="pressed", value=False),
        Step(at=3.4, action="expect_log", pattern=r"mode=2"),
    ]))
    p.scenario(Scenario("display_and_status_led", duration=3.0, steps=[
        Step(at=1.5, action="expect", device="U3", prop="on", value=True),
        Step(at=1.5, action="expect", device="U3", prop="lit_pixels", value=100, op=">"),
        Step(at=1.5, action="expect", device="D1", prop="brightness", value=1.0, tol=0.01),
    ]))

    # -- SPICE benches -----------------------------------------------------------------------------
    p.spice("led_driver", label="status_led", r_series=330, led="led_red")
    p.spice("mosfet_lowside", label="fan_switch", flyback=True, **FAN_SWITCH)
    # what-if without D2: the drain rings to ≈ 14 V — SPICE.FLYBACK_OVERVOLTAGE WARNING (below the
    # AO3400's 30 V rating); the built design has D2 (bench fan_switch, ≈ 5.4 V)
    p.spice("mosfet_lowside", label="fan_switch_no_flyback", **{**FAN_SWITCH, "flyback": False})
    p.spice("i2c_rise_time", label="i2c_bus", r_pullup=1800, c_bus=100e-12)
    # 5.1 V supply through a 0.15 Ω cable; Pi + OLED + fan ≈ 0.75 A, servo stalls (+0.65 A) for 50 ms
    p.spice("power_path", label="servo_stall", v_psu=5.1, r_cable=0.15, i_idle=0.75, i_load=1.4, t_hold=0.05)
