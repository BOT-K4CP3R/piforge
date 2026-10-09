"""Hand-made fixture project + build directory for the server and GUI tests.

The build directory follows the binding layout contract shared with the build pipeline (Task 9):
``manifest.json``, ``report.json``, ``scene.json`` + ``meshes/*.glb``, ``parts/index.json`` + STLs,
``elec/*``, ``sim/*``, ``twin/*``. Meshes are made with trimesh primitives (no build123d), so the
fixture is fast and independent of the CAD kernel.

The scene is a small "gauge": an enclosure base + lid, a Raspberry Pi board, a dial with a needle
driven by servo ``SERVO1`` (revolute joint), a status LED glowing from device ``D1``, a push button
``SW1``, a printed bracket with an overhang and two screws sharing one mesh file.
"""

from __future__ import annotations

import contextlib
import functools
import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

FIRMWARE = '''\
"""Fixture firmware: button SW1 (GPIO27) lights LED D1 (GPIO17); servo SERVO1 (GPIO18) sweeps."""
import time

from gpiozero import LED, AngularServo, Button

led = LED(17)
button = Button(27)
servo = AngularServo(18, min_angle=-90, max_angle=90)

button.when_pressed = led.on
button.when_released = led.off
print("fixture firmware ready", flush=True)

angle, step = -60, 20
while True:
    servo.angle = angle
    angle += step
    if angle >= 60 or angle <= -60:
        step = -step
    time.sleep(0.25)
'''

TWIN_CONFIG: dict[str, Any] = {
    "board": "rpi4b",
    "devices": [
        {"id": "SW1", "type": "button", "pins": {"pin": 27}, "bus": None, "params": {}},
        {"id": "D1", "type": "led", "pins": {"pin": 17}, "bus": None, "params": {"color": "red"}},
        {"id": "SERVO1", "type": "servo", "pins": {"pin": 18}, "bus": None, "params": {}},
    ],
    "pulls": {},
}

SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "button_lights_led",
        "duration": 2.0,
        "steps": [
            {"at": 0.6, "action": "input", "device": "SW1", "prop": "pressed", "value": True},
            {"at": 1.2, "action": "expect", "device": "D1", "prop": "brightness", "value": 1.0,
             "op": "==", "tol": 0.01},
        ],
    }
]

NODE_IDS = ["base", "lid", "pi", "dial", "needle", "led", "button", "bracket", "screw1", "screw2"]
PRINTED = ["base", "lid", "dial", "needle", "bracket"]
NEEDLE_ANGLE = 30.0  # current joint value in scene.json (rest pose = 0)
PORT_W = 8.0  # width (mm) of the port window in the base's front wall; its roof is a bridge
WALL = 2.0  # base wall thickness (mm)


# ---------------------------------------------------------------------------------- geometry
def _box(lx: float, ly: float, lz: float, center: tuple[float, float, float]) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=[lx, ly, lz])
    m.apply_translation(center)
    return m


def _cyl(r: float, h: float, z0: float = 0.0, xy: tuple[float, float] = (0.0, 0.0),
         sections: int = 48) -> trimesh.Trimesh:
    m = trimesh.creation.cylinder(radius=r, height=h, sections=sections)
    m.apply_translation([xy[0], xy[1], z0 + h / 2.0])
    return m


def _union(*meshes: trimesh.Trimesh) -> trimesh.Trimesh:
    return trimesh.boolean.union(list(meshes), engine="manifold")


Leaf = tuple[str, trimesh.Trimesh, str]  # (name, mesh in the node frame, colour)


