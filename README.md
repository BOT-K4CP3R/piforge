# PiForge

> A Claude Code plugin and skill for designing Raspberry Pi devices: electronics, 3D-printed mechanics and firmware, with simulation and a web GUI.

PiForge is a code-first Python toolkit for designing devices built around a Raspberry Pi: electronics,
3D-printed mechanics (enclosures, mounts, mechanisms) and firmware. You describe the whole project in
code, in a single file (`projects/<name>/project.py`), and PiForge:

- checks the electronics (ERC: 3.3 V / 5 V levels, GPIO currents, LED series resistors, flyback
  diodes, I2C addresses, power budget),
- generates 3D models (an enclosure with port cutouts, display openings, buttons, LEDs, ventilation
  and screws) and checks that they can be printed (overhangs, bridges, thin walls, fit on the printer
  bed, filament mass, time and cost),
- simulates analog circuits in SPICE (ngspice) and compares the results with textbook values,
- runs your real firmware code (gpiozero, RPi.GPIO, smbus2, Adafruit drivers, ...) on a virtual
  Raspberry Pi (a digital twin) with virtual buttons, sensors, servos and displays,
- calculates the temperature inside the enclosure (convection, ventilation, fan),
- exports print-ready files (STL, 3MF, STEP), a bill of materials (BOM), a connection table, a wiring
  diagram, `config.txt` entries and a KiCad netlist,
- shows all of it in the browser (GUI: 3D view, checks, electronics, SPICE, digital twin).

Projects are designed together with Claude: Claude edits `project.py`, builds, looks at the rendered
images and fixes problems, while you review the result in the GUI and print it.

## Install as a Claude Code plugin

```text
/plugin marketplace add BOT-K4CP3R/piforge
/plugin install piforge@piforge
```

After that, the `piforge` skill activates on its own when you ask Claude to design a Raspberry Pi
device (environment setup instructions are in `skills/piforge/SKILL.md`).

## Installation

