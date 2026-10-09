"""Mechanics of the money counter (imported by ``project.py``): 8 split-flap modules in one closed housing.

World frame (mm, Z up, the table at z = −5 = the housing's floor bottom): the display faces −Y, the
row is centred on x = 0. Modules come from :class:`piforge.mech.SplitFlapModule` (50 mm digits, 20
flaps, 28BYJ-48 on the right frame, A3144 in the right frame, magnet in the spool); the housing from
:class:`piforge.mech.SplitFlapHousing` — a matte-black box whose front windows show only the flap
faces, the comma inlaid in the front panel between digit 6 and 7, the modules screwed to the floor,
and the electronics compartment behind them: one ULN2003 board per module on floor bosses, the Pi
Zero 2 W and the 74HCT595 perfboard hanging under the top, the DC jack in the removable back cover.

Spool joints are driven by the twin devices ``SF1…SF8`` (``angle``: 0 = digit 0, +18° per flap); a
black ``face_{i}`` plate in every window carries ``display_from`` = the same device, so the GUI paints
the live flipping digits on the closed housing.
"""

from __future__ import annotations

from dataclasses import replace

from piforge.mech import HousingElectronics, SplitFlapHousing, SplitFlapModule, SplitFlapSpec
from piforge.mech.splitflap_housing import DARK

N = 8
COMMA_AFTER = 6                    # digits before the comma: 000000,00
# Module pitch 76 → 75 mm: the cap flange 2.4 → 2.0 mm (the flap pins still reach 1.8 mm into it)
# and its running gap to the left frame 1.0 → 0.6 mm. The motor body beside the spool sets the rest.
MODULE_OVERRIDES = dict(cap_flange=2.0, gap_cap=0.6)


def module(printer: str, material: str) -> SplitFlapModule:
    return SplitFlapModule(SplitFlapSpec(printer=printer, material=material, spool_color=DARK, frame_color=DARK,
                                         **MODULE_OVERRIDES))


def housing(printer: str, material: str) -> SplitFlapHousing:
    return SplitFlapHousing(module(printer, material), n_digits=N, comma_after=COMMA_AFTER,
                            electronics=HousingElectronics(board="rpizero2w", perfboard="perfboard_50x70",
                                                           driver="uln2003_board", dc_jack="dc_jack_panel_55x21",
                                                           shift_registers=4))


def add_mechanics(p, printer: str, material: str, devices: list[str]) -> dict:
    """Register printed parts, build the assembly; returns layout numbers for checks/README."""
    h = housing(printer, material)
    m = h.module
    spool, cap, flap, fl, fr = m.parts
    per_module = [replace(spool, quantity=N), replace(cap, quantity=N), replace(flap, quantity=20 * N),
                  replace(fl, quantity=N), replace(fr, quantity=N)]
    p.add_printed(*per_module, *h.parts)

    asm = p.assembly
    # housing + modules (spool joints driven by SF1…SF8) + electronics
    h.assembly(asm=asm, devices=devices, faces=False)
    # live display faces for the GUI: a thin black plate filling each window, just behind the window plane
    for i in range(N):
        asm.add(h.face_part, h.face_location(i), id=f"face_{i}",
                display_from={"device": devices[i], "kind": "splitflap"})
    ignore = [(f"m{i}_motor", f"m{i}_spool") for i in range(N)]   # module model's shaft has no D-flats
    return {"module": m, "housing": h, "ignore": ignore}