@functools.lru_cache(maxsize=1)
def make_leaves() -> dict[str, list[Leaf]]:
    """Coloured mesh leaves per GLB stem, in glTF node order, node frame, mm, Z up.

    Like ``piforge.mech.export.export_glb``: one PBR-coloured mesh per coloured leaf. ``pi`` and
    ``bracket`` have two leaves, so colour handling and face order are exercised.
    """
    outer = _box(124, 72, 32, (0, 0, 16))
    inner = _box(120, 68, 31, (0, 0, 2 + 15.5 + 0.01))
    # port window in the front (−Y) wall: its 8 × 2 mm roof spans 8 mm ≤ 10 mm → a bridge, not an overhang
    window = _box(PORT_W, 6, 5, (0, -35, 12))
    base = trimesh.boolean.difference([outer, inner, window], engine="manifold")
    pcb = _box(85, 56, 1.4, (42.5, 28, 0.7))
    parts = trimesh.util.concatenate([
        _box(21, 16, 13.5, (85 - 10.5 + 2, 45.75, 1.4 + 6.75)),   # Ethernet
        _box(17, 13, 15.5, (85 - 8.5 + 2, 27, 1.4 + 7.75)),       # USB
        _box(17, 13, 15.5, (85 - 8.5 + 2, 9, 1.4 + 7.75)),        # USB
        _box(51, 5, 8.5, (32.5, 52.5, 1.4 + 4.25)),               # GPIO header
        _box(15, 15, 2.4, (29, 32, 1.4 + 1.2))])                  # SoC
    led = _union(_cyl(2.5, 4.5, sections=32),
                 trimesh.creation.icosphere(subdivisions=2, radius=2.5).apply_translation([0, 0, 4.5]))
    return {
        "base": [("base", base, "#3b82f6")],
        "lid": [("lid", _box(124, 72, 3, (0, 0, 1.5)), "#93c5fd")],
        "pi": [("pcb", pcb, "#15803d"), ("components", parts, "#c3c9d2")],
        "dial": [("dial", _cyl(24, 2, sections=64), "#e5e7eb")],
        "needle": [("needle", _union(_box(22, 2.4, 1.6, (7, 0, 0.8)), _cyl(3, 1.6, sections=32)), "#ef4444")],
        "led": [("led", led, "#7f1d1d")],
        "button": [("button", _union(_cyl(6, 3), _cyl(3.5, 2, z0=3)), "#374151")],
        # stem r=5 h=12 + cap r=11 h=3: the ring under the cap overhangs (π(11²−5²) ≈ 301.6 mm²);
        # the gusset leaf comes FIRST, so a wrong concatenation order misaligns the mask.
        "bracket": [("gusset", _box(3, 4, 12, (7.5, 0, 6)), "#b45309"),
                    ("bracket", _union(_cyl(5, 12), _cyl(11, 3, z0=12)), "#f59e0b")],
        "screw_m2_5": [("screw", _union(_cyl(1.25, 8, z0=-8, sections=24), _cyl(2.25, 2.5, sections=24)),
                        "#9ca3af")],
    }


def concat(leaves: list[Leaf]) -> trimesh.Trimesh:
    """All leaves as one mesh, faces in glTF node order (the order the GUI and the server use)."""
    return trimesh.util.concatenate([m for _, m, _ in leaves])


def glb_leaf(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """A leaf as stored in the GLB: like ``mech.export.export_glb``, vertices are split along sharp
    edges (``smooth_shaded``), which also regroups the faces — the GLB face order is this mesh's."""
    return mesh.smooth_shaded


def write_glb(leaves: list[Leaf], path: Path) -> None:
    """GLB with one PBR-coloured mesh per leaf (same structure as ``mech.export.export_glb``)."""
    scene = trimesh.Scene()
    for i, (name, mesh, hexcol) in enumerate(leaves):
        m = glb_leaf(mesh)
        rgba = [int(hexcol[k:k + 2], 16) / 255.0 for k in (1, 3, 5)] + [1.0]
        m.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
            name=f"{name}_{hexcol[1:]}", baseColorFactor=rgba, metallicFactor=0.0, roughnessFactor=0.6))
        scene.add_geometry(m, node_name=f"{name}_{i}", geom_name=f"{name}_{i}")
    path.write_bytes(scene.export(file_type="glb", include_normals=True))


def glb_node_names(path: Path) -> list[str]:
    """Node names of a GLB in file order (independent of the server's GLB reader)."""
    data = Path(path).read_bytes()
    length = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + length])
    return [doc["nodes"][i].get("name", "") for i in doc["scenes"][doc.get("scene", 0)]["nodes"]]


def _matrix(t: tuple[float, float, float], rz: float = 0.0) -> np.ndarray:
    m = np.eye(4)
    c, s_ = np.cos(np.radians(rz)), np.sin(np.radians(rz))
    m[:2, :2] = [[c, -s_], [s_, c]]
    m[:3, 3] = t
    return m


def _colmajor(m: np.ndarray) -> list[float]:
    """4x4 → 16 floats, column-major (spec §5.4)."""
    return [float(v) for v in np.asarray(m).flatten(order="F")]


def _overhang_areas(mesh: trimesh.Trimesh, max_deg: float = 45.0, max_bridge: float = 10.0) -> tuple[float, float]:
    """(overhang area needing support, bridge area) in mm² — bridges excluded from the overhang,
    as in ``fab.analyze.PrintAnalysis`` (same rule: lean > max_deg + 1°, bed faces excluded)."""
    from piforge.fab.analyze import overhang_mask

    geometric = overhang_mask(mesh, max_deg)
    over = overhang_mask(mesh, max_deg, max_bridge_mm=max_bridge)
    areas = mesh.area_faces
    return float(areas[over].sum()), float(areas[geometric & ~over].sum())


# ---------------------------------------------------------------------------------- writers
def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _rotation(rx: float, ry: float, rz: float) -> np.ndarray:
    ax, ay, az = np.radians([rx, ry, rz])
    r_x = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    r_y = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    r_z = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    return r_z @ r_y @ r_x


def _on_bed(mesh: trimesh.Trimesh, rot: tuple[float, float, float]) -> trimesh.Trimesh:
    out = mesh.copy()
    m = np.eye(4)
    m[:3, :3] = _rotation(*rot)
    out.apply_transform(m)
    lo, hi = out.bounds
    out.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return out


