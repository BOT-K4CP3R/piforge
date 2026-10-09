"""money_counter — a physical split-flap money counter ``000000,00`` on a Raspberry Pi Zero 2 W.

Eight 50 mm split-flap digit modules (20 flaps = 0–9 twice, 28BYJ-48 stepper, A3144 Hall homing)
and a fixed comma after the sixth digit. The Pi receives the amount over WebSocket (client or
server mode, ``firmware/config.toml``) and **counts up** to it visibly: high digits follow the
count, low digits scroll at full speed and everything lands exactly on the amount (log
``SHOW 001234,56``). Moves are ramped 450 → 850 half-steps/s; every magnet pass checks the step count
(``RESYNC``/re-home/``VERIFY``), and a power-up self-test turn proves each module's speed. The twin's
28BYJ-48s have a stall model, so a too-fast firmware loses steps there just as on the real motors.

::

    piforge check projects/money_counter
    piforge build projects/money_counter --scenarios
    piforge serve projects/money_counter        # Twin → Start → type an amount in "Amount"

Files: ``mc_circuit.py`` (electronics), ``mc_mech.py`` (modules in one closed housing, electronics),
``mc_harness.py`` (every wire routed in 3D, cut list), ``mc_twin.py`` (twin devices + scenarios),
``firmware/`` (Python firmware + unit tests),
``artwork/`` (digit stickers), ``tools/export_artwork.py``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from mc_circuit import BOARD, N, build_circuit, module_bits
from mc_harness import add_harness
from mc_mech import add_mechanics
from mc_twin import add_scenarios, add_twin_devices, sf

from piforge.core.report import Report
from piforge.project import Project

PRINTER = "generic"     # 220 × 220 bed: the housing is printed in segments (front 4, floor/top/back 4)
MATERIAL = "PLA"        # indoor display; PETG works too (same geometry)
HERE = Path(__file__).resolve().parent


def firmware_config() -> dict:
    """``firmware/config.toml`` — the one source for Hall pins and per-module offsets."""
    return tomllib.loads((HERE / "firmware" / "config.toml").read_text(encoding="utf-8"))


def build(p: Project) -> None:
    p.meta(description="Split-flap money counter 000000,00: 8 × 28BYJ-48 flap modules + comma, Pi Zero 2 W, "
                       "74HCT595 chain on SPI0, ULN2003 boards, A3144 Hall homing, amount over WebSocket "
                       "with a visible count-up.",
           board=BOARD, printer=PRINTER, material=MATERIAL)
    motion = firmware_config()["motion"]
    hall_pins = tuple(motion["hall_pins"])
    build_circuit(p, hall_pins)

    mech = add_mechanics(p, PRINTER, MATERIAL, [sf(i) for i in range(N)])
    m = mech["module"]
    # interference over the whole row (motor model's round shaft sits in the spool's D-bore);
    # the spool sweep is checked once on a single module (check:module) instead of 8 × identical
    p.assembly_check(ignore=mech["ignore"], sweep_joints=False)
    p.add_check(lambda: m.checks(printability=False), name="module")      # SPLITFLAP.*, ASM.* of one module
    h = mech["housing"]
    # closed housing: bed fit, flap swing vs the front panel, front view of every window = flap faces only
    p.add_check(lambda: h.checks(asm=p.assembly, interference=False), name="housing")
    p.add_check(lambda: consistency(p, motion), name="consistency")
    # every wire in 3D: Pi header → perfboard → 8 driver ribbons, motor leads, Hall bus (mc_harness.py);
    # build → elec/cut_list.csv|md, WIRE.* findings (source "harness"), wire nodes in scene.json
    add_harness(p, h)

    p.firmware("firmware/main.py")
    add_twin_devices(p, hall_pins, tuple(motion["offsets"]), int(motion["steps_per_rev"]), int(motion["flaps"]))
    add_scenarios(p)

    # -- SPICE -------------------------------------------------------------------------------------------
    # One 28BYJ-48 coil switched by one ULN2003 channel. Bench approximation: the bjt_switch bench has
    # a single NPN (2N2222) instead of the ULN2003's Darlington (2.7 kΩ input resistor → r_base) and a
    # relay coil (70 Ω, 150 mH) instead of the motor phase (50 Ω, ≈ 30 mH) — more stored energy, so a
    # conservative flyback test; the 1N4148-class diode stands for the ULN2003's clamp diode to COM.
    p.spice("bjt_switch", label="coil_flyback", transistor="q2n2222", r_base=2700.0, v_supply=5.0,
            load="relay_coil_5v", flyback=True, diode="d1n4148")
    # 5 V 5 A adapter → 1.5 m barrel cable (≈ 0.05 Ω) → perfboard rail with 470 µF; Pi Zero 2 W + Halls +
    # 595s ≈ 0.4 A, then all 8 motors start a move together (2 coils × 100 mA each = 1.6 A more).
    p.spice("power_path", label="eight_motors_start", v_psu=5.0, r_cable=0.05, r_source=0.02, c_in=470e-6,
            i_idle=0.4, i_load=2.0, t_hold=0.05)


def consistency(p: Project, motion: dict) -> Report:
    """config.toml ↔ circuit ↔ twin: Hall pins, coil bits, one splitflap per stepper."""
    rep = Report(title="consistency")
    c = p.circuit
    for i in range(N):
        want = motion["hall_pins"][i]
        got = c.bcm_of(c.part(f"H{i + 1}")["OUT"])
        if got != want:
            rep.add("MC.HALL_PIN", "error", f"H{i + 1}.OUT is on GPIO{got}, config.toml hall_pins[{i}] says "
                    f"GPIO{want}", subject=f"H{i + 1}")
    cfg = p.twin_config()
    for i in range(N):
        st = cfg.device(f"M{i + 1}")
        bits = (st.params.get("coil_source") or {}).get("bits")
        if bits != module_bits(i):
            rep.add("MC.COIL_BITS", "error", f"M{i + 1} coils are chain bits {bits}, the firmware drives "
                    f"{module_bits(i)}", subject=f"M{i + 1}")
        dev = cfg.device(sf(i))
        if dev.params.get("offset_steps") != motion["offsets"][i]:
            rep.add("MC.OFFSET", "error", f"{sf(i)}.offset_steps differs from config.toml offsets[{i}]")
    if rep.ok and not rep.findings:
        rep.add("MC.CONSISTENT", "info", f"{N} modules: Hall pins, coil bits and offsets agree between "
                "firmware/config.toml, the circuit and the twin")
    return rep
