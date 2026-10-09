# PiForge — design spec

Date: 2026-10-04 · Status: approved (self-approved under the user's `/goal` autonomy directive)

## 1. Intent

**What the user asked for (verbatim intent, translated from Polish):**
"Write yourself software, any stack, for designing electronics and 3D printing. We'll build something
based on Raspberry Pi + 3D printing + electronics. You should be able to design everything, simulate
everything, test, and export 3D-printable parts. Use as many tokens/subagents as you want, test and
validate everything. There must be an option to view in 3D and test by giving input data."

**Who uses it:**
- Primary: Claude, through a Python API + CLI, reading PNG renders and Markdown/JSON reports.
- Secondary: the user (Kacper), through a local web GUI: 3D viewer, checks, simulations with inputs,
  digital twin (virtual Raspberry Pi running the real firmware with interactive inputs), downloads.

**Assumptions (not stated by the user):**
- The next project is a Raspberry Pi device with modules/breakouts wired to the 40-pin header, plus
  3D-printed enclosure/mechanisms (folder name "money-counter" hints at a coin/banknote counter:
  motors, IR sensors, camera, display, buttons — supported generically, not built yet).
- Custom PCB layout is NOT needed now (KiCad netlist export is the hand-off point).
- Printer is a common FDM machine (profiles for Bambu A1/P1S/X1C, Prusa MK4/Mini/Core One, Ender 3).

**Success criteria:**
1. One `project.py` describes a device; `piforge build` produces printed parts (STL/3MF/STEP),
   assembly (GLB + scene.json), electronics outputs (BOM, wiring table + diagram, pinout, boot
   config.txt, KiCad netlist), a validation report (ERC, power, printability, interference, port
   access, thermal), SPICE results with plots, PNG renders — exit code ≠ 0 on errors.
2. `piforge serve` opens a web GUI: orbit/explode/section/measure the 3D assembly, see checks, run
   SPICE benches with editable inputs + plots, run the digital twin and interact (buttons, sliders)
   while outputs (LEDs, servos, motors, displays, logs) update in panels AND in the 3D view.
3. Firmware (real Python using gpiozero / RPi.GPIO / smbus2 / spidev / Adafruit Blinka drivers)
   runs unmodified in the twin; scripted scenarios give automated pass/fail tests.
4. pytest suite green, including validation against analytic physics and datasheet values; GUI E2E.
5. Example project builds end-to-end with zero errors.

## 2. Constraints

- macOS arm64, Python 3.12, 8 GB RAM under heavy swap (a qemu VM runs on the host) → keep
  parallelism low, lazy-import heavy libs, never import OCP outside `piforge.mech`/`analysis`.
- ~14 GB free disk → no KiCad/FreeCAD install; vendor only needed three.js files.
- Repo lives in iCloud Drive (`*.nosync` dir) → `.venv` is a symlink to `~/.local/share/piforge/venv`.
- External binaries: `ngspice` (Homebrew). Optional: PrusaSlicer/Orca CLI (not installed), Blender
  (installed at /opt/homebrew/bin/blender, optional beauty renders).
- Units: millimetres for geometry, SI for electronics (V, A, Ω, F, H, s), °C for temperature.

## 3. Stack

| Concern | Choice | Why |
|---|---|---|
| CAD kernel | build123d 0.13 (OpenCascade via cadquery-ocp-novtk 8.0) | B-rep, fillets, STEP, robust booleans, algebra API |
| Mesh analysis | trimesh 5 + manifold3d | watertight checks, ray casts, fast mesh booleans |
| Circuit sim | ngspice 47 (batch mode, ASCII raw parsing) | industry standard SPICE |
| Twin | pure Python; real `gpiozero` with custom pin factory; shims for RPi.GPIO, smbus2, spidev, Blinka, picamera2 | firmware runs unmodified |
| GUI backend | FastAPI + uvicorn + websockets | async, simple |
| GUI frontend | vanilla ES modules + three.js (vendored) + uPlot (vendored), no build step | zero toolchain, easy to maintain |
| Static renders | own numpy z-buffer rasterizer → PNG (Pillow) | headless, deterministic, no GPU |
| Plots | matplotlib (PNG) + JSON traces for GUI | |
| CLI | typer (lazy imports) | |
| Tests | pytest (+ markers `slow`, `spice`, `gui`), Playwright (chromium) for GUI E2E | |

## 4. Package layout & dependency rules

```
src/piforge/
  core/        report.py (Finding/Report), cache.py, util
  fab/         profiles.py (printers, materials), analyze.py, orient.py, estimate.py, slicer.py
  render/      raster.py, views.py, colors.py
  mech/        part.py, primitives.py, fasteners.py, boards.py, modules.py, enclosure.py, gears.py,
               mechanisms.py, assembly.py, export.py
  elec/        model.py, library/ (rpi.py, modules.py, passives.py, semis.py), erc.py, power.py,
               pinmap.py, bootconfig.py, bom.py, wiring.py, kicad.py
  spice/       netlist.py, models.py, runner.py, benches.py, plot.py
  twin/        clock.py, gpio.py, bus.py, devices/, gpiozero_factory.py, shims/ (top-level module
               names: RPi, smbus2, smbus, spidev, board, busio, digitalio, pwmio, micropython,
               picamera2, …), runner.py, ipc.py, scenario.py, from_circuit.py
  analysis/    thermal.py, kinematics.py, (fea.py — stretch)
  project.py   Project API;  build.py  pipeline;  cli.py
  server/      app.py (FastAPI), twin_bridge.py
  web/         index.html, css/, js/, vendor/three/, vendor/uplot/
tests/<same tree>, projects/<name>/project.py, projects/_template/
```

Import rules (no cycles; enforced by a test):
- `core` → nothing internal. `fab`, `render`, `elec`, `spice`, `twin` → `core` (+ `twin.from_circuit` → `elec`).
- `mech` → `core`, `fab.profiles`. `analysis` → `core`, `mech`, `fab`, `elec`.
- `project`/`build`/`cli`/`server` → anything. `build123d` may be imported ONLY in `mech`, `analysis`,
  `project`, `build` (and lazily in cli/server).

## 5. Shared contracts

### 5.1 `piforge.core.report`
```python
class Severity(IntEnum): INFO = 0; WARNING = 1; ERROR = 2
@dataclass
class Finding:
    code: str              # dotted, UPPER: "ERC.LEVEL_MISMATCH", "PRINT.OVERHANG", "ASM.INTERFERENCE"
    severity: Severity
    message: str           # one human sentence with numbers + units
    subject: str = ""      # "net:SDA", "part:lid", "pin:U1.VIN", "joint:flap"
    data: dict = {}        # machine-readable numbers
    hint: str = ""         # how to fix
@dataclass
class Report:
    title: str; findings: list[Finding]
    add(code, severity, message, subject="", hint="", **data) -> Finding
    extend(iterable|Report); errors/warnings/infos; ok (no errors); counts
    to_dict(); to_markdown(); merge(*reports, title) (classmethod)
```

### 5.2 `piforge.fab.profiles`
`Material` (name, density_g_cm3, glass_transition_c, max_service_c, youngs_modulus_mpa,
tensile_strength_mpa, z_strength_factor, cost_per_kg, thermal_conductivity) and `PrinterProfile`
(name, build_x/y/z, nozzle_d, layer_h, line_w, hole_compensation, clearance_press/sliding/loose,
max_overhang_deg (measured from vertical), max_bridge_mm, min_wall, min_feature). Registries
`PRINTERS`, `MATERIALS`, helpers `get_printer(name)`, `get_material(name)`.

### 5.3 Geometry hand-off
- Universal mesh type: `trimesh.Trimesh`, units mm, Z up, print bed = z=0 plane.
- `piforge.mech.export.to_trimesh(shape, tolerance=0.05, angular_tolerance=0.2)` is the ONLY bridge
  from build123d to meshes.
- `mech.part.PartSpec`: `name, shape (build123d), kind ∈ {printed, reference, fastener, pcb},
  material="PLA", color="#hex", quantity=1, print_rotation=(rx,ry,rz) deg | None (auto), meta={}`.
- `mech.assembly.Assembly`: nodes with world `Location`, optional parent, optional `Joint`
  (`type ∈ {revolute, prismatic}`, `axis`, `origin` (in node frame), `min`, `max`, `value`,
  `driven_by` = twin device id + property). Checks: interference (OCC common volume > tol),
  clearance, joint sweep. Export: `to_scene(out_dir)` → `scene.json` + one GLB per unique part.

### 5.4 Scene JSON (GUI contract), `build/scene.json`
```json
{"name":"demo","units":"mm","up":"Z",
 "nodes":[{"id":"base","name":"Base","kind":"printed","mesh":"meshes/base.glb","color":"#3b82f6",
           "matrix":[16 floats, column-major],"parent":null,"material":"PETG",
           "joint":{"type":"revolute","axis":[0,0,1],"origin":[0,0,0],"min":-90,"max":90,"value":0,
                    "driven_by":{"device":"SERVO1","prop":"angle"}},
           "emissive_from":{"device":"D1","prop":"brightness","color":"#ff2200"},
           "explode":[0,0,1]}],
 "bounds":[[x,y,z],[x,y,z]]}
```
`matrix` is the node transform LOCAL to its parent at rest pose (joint value 0), 16 floats
column-major; `world_matrix` (same format) is the informative world transform at the current joint
values. Viewer rule: world = parent_world · matrix · T(origin) · R(axis, value°) · T(−origin)
(prismatic: translate along axis by value mm). Meshes (GLB) are in millimetres, Z-up, in the node's
own frame.

### 5.5 Electronics model (`piforge.elec.model`)
`PinType ∈ {POWER_IN, POWER_OUT, GND, INPUT, OUTPUT, BIDIR, OPEN_DRAIN, PASSIVE, ANALOG, NC}`.
`Pin(name, number, type, voltage, v_max, vih, vil, voh, vol, i_max_ma, functions, aliases)`.
`PartDef(key, name, category, pins, supply{v_min,v_max,i_typ_ma,i_max_ma}, i2c_addresses,
params, sim{twin, spice}, mech, datasheet, notes)`. `Circuit.add(key|PartDef, ref, **params) -> Part`,
`Part[pin_name]` → `PinRef`, `Circuit.connect(*pinrefs, name=None) -> Net`,
`Circuit.configure(part, pulls={...}, interfaces={...})`. Pi GPIO referenced as `pi["GPIO17"]`,
`pi["pin11"]`, `pi["SDA1"]`, `pi["3V3"]`, `pi["5V"]`, `pi["GND"]` (a free physical GND is assigned).

### 5.6 Twin protocol (runner ⇄ server, JSON lines over TCP localhost)
- server → runner: `{"op":"input","device":"SW1","prop":"pressed","value":true}`, `{"op":"stop"}`.
- runner → server: `{"op":"hello","devices":[{id,type,inputs:{prop:{type,min,max,unit,default}},
  outputs:{...}}]}`, `{"op":"state","t":1.23,"pins":{"17":1,…},"devices":{"D1":{"brightness":1.0},…}}`
  at ≤ 30 Hz, `{"op":"display","device":"OLED1","w":128,"h":64,"png_b64":"…"}` on change,
  `{"op":"log","stream":"stdout","text":"…"}`, `{"op":"exit","code":0,"error":null}`.

## 6. Subsystems

### 6.1 fab — printability
Watertight/manifold/volume; bed fit (in chosen orientation); overhang faces (downward angle from
vertical > `max_overhang_deg` + 1°, excluding bed contact; short supported spans ≤ `max_bridge_mm` are
bridges, not overhangs) with area + face masks; thin walls via inward ray casts
from sampled faces (thickness < `min_wall`); first-layer contact area; small features; auto-orientation
(candidate rotations: 6 axis-aligned + largest convex-hull faces; score = overhang area, height,
contact area); estimates (mass, filament length, cost, rough time); optional slicer CLI if installed.
Output `PrintAnalysis` dataclass + `Report`.

### 6.2 render — PNG previews
Vectorized numpy z-buffer rasterizer: flat/Lambert shading, per-part colours, optional per-face
colour overrides (heatmaps), silhouette/crease edge lines, views iso/front/back/left/right/top/bottom,
multi-view sheet with bbox dimensions, transparent background option. 100k triangles < 3 s.

### 6.3 mech — CAD library
Primitives (rounded boxes, shells, vents, text, slots, bottom chamfer), fasteners (M2–M5:
clearance/tap/heat-set insert holes, nut traps, counterbores/countersinks, screw reference models),
Raspberry Pi board models (Pi 5, 4B, 3B+, Zero 2 W; PCB, holes, connectors with outward
direction, opening size, plug envelope, keep-outs; data cross-checked against official mechanical
drawings), module mechanical models (displays, sensors, buttons, LEDs, motors, fans, camera,
regulators…), enclosure generator (base + lid, standoffs, automatic port cutouts, panel components,
vents, labels, lid fastening: screws/inserts/snap), gears (involute spur, rack; mesh check),
mechanisms (hinge, snap-fit with strain check, cable clip, DIN clip, servo horn adapter, funnel,
chute, bearing seat), assembly + export (STL/3MF/STEP/GLB).

### 6.4 elec — electronics
Library (Pi boards with full 40-pin data incl. alt functions; ~40 common modules and discrete
parts with electrical specs), ERC (unconnected required pins, rail voltage compatibility, logic-level
mismatch incl. 5 V into 3.3 V GPIO, output contention, rail shorts, I2C address conflicts & pull-ups,
GPIO current per pin/total, LED resistor sizing, inductive loads without flyback, non-logic-level
MOSFET gate drive, floating inputs, reserved/conflicting pins), power budget (rails, PSU, Pi
consumption, 3V3 limit), pin allocator (+ boot `config.txt` overlay lines), BOM (CSV/MD), wiring
table + wiring diagram (SVG/PNG, colour-coded), KiCad netlist export.

### 6.5 spice — circuit simulation
Netlist builder (R, C, L, V/I with DC/PULSE/PWL/SIN, D, Q, M, S, B, X), model library (LEDs,
diodes, BJTs, MOSFETs, Pi GPIO output, relay coil, DC motor, PSU+cable), ngspice batch runner with
ASCII raw parser + `.meas` parsing, parametrised benches (LED driver, divider, RC filter, debounce,
MOSFET low-side with/without flyback, BJT switch, BSS138 level shifter, I2C rise time, power path
brown-out), PNG plots + JSON traces. Validated against analytic results.

### 6.6 twin — digital twin
`VirtualPi` (BCM 0–27: mode, pull, level, PWM duty/freq, edge callbacks), net-level resolution with
contention detection, I2C/SPI buses, device models (button, switch, limit switch, IR break-beam, PIR,
HC-SR04, LED/RGB LED, buzzer, relay, servo, 28BYJ-48 stepper + ULN2003, DC motor + H-bridge, BME280,
SSD1306, LCD1602 via PCF8574, MCP3008, HX711 + load cell, rotary encoder, NeoPixel, camera stub),
shims so firmware runs unmodified, runner subprocess + IPC (5.6), scenarios (timed inputs +
expectations → pass/fail report), `from_circuit(circuit)` auto-wiring from the netlist.

### 6.7 analysis
Thermal lumped model (enclosure ΔT from power, walls, vents/chimney flow, fan) with checks vs Pi
throttle temperature and material glass transition; kinematic joint sweeps with interference; port
access (plug envelope vs enclosure); FEA (stretch, scikit-fem, validated on a cantilever).

### 6.8 project / build / CLI
`Project` collects circuit, parts, assembly, firmware, scenarios, SPICE benches, links between twin
devices and 3D nodes. `build(project_dir)` writes `build/` (parts/, meshes/, scene.json, renders/,
elec/, sim/, report.md, report.json). CLI: `new, build, check, render, print, spice, twin run,
twin test, serve, info`.

### 6.9 server + web GUI
FastAPI: `/api/project`, `/api/scene`, `/meshes/*`, `/api/report`, `/api/build` (rebuild),
`/api/elec/*` (wiring svg, bom, pinout), `/api/spice/benches` + `POST /api/spice/run`,
`/api/print/{part}` (analysis + per-face overhang colours), `/files/*` downloads, `WS /ws/twin`.
Frontend: left tree (visibility, colours, select), centre three.js viewport (orbit, grid, explode
slider, section plane, measure, overhang heatmap, print-bed view), right tabs: Checks, Electronics,
SPICE (inputs + uPlot charts), Twin (auto-generated input widgets, output indicators, display
canvases, console), Print (stats + downloads). Live reload on rebuild.

## 7. Error handling
- Library/API errors raise `PiForgeError` subclasses with actionable messages; checks never raise for
  design problems — they return `Finding`s.
- Missing optional tools (slicer, Blender) → INFO finding + graceful skip; missing ngspice → spice
  tests skipped with a clear reason, CLI prints install hint.
- Twin: firmware exceptions are captured and reported (`exit` message with traceback), never crash
  the server; runner killed on stop/timeout.

## 8. Testing & validation
- Unit tests per module; validation tests against physics/datasheets (RC τ, LED current, divider,
  I2C rise time, gear mesh, thermal energy balance, cantilever if FEA, rasterizer z-order).
- Golden data tests: Pi mechanical dimensions, 40-pin header table (26 GPIO + 2 ID + 8 GND + 2×5V +
  2×3V3), alt-function maps.
- Twin tests run real gpiozero + Adafruit drivers against device models.
- E2E: example project build with zero errors; twin scenarios pass; GUI Playwright tests (loads,
  renders N meshes, twin button → LED state change).
- Visual review: Claude inspects rendered PNGs and GUI screenshots.

## 9. Non-goals (for now)
PCB layout/routing/Gerbers, schematic-capture GUI, own G-code generator, cloud hosting, non-Pi MCUs.