def _analysis(name: str, mesh: trimesh.Trimesh, rot: tuple[float, float, float]) -> dict:
    placed = _on_bed(mesh, rot)
    vol = float(placed.volume)
    mass = vol / 1000.0 * 1.27 * 0.45
    over, bridge = _overhang_areas(placed)
    findings = []
    if over > 25:
        findings.append({"code": "PRINT.OVERHANG", "severity": "warning",
                         "message": f"Overhang area {over:.0f} mm² beyond 45° needs supports.",
                         "subject": f"part:{name}", "data": {"area_mm2": over},
                         "hint": "Rotate the part or add a 45° chamfer under the ledge.",
                         "source": f"print:{name}"})
    if bridge > 0:
        findings.append({"code": "PRINT.BRIDGE", "severity": "info",
                         "message": f"1 bridge ({bridge:.1f} mm², longest span {PORT_W:.1f} mm) prints "
                                    "without supports.", "subject": f"part:{name}",
                         "data": {"count": 1, "area_mm2": bridge}, "hint": "", "source": f"print:{name}"})
    return {
        "name": name, "printer": "prusa_mk4", "material": "PETG",
        "watertight": bool(placed.is_watertight), "winding_consistent": True, "body_count": 1,
        "volume_mm3": vol, "area_mm2": float(placed.area),
        "size_mm": [float(v) for v in placed.extents], "fits_bed": True,
        "overhang_area_mm2": over, "bridge_count": int(bridge > 0), "bridge_area_mm2": bridge,
        "bed_contact_area_mm2": 0.0, "min_wall_mm": 2.0,
        "needs_supports": over > 25,
        "estimate": {"mass_g": mass, "filament_m": mass / 2.98, "cost": mass / 1000 * 85.0,
                     "time_h": mass / 11.0 + 0.1, "shell_volume_mm3": vol * 0.35,
                     "infill_volume_mm3": vol * 0.65},
        "report": {"title": f"print:{name}", "ok": True,
                   "counts": {sev: sum(f["severity"] == sev for f in findings)
                              for sev in ("error", "warning", "info")},
                   "findings": findings},
    }


def _report() -> dict:
    def f(code: str, sev: str, msg: str, subject: str, source: str, hint: str = "", **data: Any) -> dict:
        return {"code": code, "severity": sev, "message": msg, "subject": subject, "data": data,
                "hint": hint, "source": source}

    findings = [
        f("ERC.I2C_PULLUPS", "info", "I2C1 relies on the Pi's on-board 1.8 kΩ pull-ups.", "net:SDA1", "erc"),
        f("ERC.FLOATING_INPUT", "warning", "GPIO22 is an input with no pull resistor configured.",
          "pin:U1.GPIO22", "erc", "Enable the internal pull-up: configure(pi, pulls={'GPIO22': 'up'}).",
          pin=22),
        f("POWER.RAIL_MARGIN", "warning", "5V rail worst case 2.45 A is 82 % of the 3.00 A PSU.",
          "rail:5V", "power", "Use a 5 A supply or limit servo stall current.", max_ma=2450,
          available_ma=3000),
        f("PRINT.OVERHANG", "warning", "Overhang area 302 mm² beyond 45° needs supports.",
          "part:bracket", "print:bracket", "Rotate the part or add a 45° chamfer under the ledge.",
          area_mm2=301.6),
        f("PRINT.ESTIMATE", "info", "lid: 23.4 g PETG, 2 h 10 min, 1.99 PLN.", "part:lid", "print:lid"),
        # subject/data as written by mech.assembly.Assembly.check_interference
        f("ASM.INTERFERENCE", "error", "bracket and base overlap by 12.5 mm³.",
          "node:bracket/base", "assembly", "Move or resize one of the parts, or add the pair to ignore= if intended.",
          a="bracket", b="base", volume_mm3=12.5),
        f("THERMAL.OK", "info", "Enclosure air ΔT 7.9 °C at 4.6 W with vents; Pi SoC below throttle.",
          "", "thermal", delta_t_c=7.9),
        f("SPICE.OK", "info", "LED current 3.9 mA (GPIO limit 16 mA).", "", "spice:status_led",
          i_ma=3.9),
        f("TWIN.SCENARIO_OK", "info", "Scenario 'button_lights_led' passed (2 steps).", "",
          "twin:button_lights_led"),
        f("PROJECT.SLICER_MISSING", "info", "No slicer CLI found; print times are estimates.", "",
          "project", "Install PrusaSlicer to get sliced times."),
    ]
    counts = {"error": 0, "warning": 0, "info": 0}
    for x in findings:
        counts[x["severity"]] += 1
    return {"title": "build", "ok": counts["error"] == 0, "counts": counts, "findings": findings}


