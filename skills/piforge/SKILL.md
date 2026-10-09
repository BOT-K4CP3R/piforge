---
name: piforge
description: Design, simulate, validate and export Raspberry Pi devices with PiForge — electronics (ERC, power budget, wiring, BOM, config.txt, KiCad netlist), 3D-printed mechanics (enclosures with port cutouts, split-flap displays, gears, mechanisms, 3D wire harnesses), SPICE benches, thermal checks and a digital twin that runs the real firmware, plus a web GUI with a 3D viewer. Use when the user wants to build anything Raspberry Pi + electronics + 3D printing, or asks to design, check, simulate, render, export STL/3MF or show a device in the GUI.
---

# PiForge — Raspberry Pi device design

PiForge is a Python 3.12 toolkit. One `projects/<name>/project.py` describes a whole device;
`piforge build` turns it into printable parts, renders, electronics documents, simulations and one
validation report; `piforge serve` shows it in a browser GUI (3D view, checks, SPICE with inputs,
interactive digital twin).

## Setup (once per machine)

The plugin root (`${CLAUDE_PLUGIN_ROOT}`, or a clone of this repository) contains the package.

```bash
uv venv --python 3.12 "$HOME/.local/share/piforge/venv"
uv pip install --python "$HOME/.local/share/piforge/venv/bin/python" -e "${CLAUDE_PLUGIN_ROOT}[dev]"
brew install ngspice                                   # SPICE (optional; stages are skipped without it)
"$HOME/.local/share/piforge/venv/bin/playwright" install chromium   # only for GUI tests
```

Then use `"$HOME/.local/share/piforge/venv/bin/piforge"` (or `.venv/bin/piforge` inside the repo).
Paths may contain spaces — always quote them.

## Workflow

1. `piforge new NAME --board rpi5|rpi4b|rpi3bp|rpizero2w --dir <projects dir>` — template project.
2. Edit `project.py` (`build(p)`: circuit, printed parts, assembly, firmware, scenarios, SPICE,
   thermal, harness) and `firmware/main.py` (ordinary Pi Python: gpiozero, RPi.GPIO, smbus2, spidev,
   Adafruit drivers — runs unmodified in the twin and on the Pi).
3. `piforge check DIR` (fast, no files) → fix every ERROR.
4. `piforge build DIR --scenarios` → `DIR/build/`.
5. **Look at the result**: Read `build/renders/*.png`, `build/elec/wiring.png`, `build/sim/*.png`,
   then `build/report.md`. Never trust a green report without looking at the renders.
6. `piforge serve DIR --port 8765 --no-browser` in the background and open it for the user
   (Twin tab: Start, then drive inputs; 3D view follows the twin).
7. Printable files: `build/parts/*.stl|3mf` (already in print orientation), `parts/index.json`.

Always check names with `piforge info boards|modules|parts|printers|materials|benches|devices`
instead of guessing; unknown names raise errors that list close matches.

## Reference

- Full API guide (Project, elec, mech, spice, twin, analysis, findings, build layout, tests,
  limitations): [reference.md](reference.md).
- Example projects in the repository: `projects/_template` (minimal), `projects/demo_gauge`
  (every subsystem), `projects/money_counter` (split-flap money counter with a closed housing,
  WebSocket feed, 3D wire harness and 7 twin scenarios).

## Rules of thumb

- Units mm / V / A / Ω / °C; Z up; print bed at z = 0.
- Design problems are `Finding`s in the report (codes like `ERC.LEVEL_MISMATCH`, `PRINT.OVERHANG`);
  usage errors are exceptions.
- Builds must end with 0 ERRORs; explain every WARNING to the user.
- The CAD kernel is slow (check ≈ 10 s, build 15–200 s): iterate with `check` and
  `build --no-render --no-spice`, run test areas one at a time.