Requirements: macOS, Python 3.12, [uv](https://docs.astral.sh/uv/), Homebrew.

```sh
cd "<path to the repository>"
uv venv --python 3.12                       # creates .venv
uv pip install -e ".[dev]"                  # PiForge + drivers for twin tests + pytest + Playwright
brew install ngspice                        # SPICE simulator (SPICE simulations are skipped without it)
.venv/bin/playwright install chromium       # only for GUI tests (pytest -m gui)
```

Notes:

- A repository path inside iCloud contains spaces and `~`, so always quote it.
- In this repository `.venv` is a symlink to `~/.local/share/piforge/venv` (outside iCloud). Do not
  create a second environment.
- The commands below assume `.venv/bin/piforge` (or activate the environment with
  `source .venv/bin/activate`).
- Optional: PrusaSlicer / OrcaSlicer / Bambu Studio (only for `piforge print --slice`, which computes
  G-code), and Blender.

## Quick start

```sh
.venv/bin/piforge new my_project --board rpi4b      # new project in projects/my_project
                                                    # (rpi5, rpi4b, rpi3bp or rpizero2w)
.venv/bin/piforge check projects/my_project         # fast checks (~10 s), writes nothing
.venv/bin/piforge build projects/my_project --scenarios    # builds everything into projects/my_project/build/
.venv/bin/piforge serve projects/my_project         # GUI in the browser: http://127.0.0.1:8765
```

The template (`piforge new`) is a Raspberry Pi on a printed plate with a button that toggles an LED.
You edit `project.py` (circuit, parts, assembly, test scenarios) and `firmware/main.py` (code that
runs unmodified on a real Pi and in the twin), then run `check` and `build` again. In the GUI, the
Rebuild button rebuilds the project after you change `project.py`.

Other commands:

| Command | What it does |
|---|---|
| `piforge twin test projects/X [-s scenario]` | runs scenarios (inputs over time + expectations), PASS/FAIL |
| `piforge twin run projects/X --duration 10` | runs the firmware on the virtual Pi and shows its log |
| `piforge spice led_driver -p r_series=330 --plot led.png` | a single SPICE simulation with a plot |
| `piforge print projects/X [--part P] [--slice]` | print table: mass, time, cost, orientation, overhangs |
| `piforge render file.stl` / `piforge render projects/X --sheet` | PNG render (4 views with a scale) |
| `piforge info boards\|modules\|parts\|printers\|materials\|benches\|devices` | what PiForge knows about |

`check` and `build` exit with code 1 when the project has an error (ERROR). Warnings (WARNING) are
worth reading but do not block the build.

## What the GUI shows (`piforge serve`)

On the left is the part tree (visibility, color, isolate); in the middle is the 3D view with tools:
fit view, iso / front / top / right views, section, distance measurement, overhang map of the
selected printed part, print-bed view, X-ray mode, grid and exploded view. The tabs at the bottom:

- **Checks** — all findings from the build (errors, warnings, info) with a hint on how to fix them;
  the counters are also shown in the header.
- **Electronics** — wiring diagram and connection table, BOM, pinout, `config.txt` entries (with a
  copy button), power budget (typical and worst-case current on the rails), ERC.
- **SPICE** — pick a bench (e.g. LED, MOSFET with a flyback diode, I2C rise time, supply voltage
  drop), set the parameters and see the plot and a comparison with the theoretical value.
- **Twin** — the virtual Raspberry Pi: Start / Stop, device cards with inputs (the "Press" button,
  the BME280 temperature slider, distance, ...) and outputs (LED brightness, servo angle, OLED
  image), a console with the firmware log, a virtual GPIO header and scenario runs. Parts in the 3D
  view react live (the LED lights up, the needle rotates).
- **Print** — printed parts: dimensions, mass, time, cost, printability status, overhang map, bed
  view and STL / 3MF / STEP downloads.

## Demo project: `projects/demo_gauge`

A desk climate gauge that uses every subsystem: a Raspberry Pi 4B in a printed PETG enclosure, a
BME280 sensor (temperature, humidity, pressure), an SSD1306 OLED display in a lid window, an SG90
servo turning a printed needle over a printed 0–40 °C dial, a button that changes the display mode, a
red LED that blinks on every reading, and a 5 V fan switched on above 28 °C by a MOSFET with a
flyback diode.

```sh
.venv/bin/piforge build projects/demo_gauge --scenarios     # ~35 s
.venv/bin/piforge serve projects/demo_gauge
```

In the Twin tab press Start and move the BME280 Temperature input: the needle rotates in the 3D view,
and above 28 °C the fan turns on (it glows blue). Pressing `SW1` changes the page on the OLED.
Details and expected warnings: `projects/demo_gauge/README.md`.

## Printing the parts

After `piforge build`, the print files are in `projects/<name>/build/parts/`:

- `<part>.stl` and `<part>.3mf` — ready for the slicer. **The recommended orientation is already
  applied** (the part is rotated to its print position and placed on the bed, lowest point at z = 0),
  so nothing needs to be rotated in the slicer. Example: the enclosure lid is rotated by 180° so that
  its outer face lies on the bed. Just load the file and set your parameters.
- `<part>.step` — the CAD model in the design frame (not rotated), for further work in CAD.
- `parts/index.json` — for each part: material, quantity, rotation, mass, time, cost, overhangs.
- `renders/part_<part>.png` — a preview of the part in its print position; faces that need supports
  are marked red.

Tips:

- The material and printer are chosen in `project.py` (`MATERIAL`, `PRINTER`; lists: `piforge info
  printers` and `piforge info materials`). Filament prices are in PLN.
- Before printing, read `build/report.md` and the Print tab: a part flagged as needing supports, too
  large for the bed or with thin walls (`PRINT.*`) should be fixed in the code.
- "Reference" parts (modules, screws, the Pi board) appear only in the 3D view and are not printed.
- Screws: the enclosure uses M2.5 for the Pi board (threaded into printed standoffs) and M3 for the
  lid; their lengths are visible as "fastener" parts in the GUI tree (they are not in the electronics
  BOM).
- Demo: the dial (`dial`) is printed face up with a filament change at a height of 1.2 mm, so the
  raised markings come out in a different color.
- If you have a slicer, `piforge print projects/X --slice` also computes G-code (PrusaSlicer directly;
  OrcaSlicer and Bambu Studio need their own profile files).

## Repository layout

```
src/piforge/      code: core, elec, spice, twin, mech, fab, render, analysis, project, build, cli, server, web
projects/         projects (_template = template for `piforge new`, demo_gauge = example)
tests/            tests (pytest; markers slow, spice, gui)
docs/superpowers/ specification and implementation plan
CLAUDE.md         working notes for Claude (workflow, API, finding codes, limitations)
```

Tests: `.venv/bin/python -m pytest tests/<area> -q` (areas: core, elec, fab, mech, spice, twin,
analysis, render, project, server, gui). Do not use `-n` (little RAM, heavy swap).

## Limitations

- Raspberry Pi only (Pi 5, 4B, 3B+, Zero 2 W): no other microcontrollers, no PCB design (layout,
  routing, Gerbers), no schematic editor; wiring is point-to-point, and the KiCad export is just a
  netlist.
- SPICE: nine ready-made, parameterized benches for small analog circuits (LED, divider, RC filter,
  contact bounce, MOSFET, BJT, level shifter, I2C rise time, supply drop) — this is not a simulation
  of your whole circuit. There is no model of a fan or DC motor load (the demo uses a relay coil as a
  stand-in).
- The thermal model is steady-state (no time-dependent behavior, no local hotspots); there is no
  structural analysis (FEA).
- The digital twin runs Python firmware, in real time, on behavioral device models (not exact bus
  timing). A fan has no model of its own (in the demo it is a `relay`-type output). C / Arduino /
  MicroPython on hardware is not supported.
- The enclosure generator makes a rectangular box with a lid for a single Pi. The microSD card is
  accessible after removing the lid. Modules must be in the database (`piforge info modules`); others
  are modeled by hand as parts.
- Print time, mass and cost are estimates, not slicer output.
- Building a project with geometry takes from over ten to a few dozen seconds (the CAD kernel); for
  quick iteration use `piforge check` and `build --no-render --no-spice`.