WIRING_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="300" viewBox="0 0 640 300">
<rect width="640" height="300" fill="#ffffff"/>
<rect x="20" y="40" width="140" height="220" rx="6" fill="#166534"/>
<text x="90" y="30" font-family="sans-serif" font-size="14" text-anchor="middle">U1 Raspberry Pi 4B</text>
<g font-family="sans-serif" font-size="11">
<rect x="420" y="40" width="180" height="50" rx="6" fill="#f1f5f9" stroke="#334155"/>
<text x="510" y="70" text-anchor="middle">D1 LED + R1 330 Ω</text>
<rect x="420" y="120" width="180" height="50" rx="6" fill="#f1f5f9" stroke="#334155"/>
<text x="510" y="150" text-anchor="middle">SW1 pushbutton</text>
<rect x="420" y="200" width="180" height="50" rx="6" fill="#f1f5f9" stroke="#334155"/>
<text x="510" y="230" text-anchor="middle">M1 SG90 servo</text>
</g>
<path d="M160 70 L420 65" stroke="#22c55e" stroke-width="3" fill="none"/>
<path d="M160 145 L420 145" stroke="#eab308" stroke-width="3" fill="none"/>
<path d="M160 225 L420 225" stroke="#f97316" stroke-width="3" fill="none"/>
<path d="M160 240 L420 240" stroke="#ef4444" stroke-width="3" fill="none"/>
<path d="M160 250 L420 248" stroke="#111827" stroke-width="3" fill="none"/>
</svg>
"""

WIRING_ROWS = [
    {"net": "GPIO17", "a_ref": "U1", "a_pin": "GPIO17", "a_phys": "11", "b_ref": "R1", "b_pin": "1",
     "b_phys": "1", "color": "green", "signal": "LED drive"},
    {"net": "GPIO27", "a_ref": "U1", "a_pin": "GPIO27", "a_phys": "13", "b_ref": "SW1", "b_pin": "1",
     "b_phys": "1", "color": "yellow", "signal": "Button (active low)"},
    {"net": "GPIO18", "a_ref": "U1", "a_pin": "GPIO18", "a_phys": "12", "b_ref": "M1", "b_pin": "SIG",
     "b_phys": "3", "color": "orange", "signal": "Servo PWM"},
    {"net": "5V", "a_ref": "U1", "a_pin": "5V", "a_phys": "2", "b_ref": "M1", "b_pin": "VCC",
     "b_phys": "2", "color": "red", "signal": "Power"},
    {"net": "GND", "a_ref": "U1", "a_pin": "GND", "a_phys": "6", "b_ref": "M1", "b_pin": "GND",
     "b_phys": "1", "color": "black", "signal": "Ground"},
    {"net": "GND", "a_ref": "U1", "a_pin": "GND", "a_phys": "9", "b_ref": "SW1", "b_pin": "2",
     "b_phys": "2", "color": "black", "signal": "Ground"},
    {"net": "GND", "a_ref": "U1", "a_pin": "GND", "a_phys": "14", "b_ref": "D1", "b_pin": "K",
     "b_phys": "2", "color": "black", "signal": "Ground"},
]

BOM_CSV = """Qty,Refs,Key,Name,Value,Notes
1,U1,rpi4b,Raspberry Pi 4 Model B,4 GB,
1,PS1,psu_usbc_5v3a,USB-C power supply 5.1 V 3 A,,
1,D1,led,LED 5 mm,red,
1,R1,resistor,"Resistor (axial, 0.25 W)",330 Ω,
1,SW1,pushbutton,Tactile push button 12 mm,,
1,M1,sg90_servo,SG90 micro servo,,needs 5 V
"""  # header and quoting as written by piforge.elec.bom.bom_csv

PINOUT_MD = """# Pinout — fixture_gauge

| Phys | BCM | Function | Connected to |
|---:|---|---|---|
| 11 | GPIO17 | output | R1 → D1 (LED) |
| 12 | GPIO18 | PWM0 | M1 SIG (servo) |
| 13 | GPIO27 | input, pull-up | SW1 |
| 2 | 5V | power | M1 VCC |
| 6, 9, 14 | GND | ground | M1, SW1, D1 |
"""

CONFIG_TXT = """# PiForge: fixture_gauge
dtparam=audio=off
dtoverlay=pwm,pin=18,func=2
"""

POWER = {
    "psu": "psu_usbc_5v3a",
    "rails": {
        "5V": {"name": "5V", "voltage": 5.0, "available_ma": 3000.0, "typ_ma": 1150.0, "max_ma": 2450.0,
               "loads": [["U1 Raspberry Pi 4B", 600.0, 1200.0], ["M1 SG90 servo", 150.0, 650.0],
                         ["3V3 regulator", 400.0, 600.0]]},
        "3V3": {"name": "3V3", "voltage": 3.3, "available_ma": 500.0, "typ_ma": 12.0, "max_ma": 25.0,
                "loads": [["D1 LED", 4.0, 4.0], ["GPIO", 8.0, 21.0]]},
    },
    "report": {"title": "power", "ok": True, "counts": {"error": 0, "warning": 1, "info": 0},
               "findings": []},
}


def _sim_result() -> dict:
    """A stored bench result in ``BenchResult.to_dict()`` form (labels carry units)."""
    t = np.linspace(0.0, 5e-3, 251)
    tau = 330 * 1e-6
    vin = np.where(t > 0.5e-3, 3.3, 0.0)
    vout = np.where(t > 0.5e-3, 3.3 * (1 - np.exp(-(t - 0.5e-3) / tau)), 0.0)
    measures, analytic = {"tau": 3.31e-4, "f_3db": 482.0}, {"tau": 3.3e-4, "f_3db": 482.3}
    units = {"tau": "s", "f_3db": "Hz"}
    return {"key": "rc_filter", "title": "RC low-pass filter",
            "params": {"r": 330.0, "c": 1e-6, "analysis": "step", "v_step": 3.3},
            "measures": measures, "analytic": analytic, "units": units,
            "table": [{"name": k, "value": v, "unit": units[k], "analytic": analytic[k],
                       "error_pct": (v - analytic[k]) / analytic[k] * 100} for k, v in measures.items()],
            "x_label": "time [s]", "x_scale": "linear",
            "traces": {"time [s]": t.tolist(), "V(in) [V]": vin.tolist(), "V(out) [V]": vout.tolist()},
            "report": {"title": "spice:rc_debounce", "ok": True,
                       "counts": {"error": 0, "warning": 0, "info": 1},
                       "findings": [{"code": "SPICE.SUMMARY", "severity": "info",
                                     "message": "τ = 0.331 ms (analytic 0.330 ms).", "subject": "",
                                     "data": {}, "hint": "", "source": "spice:rc_debounce"}]}}


# ---------------------------------------------------------------------------------- wiring harness
def header_pin(n: int) -> list[float]:
    """World position (mm) of the tip of pin ``n`` of the fixture Pi's 40-pin header."""
    col, row = (n - 1) // 2, (n - 1) % 2
    return [round(-49.73 + col * 2.54, 3), 23.23 if row == 0 else 25.77, 15.0]


# id, ref, pin, label, node, pos, dir
CONNECTORS: list[tuple[str, str, str, str, str, list[float], list[float]]] = [
    *[(f"U1.{n}", "U1", str(n), lab, "pi", header_pin(n), [0.0, 0.0, 1.0]) for n, lab in (
        (1, "3V3"), (2, "5V"), (6, "GND"), (9, "GND"), (11, "GPIO17"), (12, "GPIO18 / PWM0"), (13, "GPIO27"),
        (14, "GND"))],
    ("D1.A", "D1", "A", "anode", "led", [38.8, 22.0, 31.0], [0.0, 0.0, -1.0]),
    ("D1.K", "D1", "K", "cathode", "led", [41.2, 22.0, 31.0], [0.0, 0.0, -1.0]),
    ("SW1.1", "SW1", "1", "", "button", [-42.5, 22.0, 31.0], [0.0, 0.0, -1.0]),
    ("SW1.2", "SW1", "2", "", "button", [-37.5, 22.0, 31.0], [0.0, 0.0, -1.0]),
    ("M1.SIG", "M1", "SIG", "signal", "bracket", [44.0, -14.0, 16.0], [0.0, -1.0, 0.0]),
    ("M1.VCC", "M1", "VCC", "V+", "bracket", [46.5, -14.0, 16.0], [0.0, -1.0, 0.0]),
    ("M1.GND", "M1", "GND", "", "bracket", [49.0, -14.0, 16.0], [0.0, -1.0, 0.0]),
]
# id, from connector, to connector, net, signal, colour name, hex, AWG, cable, routing height z (mm)
WIRES: list[tuple[str, str, str, str, str, str, str, int, str | None, float]] = [
    ("W1", "U1.11", "D1.A", "GPIO17", "LED drive", "green", "#16a34a", 24, None, 27.0),
    ("W2", "U1.13", "SW1.1", "GPIO27", "Button", "yellow", "#eab308", 24, None, 25.0),
    ("W3", "U1.14", "D1.K", "GND", "Ground", "black", "#1f2937", 24, None, 23.0),
    ("W4", "U1.9", "SW1.2", "GND", "Ground", "black", "#1f2937", 24, None, 21.0),
    ("W5", "U1.12", "M1.SIG", "GPIO18", "Servo PWM", "orange", "#f97316", 26, "SERVO lead", 19.5),
    ("W6", "U1.2", "M1.VCC", "5V", "Servo power", "red", "#dc2626", 26, "SERVO lead", 18.5),
    ("W7", "U1.6", "M1.GND", "GND", "Ground", "brown", "#7c2d12", 26, "SERVO lead", 17.5),
]
WIRE_R = 0.8  # insulation radius (mm)


def wire_node_id(wire_id: str) -> str:
    """Scene node id of a fixture wire (≠ the wire id, so the GUI must map between them)."""
    return f"wire_{wire_id}"


def _tube(points: list[np.ndarray], r: float) -> trimesh.Trimesh:
    """A round tube along a polyline (cylinders + spheres at the bends), world frame."""
    parts = [trimesh.creation.icosphere(subdivisions=1, radius=r).apply_translation(p) for p in points]
    for a, b in zip(points[:-1], points[1:], strict=True):
        if np.linalg.norm(b - a) > 1e-6:
            parts.append(trimesh.creation.cylinder(radius=r, segment=[a, b], sections=12))
    return trimesh.util.concatenate(parts)


def _write_harness(b: Path) -> tuple[list[dict], list[dict]]:
    """Wire GLBs + cut list; returns (wire scene nodes, connectors)."""
    conns = {c[0]: c for c in CONNECTORS}
    nodes, rows = [], []
    for wid, a, z, net, signal, cname, hexcol, awg, cable, height in WIRES:
        ca, cb = conns[a], conns[z]
        pa, pb = np.array(ca[5]), np.array(cb[5])
        pts = [pa, np.array([pa[0], pa[1], height]), np.array([pb[0], pb[1], height]), pb]
        if cb[6][1] < 0:  # servo pins face −Y: come in from the front
            pts[2:] = [np.array([pb[0], pb[1] - 6, height]), np.array([pb[0], pb[1] - 6, pb[2]]), pb]
        length = float(sum(np.linalg.norm(q - p) for p, q in zip(pts[:-1], pts[1:], strict=True))) + 30.0
        write_glb([("wire", _tube(pts, WIRE_R), hexcol)], b / "meshes" / f"{wid}.glb")
        end = lambda c: {"ref": c[1], "pin": c[2], "label": c[3], "connector": c[0]}  # noqa: E731
        nodes.append({"id": wire_node_id(wid), "name": f"Wire {wid}", "kind": "wire", "mesh": f"meshes/{wid}.glb",
                      "color": hexcol, "matrix": _colmajor(np.eye(4)), "world_matrix": _colmajor(np.eye(4)),
                      "parent": None, "material": None, "joint": None, "emissive_from": None,
                      "display_from": None, "explode": None,
                      "wire": {"id": wid, "from": end(ca), "to": end(cb), "net": net, "signal": signal,
                               "color_name": cname, "gauge_awg": awg, "length_mm": round(length, 1),
                               "cable": cable}})
        rows.append([wid, f"{ca[1]}.{ca[2]}", f"{cb[1]}.{cb[2]}", net, cname, f"{awg} AWG", f"{length:.1f}"])
    head = ["wire id", "from", "to", "net", "colour", "gauge", "length_mm"]
    (b / "elec" / "cut_list.csv").write_text(
        "\n".join(",".join(r) for r in [head, *rows]) + "\n", encoding="utf-8")
    (b / "elec" / "cut_list.md").write_text(
        "| " + " | ".join(head) + " |\n|" + "---|" * len(head) + "\n"
        + "".join("| " + " | ".join(r) + " |\n" for r in rows), encoding="utf-8")
    connectors = [{"id": c[0], "ref": c[1], "pin": c[2], "label": c[3], "node": c[4], "pos": c[5], "dir": c[6]}
                  for c in CONNECTORS]
    return nodes, connectors


# ---------------------------------------------------------------------------------- entry point
SF_PITCH, SF_Y, SF_Z = 48.0, -70.0, 75.0  # split-flap row: window pitch, front plane, centre height (mm)


def sf_window_id(i: int) -> str:
    return f"sf_window_{i}"


def sf_spool_id(i: int) -> str:
    return f"sf_spool_{i}"


def _splitflap_nodes(b: Path, n: int, device: Callable[[int], str]) -> list[dict]:
    """``n`` split-flap windows (``display_from``) + spools (joint driven by ``<device>.angle``)."""
    write_glb([("window", _box(40, 1.0, 60, (0, 0, 0)), "#16181c")], b / "meshes" / "sf_window.glb")
    drum = _cyl(16, 30, z0=-15, sections=40)
    drum.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    marker = _box(30, 4, 6, (0, 0, 16))  # a rib on the rim: the rotation is easy to see
    write_glb([("drum", drum, "#d1d5db"), ("rib", marker, "#ef4444")], b / "meshes" / "sf_spool.glb")
    nodes = []
    for i in range(n):
        x = (i - (n - 1) / 2) * SF_PITCH
        win, spool = _matrix((x, SF_Y, SF_Z)), _matrix((x, SF_Y + 25, SF_Z - 75))
        nodes.append({"id": sf_window_id(i), "name": f"Flap window {i}", "kind": "reference",
                      "mesh": "meshes/sf_window.glb", "color": "#16181c", "matrix": _colmajor(win),
                      "world_matrix": _colmajor(win), "parent": None, "material": None, "joint": None,
                      "emissive_from": None, "explode": [0.0, -20.0, 0.0],
                      "display_from": {"device": device(i), "kind": "splitflap"}})
        nodes.append({"id": sf_spool_id(i), "name": f"Spool {i}", "kind": "printed", "mesh": "meshes/sf_spool.glb",
                      "color": "#d1d5db", "matrix": _colmajor(spool), "world_matrix": _colmajor(spool),
                      "parent": None, "material": "PETG", "emissive_from": None, "display_from": None,
                      "explode": None,
                      "joint": {"type": "revolute", "axis": [1.0, 0.0, 0.0], "origin": [0.0, 0.0, 0.0],
                                "min": 0.0, "max": 360.0, "value": 0.0,
                                "driven_by": {"device": device(i), "prop": "angle", "scale": 1.0, "offset": 0.0}}})
    return nodes


def make_fixture_project(root: Path, *, splitflaps: int = 0,
                         flap_device: Callable[[int], str] = lambda i: f"FLAP{i + 1}", wires: bool = False) -> Path:
    """Create ``root`` (project dir) with ``firmware/main.py`` and a complete ``build/`` dir.

    ``splitflaps=n`` adds a row of n split-flap windows (``display_from`` → ``flap_device(i)``) and
    their spools (revolute joints driven by ``flap_device(i).angle``) to the scene. ``wires=True``
    adds the wiring harness: ``WIRES`` as ``kind: "wire"`` tube nodes, ``CONNECTORS`` (top-level
    ``connectors``) and ``elec/cut_list.csv|md``.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "firmware").mkdir(exist_ok=True)
    (root / "firmware" / "main.py").write_text(FIRMWARE, encoding="utf-8")
    b = root / "build"
    for sub in ("meshes", "parts", "elec", "sim", "twin", "renders"):
        (b / sub).mkdir(parents=True, exist_ok=True)

    leaves = make_leaves()
    for stem, lv in leaves.items():
        write_glb(lv, b / "meshes" / f"{stem}.glb")
    mesh_of = {nid: f"meshes/{nid}.glb" for nid in NODE_IDS}
    mesh_of["screw1"] = mesh_of["screw2"] = "meshes/screw_m2_5.glb"

    spec = {  # id: (name, kind, colour, material, parent, translation, rz°, explode offset mm)
        "base": ("Enclosure base", "printed", "#3b82f6", "PETG", None, (0, 0, 0), 0, (0, 0, -15)),
        "lid": ("Lid", "printed", "#93c5fd", "PETG", None, (0, 0, 32), 0, (0, 0, 45)),
        "pi": ("Raspberry Pi 4B", "pcb", "#15803d", "FR4", None, (-58, -28, 5), 0, None),
        "dial": ("Dial", "printed", "#e5e7eb", "PETG", "lid", (0, 0, 3), 0, (0, 0, 14)),
        "needle": ("Needle", "printed", "#ef4444", "PETG", "dial", (0, 0, 2), 0, (0, 0, 8)),
        "led": ("Status LED D1", "reference", "#7f1d1d", None, "lid", (40, 22, 3), 0, (0, 0, 10)),
        "button": ("Button SW1", "reference", "#374151", None, "lid", (-40, 22, 3), 0, (0, 0, 10)),
        "bracket": ("Servo bracket", "printed", "#f59e0b", "PETG", None, (44, 0, 2), 90, (45, 0, 8)),
        "screw1": ("Screw M2.5x8", "fastener", "#9ca3af", "steel", "pi", (3.5, 3.5, 1.4), 0, (0, 0, 25)),
        "screw2": ("Screw M2.5x8", "fastener", "#9ca3af", "steel", "pi", (61.5, 3.5, 1.4), 0, (0, 0, 25)),
    }
    joints = {"needle": {"type": "revolute", "axis": [0.0, 0.0, 1.0], "origin": [0.0, 0.0, 0.0],
                         "min": -90.0, "max": 90.0, "value": NEEDLE_ANGLE,
                         "driven_by": {"device": "SERVO1", "prop": "angle", "scale": 1.0, "offset": 0.0}}}
    local = {nid: _matrix(v[5], v[6]) for nid, v in spec.items()}
    world: dict[str, np.ndarray] = {}

    def world_of(nid: str) -> np.ndarray:  # world = parent_world · matrix · J(value)
        if nid not in world:
            parent = spec[nid][4]
            m = (world_of(parent) if parent else np.eye(4)) @ local[nid]
            if nid in joints:
                m = m @ _matrix((0, 0, 0), joints[nid]["value"])  # revolute about +Z through the origin
            world[nid] = m
        return world[nid]

    nodes = []
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for nid in NODE_IDS:
        name, kind, color, material, parent, _t, _rz, explode = spec[nid]
        stem = mesh_of[nid].split("/")[-1][:-4]
        pts = concat(leaves[stem]).vertices
        wpts = (np.c_[pts, np.ones(len(pts))] @ world_of(nid).T)[:, :3]
        lo, hi = np.minimum(lo, wpts.min(axis=0)), np.maximum(hi, wpts.max(axis=0))
        nodes.append({"id": nid, "name": name, "kind": kind, "mesh": mesh_of[nid], "color": color,
                      "matrix": _colmajor(local[nid]), "world_matrix": _colmajor(world_of(nid)),
                      "parent": parent, "material": material, "joint": joints.get(nid),
                      "emissive_from": None, "display_from": None,
                      "explode": list(explode) if explode else None})
    next(n for n in nodes if n["id"] == "led")["emissive_from"] = {
        "device": "D1", "prop": "brightness", "color": "#ff2a1a"}
    if splitflaps:
        nodes += _splitflap_nodes(b, splitflaps, flap_device)
        half = (splitflaps - 1) / 2 * SF_PITCH + 20
        lo = np.minimum(lo, [-half, SF_Y - 1, SF_Z - 75 - 17])
        hi = np.maximum(hi, [half, SF_Y + 42, SF_Z + 30])
    scene: dict[str, Any] = {"name": "fixture_gauge", "units": "mm", "up": "Z", "nodes": nodes,
                             "bounds": [lo.tolist(), hi.tolist()]}
    if wires:
        wire_nodes, scene["connectors"] = _write_harness(b)
        nodes += wire_nodes
    _write_json(b / "scene.json", scene)

    rotations = {"base": (0, 0, 0), "lid": (180, 0, 0), "dial": (0, 0, 0), "needle": (0, 0, 0),
                 "bracket": (0, 0, 0)}
    parts = []
    for name in PRINTED:
        rot = rotations[name]
        mesh = concat(leaves[name])
        _on_bed(mesh, rot).export(b / "parts" / f"{name}.stl")
        parts.append({"name": name, "material": "PETG", "color": spec[name][2], "quantity": 1,
                      "files": {"stl": f"parts/{name}.stl"}, "print_rotation": list(rot),
                      "analysis": _analysis(name, mesh, rot), "node_ids": [name]})
    _write_json(b / "parts" / "index.json", parts)

    elec = b / "elec"
    _write_json(elec / "circuit.json", {"name": "fixture_gauge", "parts": [
        {"ref": "U1", "key": "rpi4b"}, {"ref": "D1", "key": "led"}, {"ref": "R1", "key": "resistor"},
        {"ref": "SW1", "key": "pushbutton"}, {"ref": "M1", "key": "sg90_servo"}], "nets": []})
    (elec / "bom.csv").write_text(BOM_CSV, encoding="utf-8")
    (elec / "bom.md").write_text("| refs | name | qty |\n|---|---|---|\n| U1 | Pi 4B | 1 |\n",
                                 encoding="utf-8")
    _write_json(elec / "wiring.json", WIRING_ROWS)
    (elec / "wiring.md").write_text("| net | from | to |\n|---|---|---|\n| GPIO17 | U1.11 | R1.1 |\n",
                                    encoding="utf-8")
    (elec / "wiring.svg").write_text(WIRING_SVG, encoding="utf-8")
    (elec / "config.txt").write_text(CONFIG_TXT, encoding="utf-8")
    (elec / "netlist.net").write_text('(export (version "E") (components) (nets))\n', encoding="utf-8")
    _write_json(elec / "power.json", POWER)
    (elec / "pinout.md").write_text(PINOUT_MD, encoding="utf-8")

    sim = _sim_result()
    _write_json(b / "sim" / "rc_debounce.json", sim)
    _write_json(b / "sim" / "index.json", [{"label": "rc_debounce", "bench": "rc_filter",
                                            "params": sim["params"], "measures": sim["measures"],
                                            "analytic": sim["analytic"], "ok": True}])
    _write_json(b / "twin" / "config.json", TWIN_CONFIG)
    _write_json(b / "twin" / "scenarios.json", SCENARIOS)
    _write_json(b / "report.json", _report())
    (b / "report.md").write_text("## build — FAIL\n", encoding="utf-8")
    _write_json(b / "manifest.json", {
        "name": "fixture_gauge", "description": "Fixture gauge for server/GUI tests",
        "board": "rpi4b", "printer": "prusa_mk4", "material": "PETG", "firmware": "firmware/main.py",
        "project_dir": str(root), "built_at": "2026-10-04T12:00:00+02:00", "piforge_version": "0.1.0",
        "files": {"parts": [f"parts/{p}.stl" for p in PRINTED], "renders": [],
                  "elec": sorted(p.name for p in elec.iterdir()), "sim": ["sim/rc_debounce.json"],
                  "twin": ["twin/config.json", "twin/scenarios.json"]},
        "durations_s": {"load": 0.2, "elec": 0.1, "parts": 1.5, "scene": 0.4, "render": 0.0,
                        "spice": 0.8, "twin": 0.0}})
    return root


# ---------------------------------------------------------------------------------- live server
def free_port() -> int:
    """An unused TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def live_server(app: Any, *, timeout: float = 20.0) -> Iterator[str]:
    """Run ``app`` with uvicorn in a background thread; yields the base URL; stops on exit."""
    import uvicorn

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                            ws_ping_interval=None)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn test server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
