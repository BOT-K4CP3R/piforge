# PiForge — working notes for Claude

PiForge is a code-first toolkit (Python 3.12) for designing Raspberry Pi devices: electronics +
3D-printed mechanics + firmware, with simulation (SPICE, digital twin), validation and export.
One `projects/<name>/project.py` describes a whole device; `piforge build` turns it into
`build/` (printable parts, renders, wiring/BOM, SPICE plots, twin results, one report). The same
folder is shown to the user in a web GUI (`piforge serve`).

- Design spec: `docs/superpowers/specs/2026-10-04-piforge-design.md`
- Implementation plan: `docs/superpowers/plans/2026-10-04-piforge.md`
- Reference projects: `projects/_template/` (Pi + plate + button + LED), `projects/demo_gauge/`
  (every subsystem: enclosure, I2C sensor + OLED, servo needle joint, fan + MOSFET, thermal, 4
  scenarios, 4 SPICE benches). Read `demo_gauge/project.py` before designing anything non-trivial.

## Environment

- Repo path contains spaces and `~` — always quote paths; subprocesses use argv lists.
- Python: `.venv/bin/python` (symlink to `~/.local/share/piforge/venv`, outside iCloud). Never create
  another venv. Add packages with `uv pip install --python .venv/bin/python <pkg>`.
  The CLI is `.venv/bin/piforge` (or `.venv/bin/python -m piforge`).
- Tests: `.venv/bin/python -m pytest tests/<area> -q`. Markers: `slow` (CAD kernel), `spice`
  (ngspice), `gui` (Playwright). Do not use `-n` (8 GB RAM host, heavy swap).
- External tools: `ngspice` (Homebrew, /opt/homebrew/bin/ngspice; SPICE stages are skipped with a
  finding when missing). Blender at /opt/homebrew/bin/blender (optional). A slicer (PrusaSlicer,
  OrcaSlicer, Bambu Studio, or `$PIFORGE_SLICER`) is only needed for `piforge print --slice`.
- Scratch scripts and throw-away projects go to the session scratchpad, not into the repo.
- Do not commit unless the user asks.

## Layering rules

`core` ← `fab`, `render`, `elec`, `spice`, `twin` ← `mech` ← `analysis` ← `project`/`build`/`cli`/`server`.
`build123d`/`OCP` may be imported only in `piforge.mech`, `piforge.analysis`, `piforge.project`,
`piforge.build` (lazily elsewhere). `tests/test_import_rules.py` enforces it. `import piforge.mech`
is lazy (names resolve on first use) so electronics-only projects never load the CAD kernel.

## Conventions

- Units: mm, V/A/Ω/F/H/s, °C (rotations in degrees). Z up; print bed at z = 0.
- Design problems → `Report`/`Finding` (`piforge.core.report`), codes like `ERC.LEVEL_MISMATCH`.
  Usage errors → `PiForgeError` subclasses (`NotFoundError` suggests close matches,
  `ValidationError`, `ToolNotFoundError`). A design problem is never an exception.
- Every hard-coded physical number (board dimensions, GPIO limits, material data) carries a
  `# src:` comment naming its source.
- Rotation tuples `(rx, ry, rz)` (`print_rotation`, 6-tuple locations) are extrinsic X→Y→Z. A
  build123d `Location((x,y,z),(rx,ry,rz))` is intrinsic — they differ when two angles are non-zero.
- Twin device ids are the circuit's part references (`SW1`, `D1`, `U2`…).

## Workflow for a new device

1. `.venv/bin/piforge new NAME --board rpi5|rpi4b|rpi3bp|rpizero2w` (default dir `projects/`). Gives
   `project.py`, `firmware/main.py`, `README.md` (a Pi on a plate with a button + LED).
2. Edit `project.py` (`build(p)`) and `firmware/main.py`. Firmware is ordinary Python (gpiozero,
   RPi.GPIO, smbus2, spidev, Adafruit Blinka drivers…) and must run unmodified on the Pi.
3. `piforge check projects/NAME` — fast (≈ 10 s), writes nothing: ERC, power budget, printability,
   assembly interference, user checks, twin wiring, firmware syntax. Fix every ERROR; read WARNINGs.
4. `piforge build projects/NAME [--scenarios]` — writes `build/` (≈ 15–40 s). Exit code 1 if any
   ERROR. Add `--no-render`, `--no-spice`, `--formats stl,3mf,step,glb`, `--out DIR` as needed.
5. **Look at the result**: Read `build/renders/assembly_iso.png`, `assembly_sheet.png` and each
   `part_<name>.png` (the Read tool shows PNGs; overhang faces are painted red in part renders),
   `build/elec/wiring.png`, `build/sim/<label>.png`. Then read `build/report.md`. Do this after every
   geometry change; do not trust a green report alone.
6. SPICE: `piforge spice BENCH -p key=value … [--plot out.png]` to explore a bench quickly; add
   `p.spice(...)` to the project once the values are settled.
7. Twin: `piforge twin test projects/NAME [-s scenario]` (PASS/FAIL per scenario),
   `piforge twin run projects/NAME --duration 10` (stream the firmware log, no inputs).
8. For the user: `piforge serve projects/NAME --port 8765` (add `--no-browser` when you start it
   yourself; run it in the background, then open it in the Browser pane). It builds first if there is
   no build, and the GUI's Rebuild button re-runs the pipeline after you edit `project.py`.
9. Export: the printable files are already in `build/parts/` (`<name>.stl|3mf|step`, `index.json`
   lists orientation, mass, time, cost). STL/3MF are rotated to the part's print orientation and sit
   on the bed (min z = 0); STEP and GLB keep the design frame. `piforge print projects/NAME [--part P]
   [--slice]` shows the per-part table (and G-code in `<project>/gcode/` with a slicer).

Other CLI: `piforge render FILE_OR_PROJECT [--view iso|front|back|left|right|top|bottom] [--sheet]
[--out x.png]`; `piforge info boards|modules|parts|printers|materials|benches|devices` lists
everything usable in `project.py` (always check these instead of guessing keys; unknown keys raise
`NotFoundError` with suggestions). `-v` before the command shows log messages.

## Project API cheat-sheet (`piforge.project.Project`)

```python
from piforge.project import Project

def build(p: Project) -> None:
    p.meta(description="…", board="rpi4b", printer="prusa_mk4", material="PETG")
    p.circuit                      # piforge.elec Circuit
    p.assembly                     # piforge.mech Assembly (created on first use → loads the CAD kernel)
    p.add_printed(*parts)          # printed PartSpecs → build/parts/ + printability analysis
    p.add_check(fn, name="x")      # fn() -> Report; shows as source "check:x"
    p.thermal(ThermalInputs(...), label="fan on")  # a thermal case; call several times with labels
    p.firmware("firmware/main.py") # relative to the project folder; must exist
    p.scenario(Scenario(...))      # twin scenario (unique names)
    p.spice("led_driver", label="status_led", r_series=330, led="led_red")  # validated immediately
    p.twin_override("M1", min_pulse=0.0005, max_pulse=0.0024)  # tweak an auto-derived device
    p.twin_device(DeviceConfig("M2", "relay", pins={"pin": 22}))  # device the circuit cannot derive
    p.assembly_check(ignore=[("lid", "button")], min_volume=0.5, sweep_joints=True)
```

`project.py` may import sibling modules (`parts.py`, as in demo_gauge); they are reloaded on every
load. A broken `project.py` raises `ProjectLoadError` with the traceback; any other failure in a build
stage becomes `PROJECT.STAGE_FAILED` (ERROR, traceback in the finding data) and the build goes on.

A complete minimal project (enclosure with panel items and vents, joint driven by the twin,
two thermal cases, one scenario, one SPICE bench) builds clean with 0 errors/warnings:

```python
from piforge.analysis.thermal import FAN_30MM_CFM, ThermalInputs
from piforge.mech import Enclosure, EnclosureSpec, Joint, PanelItem, PartSpec, VentSpec, rounded_box
from piforge.project import Project
from piforge.twin.scenario import Scenario, Step

BOARD, PRINTER, MATERIAL = "rpi4b", "prusa_mk4", "PETG"


def build(p: Project) -> None:
    p.meta(description="Pi in a box with an LED and a button", board=BOARD, printer=PRINTER, material=MATERIAL)
    c = p.circuit
    pi = c.add(BOARD, "U1")
    c.add("psu_usbc_5v3a", "PS1")                       # unconnected PSU = the Pi's USB-C supply
    sw = c.add("pushbutton_12mm", "SW1")
    r1 = c.add("resistor", "R1", value=330)
    led = c.add("led", "D1", color="red")
    c.configure(pi, pulls={"GPIO17": "up"})
    c.connect(pi["GPIO17"], sw["A"]); c.connect(sw["B"], pi["GND"])
    c.connect(pi["GPIO27"], r1["1"]); c.connect(r1["2"], led["A"]); c.connect(led["K"], pi["GND"])

    enc = Enclosure(EnclosureSpec(
        board=BOARD, inner_height=30.0, ports=("power", "hdmi0", "usb3", "ethernet"),
        panel_items=(PanelItem("pushbutton_12mm", "top", (10, -14)), PanelItem("led_5mm", "top", (10, 8))),
        vents=(VentSpec("+y", size=(60, 10), offset=(0, 6)), VentSpec("top", size=(26, 14), offset=(-20, 0))),
        printer=PRINTER, material=MATERIAL))
    flag = PartSpec("flag", rounded_box(30, 4, 2, radius=1), material=MATERIAL, color="#f59e0b",
                    print_rotation=(0, 0, 0))
    p.add_printed(*enc.parts, flag)                      # enc.parts = [base, lid]
    asm = p.assembly
    for n in enc.assembly().nodes:                       # board, screws, modules, base, lid
        glow = {"module_1_led_5mm": {"device": "D1", "prop": "brightness", "color": "#ff2200"}}.get(n.id)
        asm.add(n.part, n.loc, id=n.id, explode=n.explode, emissive_from=glow)
    asm.add(flag, (0, 0, enc.outer_size[2] + 15), id="flag", explode=(0, 0, 10),
            joint=Joint("revolute", axis=(0, 0, 1), min=-90, max=90, value=0,
                        driven_by={"device": "D1", "prop": "brightness", "scale": 90.0, "offset": 0.0}))
    p.add_check(enc.checks, name="enclosure")            # port access, walls, standoffs, screws

    vents = dict(vent_in_mm2=800, vent_out_mm2=800, vent_height_mm=12)
    p.thermal(ThermalInputs(power_w=2.7, outer_mm=enc.outer_size, wall_mm=enc.spec.wall, material=MATERIAL, **vents),
              label="idle")
    p.thermal(ThermalInputs(power_w=3.5, outer_mm=enc.outer_size, wall_mm=enc.spec.wall, material=MATERIAL,
                            fan_cfm=FAN_30MM_CFM, **vents), label="fan on")

    p.firmware("firmware/main.py")
    p.scenario(Scenario("press_toggles_led", duration=2.0, steps=[
        Step(at=0.5, action="input", device="SW1", prop="pressed", value=True),
        Step(at=0.7, action="input", device="SW1", prop="pressed", value=False),
        Step(at=0.9, action="expect", device="D1", prop="brightness", value=1.0, tol=0.01),
    ]))
    p.spice("led_driver", label="status_led", r_series=330, led="led_red")
```

## `piforge.elec` — circuits, ERC, power

Pure Python, no CAD kernel. Pin names come from the part definition (`piforge info parts`; examples:
pushbutton `A,B`; led `A,K` (anode, cathode); resistor `1,2`; sg90_servo `V+,GND,SIG`; bme280_breakout
`VIN,GND,SCL,SDA`; ssd1306_096_i2c `VCC,GND,SCL,SDA`; nmos_ao3400 `G,S,D`; fan_5v `+,-`; boards
`GPIO0…GPIO27, 3V3, 5V, GND`). Wrong pin or part names raise `PinNotFoundError`/`NotFoundError`
listing the candidates.

```python
from piforge.elec import Circuit, run_erc, power_budget, bom, allocate_pins

c = Circuit("demo")
pi = c.add("rpi4b", "U1")                          # ref optional; extra kwargs are part params
c.add("psu_usbc_5v3a", "PS1")
env = c.add("bme280_breakout", "U2", i2c_address=0x76)
oled = c.add("ssd1306_096_i2c", "U3", i2c_address=0x3C)
for dev, vcc in ((env, "VIN"), (oled, "VCC")):     # I2C1 shared by both
    c.connect(pi["3V3"], dev[vcc]); c.connect(pi["GND"], dev["GND"])
    c.connect(pi["GPIO2"], dev["SDA"]); c.connect(pi["GPIO3"], dev["SCL"])
c.configure(pi, interfaces={"i2c": True}, pulls={"GPIO17": "up"})   # → config.txt overlays, ERC pull checks
sw = c.add("pushbutton", "SW1")
c.connect(pi["GPIO17"], sw["A"]); c.connect(sw["B"], pi["GND"])
# low-side switch of a 5 V fan: connect() takes any number of pins and accepts name=
fan, q, fly = c.add("fan_5v", "M2"), c.add("nmos_ao3400", "Q1"), c.add("schottky_1n5819", "D2")
c.connect(pi["5V"], fan["+"], fly["K"])
c.connect(fan["-"], q["D"], fly["A"], name="FAN_SW")
c.connect(q["S"], pi["GND"])
rep = run_erc(c)                  # Report; 20 rules, ERC.OK when clean
pb = power_budget(c)              # PowerBudget: per-rail typical/worst mA vs PSU
```

- `Circuit.connect` merges nets; power nets are named `GND`/`5V`/`3V3`, others `N$n` unless `name=`.
- `c.configure(pi, pulls={"GPIO17": "up"}, interfaces={"i2c": True})`: gpiozero `Button(17)` enables the
  internal pull-up, so declare it or ERC reports `ERC.FLOATING_INPUT`. Interfaces (aliases such as
  `i2c` accepted): i2c1, spi0, spi1, uart0, pwm, pcm, onewire, camera, id_eeprom.
- Add a PSU part (`psu_usbc_5v3a`, `psu_usbc_5v5a`, `psu_microusb_5v2a5`, `psu_dc_12v2a`) or get
  `POWER.NO_PSU` (INFO). Servos/motors/relays: never drive from a GPIO directly (`ERC.MOTOR_ON_GPIO`);
  inductive loads need a flyback diode (`ERC.INDUCTIVE_NO_FLYBACK`); LEDs need a series resistor.
- Custom parts: `piforge.elec.register(PartDef(...))` (see `elec/library/__init__.py` for the `params`
  conventions the ERC/power/twin code reads).
- Generated by the build into `build/elec/`: BOM (csv/md), wiring table/diagram, pinout, `config.txt`
  overlay lines, KiCad netlist, `power.json`.

## `piforge.mech` — CAD (build123d), enclosures, assemblies

Everything is mm, Z up. `PartSpec(name, shape, kind="printed"|"reference"|"fastener"|"pcb",
material, color, quantity, print_rotation, meta)`. Printed parts must have a known material
(`piforge info materials`); `print_rotation=None` lets the exporter pick an orientation
(`piforge.fab.orient.best_orientation`), a tuple fixes it (lid: `(180, 0, 0)`, outside face down).

```python
from piforge.mech import (EPS, PartSpec, rounded_box, standoff, to_location, get_board, get_module,
                          Enclosure, EnclosureSpec, PanelItem, VentSpec, Assembly, Joint)

board = get_board("rpi4b")            # .length .width .holes .bottom_clearance .port("ethernet").center .part()
plate = rounded_box(95, 66, 3, radius=4, bottom_chamfer=0.4)
boss = standoff("M2.5", 8 + EPS, hole="tap", printer="prusa_mk4")
plate = plate + boss.moved(to_location((10, 10, 3 - EPS)))      # overlap by EPS so unions have no coplanar faces
part = PartSpec("pi_plate", plate, material="PETG", color="#3b82f6", print_rotation=(0, 0, 0))
```

Other geometry helpers: `hollow_box, slot, vent_slots, hex_vents, text_solid, emboss, engrave,
chamfer_bottom`; fasteners `clearance_hole, tap_hole, insert_hole, counterbore_hole, countersink_hole,
nut_trap, screw, standoff` (`get_size("M3")`); `spur_gear, rack, check_mesh`; mechanisms
`snap_fit_cantilever, hinge, cable_clip, din_rail_clip, funnel, chute, bearing_seat, servo_mount,
shaft_coupler`.

**Enclosure** — base + lid around a board, with automatic port cutouts, standoffs, lid screws and
panel modules. Frame: origin at the centre of the base's outer bottom face, X along the board's long
edge, GPIO header towards +Y. Faces: `top` (lid), `bottom`, `-x`, `+x`, `-y`, `+y` (aliases front=-y,
back=+y, left=-x, right=+x). `offset=(u, v)` is in the face's local frame (origin = centre of the
inner surface, local Z = outward normal; walls: v = world Z; top: u = +x, v = +y).

```python
enc = Enclosure(EnclosureSpec(
    board="rpi4b", extra_space=(36, 0, 0, 0),         # extra inner room on -x, +x, -y, +y
    inner_height=34.0,                                 # raise it when top-mounted modules are tall
    ports=("power", "hdmi0", "usb3", "ethernet"),      # None = every horizontal edge port
    panel_items=(PanelItem("ssd1306_096_i2c", "top", (12, -8)),      # module key from `piforge info modules`
                 PanelItem("led_5mm", "top", (40, 8)),
                 PanelItem("fan_30mm", "-y", (-41, -1.5))),          # also a ModuleModel instance
    vents=(VentSpec("+y", size=(100, 14), offset=(0, 7)), VentSpec("top", size=(26, 14), offset=(42, 22))),
    lid_fastening="screws",                            # or "snap"; board_fastening "tap"|"insert"
    label="PiForge", printer="prusa_mk4", material="PETG"))
enc.parts            # [base, lid] PartSpecs (lid pre-rotated for printing)
enc.outer_size, enc.inner_size, enc.spec.floor, enc.spec.wall
enc.assembly()       # Assembly with ids: base, lid, board, board_screw_N, lid_screw_N, module_<i>_<key>
enc.checks()         # Report: ACCESS.*, ENCL.*, ASM.* (ports reachable, walls, screws, interference)
enc.print_checks()   # Report: printability of the generated geometry
```

Module windows/bosses come from the module database (`piforge info modules`; `piforge.mech.get_module`).
The snippet above is an illustration of the API (its arbitrary panel positions may raise
`ENCL.FEATURE_OVERLAP`); check `enc.checks()` for yours. Anything the enclosure cannot generate (a custom opening, extra posts) you cut or add yourself on
`enc.base`/`enc.lid` PartSpecs with `dataclasses.replace(part, shape=...)` — demo_gauge's `parts.py`
shows this for a fan grille and servo posts.

**Assembly** — placed parts, hierarchy, joints, live bindings to the twin:

```python
asm = p.assembly                                              # or Assembly("name")
asm.add(plate, id="plate")
asm.add(board.part(), (x0, y0, z0), id="pi", explode=(0, 0, 40))           # explode = exploded-view offset
asm.add(led_part, (12, py, 3), id="led", parent="plate",
        emissive_from={"device": "D1", "prop": "brightness", "color": "#ff2200"})   # glows with the twin
asm.add(needle, (x, y, z), id="needle",
        joint=Joint("revolute", axis=(0, 0, 1), origin=(0, 0, 0), min=-90, max=90, value=0,
                    driven_by={"device": "M1", "prop": "angle", "scale": 1.0, "offset": 0.0}))
```

`loc` is a build123d `Location`, `(x, y, z)` or `(x, y, z, rx, ry, rz)`. Joints are `"revolute"`
(degrees) or `"prismatic"` (mm); `driven_by` makes the GUI move the node with a twin device property
(`value = prop * scale + offset`). Also: `asm.check_interference(ignore=[("a","b")], min_volume=0.5)`,
`asm.sweep_joint("needle")`, `asm.bounds()`. The build runs both (`ASM.INTERFERENCE`,
`ASM.JOINT_COLLISION`); fasteners are exempt from interference by default.

## `piforge.spice` — ngspice benches

```python
from piforge.spice import run_bench, plot_result

res = run_bench("mosfet_lowside", mosfet="nmos_ao3400", v_supply=5.0, load="relay_coil_5v",
                diode="d1n5819", r_gate=100.0, r_pulldown=100_000.0, flyback=True)
res.measures        # simulated, SI units: i_load_on, v_ds_on, v_drain_peak, t_off …
res.analytic        # textbook values for the same keys;  res.errors_pct()
res.report          # SPICE.* findings (e.g. SPICE.SUMMARY, SPICE.FLYBACK_OVERVOLTAGE)
plot_result(res, "out.png")
```

Benches (`piforge info benches` shows params and defaults): `led_driver`, `voltage_divider`,
`rc_filter`, `button_debounce`, `mosfet_lowside`, `bjt_switch`, `level_shifter_bss138`,
`i2c_rise_time`, `power_path`. Bad values raise `BenchParamError` before ngspice starts; unknown
benches `NotFoundError`; no ngspice → `NgspiceNotFoundError`. Benches are parametrised templates, not
a netlist of your circuit: choose the bench and parameters that mirror the real subcircuit and say
so in a comment (loads: `relay_coil_5v`, `fan_5v`, `dc_motor_small`; demo_gauge uses `fan_5v`). Custom circuits:
`SpiceCircuit` (`V, R, C, …`, `Pulse/Sine/PWL`, `.tran/.ac/.op`, `.measure`) + `run(circuit)` →
`SimResult` (`.measures`, `.vectors`). In a project use `p.spice(bench, label=..., **params)`.

## `piforge.twin` — digital twin

The firmware runs unmodified in a subprocess (`python -m piforge.twin.runner`) with shim modules
for `RPi.GPIO`, gpiozero (pin factory), `smbus2`/`smbus`, `spidev`, `board`/`busio`/`digitalio`/
`neopixel`/`adafruit_dht`, `picamera2`, `w1thermsensor`… backed by a virtual Pi (BCM 0–27, pulls,
PWM, I2C/SPI buses) and device models (`piforge info devices`: button, switch, led, rgb_led, buzzer,
relay, servo, stepper_28byj48, dc_motor, hcsr04, bme280, ssd1306, lcd1602_pcf8574, mcp3008,
rotary_encoder, neopixel, pir, ir_breakbeam, limit_switch, dht22, ds18b20, camera…). Devices and
wiring are derived from the circuit (`twin_config_from_circuit`); adjust with `p.twin_override` and
`p.twin_device`. Each device lists its `inputs` (settable: `pressed`, `temperature`, `distance`…) and
`outputs` (observable: `brightness`, `angle`, `lit_pixels`…). `stepper_28byj48` has an opt-in stall model
(`p.twin_override("M1", stall_model=True, max_pps=950, start_pps=500, max_accel=5000)`): steps the motor
cannot follow are lost (`lost_steps`, `TWIN.STEPPER_LOST_STEPS`); input `slip` knocks the rotor back.

```python
from piforge.twin.scenario import Scenario, Step, run_scenario

Scenario("fan_on_above_28C", duration=4.5, settle=1.5, steps=[         # time 0 = firmware about to start
    Step(at=0.0, action="input",  device="U2", prop="temperature", value=22.0),
    Step(at=2.5, action="input",  device="U2", prop="temperature", value=31.0),
    Step(at=2.5, action="expect", device="M2", prop="on", value=True),                  # holds in [at, at+settle]
    Step(at=2.5, action="expect", device="M1", prop="angle", value=-38.5, op="~=", tol=3.0),
    Step(at=2.5, action="expect_log", pattern=r"T=31\.\d .*fan=1"),                    # regex on firmware stdout
])
```

`op` ∈ `== != < <= > >= ~= contains matches`; `settle` (default 0.5 s, per scenario; a `Step(settle=…)`
overrides it for that step) is how long after `at` the condition may become true. Steps run in order and
an expectation blocks until met (≤ its settle), so order checks before long waits. Leave ≈ 0.2 s at the start for firmware set-up. `Project.lint` rejects
steps naming unknown devices/inputs before anything runs. Run from code:

```python
from pathlib import Path
from piforge.project import load_project
from piforge.twin.scenario import run_scenario
from piforge.twin.session import TwinSession
import time

p = load_project(Path("projects/_template"))
rep = run_scenario(p.twin_config(), p.firmware_path, p.scenarios[0])        # Report (TWIN.SCENARIO_OK / TWIN.EXPECT_FAILED)
with TwinSession(p.twin_config(), p.firmware_path) as s:                    # interactive: what the GUI does
    s.wait_ready(20); time.sleep(0.5)                                       # firmware needs ~0.2 s to set up
    s.send_input("SW1", "pressed", True); time.sleep(0.3); s.send_input("SW1", "pressed", False)
    msg = s.wait_for(lambda m: m["op"] == "state" and m["devices"]["D1"]["brightness"] == 1.0, 3)
    print(s.logs()[-1], s.latest_state()["devices"])
```

The twin runs in real time (≈ scenario duration per scenario). A button press is two steps
(`pressed=True`, then `False` a little later, 0.2 s in the template); sending both back to back or
right after `hello` can be missed by the firmware.

## `piforge.analysis` / `piforge.fab` / `piforge.render`

```python
from piforge.analysis.thermal import ThermalInputs, enclosure_temperature, FAN_30MM_CFM

inp = ThermalInputs(power_w=3.5, outer_mm=(128, 64, 38), wall_mm=2.0, material="PETG",
                    vent_in_mm2=300, vent_out_mm2=500, vent_height_mm=12)   # fan_cfm=FAN_30MM_CFM adds a fan
res = enclosure_temperature(inp, board="rpi4b")
res.internal_c, res.soc_c, res.report      # → 52 °C, 83 °C, THERMAL.PI_THROTTLE
```

Steady-state lumped model (natural convection + radiation through the walls, stack-effect or fan
ventilation through the vents). `p.thermal(inputs, label="fan on")` may be called several times (fan
on/off, hot room, other material): with labels, `build/thermal.json` is a dict keyed by label and the
findings have source `thermal:<label>`; a single unlabelled call keeps the old layout (flat
`thermal.json`, source `thermal`). `enclosure_temperature(...)` can also be called directly from a
check (demo_gauge's `fan_on` does). Vent areas are open areas in mm² (slots: count × width × height; mind the
bars). Rules of thumb it enforces: PETG inside air ≤ 65 °C, Pi 4/5 SoC < 80 °C sustained.

- `piforge.fab`: `get_printer`, `get_material`, `analyze_mesh` (watertight, bed fit, overhangs,
  bridges, thin walls, estimate), `best_orientation`, `estimate_print`, slicer bridge. Printers/
  materials: `piforge info printers|materials`.
- `piforge.render`: headless numpy rasteriser (`render`, `render_views`, `save_png`, `RenderItem`), no
  GPU. `piforge.analysis.access`: plug/finger clearance at ports (used by `Enclosure.checks`).

## Findings: code prefixes and sources

Codes are `AREA.NAME`; severity `error` (design does not work / cannot be built), `warning`,
`info`. In the merged `build/report.*`, `Finding.source` says who produced it.

| Prefix | Meaning | Source label |
|---|---|---|
| `ERC.*` | electrical rules (levels, currents, pulls, I2C, flyback, grounds, reserved pins) | `erc` |
| `POWER.*` | PSU rating, rail margin/overload, missing PSU | `power` |
| `PRINT.*` | printability: overhang, bridge, thin wall, bed fit, watertight, estimate | `print:<part>` |
| `ASM.*` | assembly interference, joint sweep collisions | `assembly` |
| `ACCESS.*`, `ENCL.*` | port reachability, enclosure geometry/fit (walls, screws, standoffs, port depth, feature overlap, too-low interior) | `check:enclosure` (name you gave `add_check`) |
| `SPICE.*` | bench results vs textbook (LED current, flyback, rise time, brown-out…) | `spice:<label>` |
| `TWIN.*` | scenario outcome, GPIO contention, floating input, firmware errors | `twin:<scenario>` |
| `THERMAL.*` | over-temperature, throttling, material softening, starved fan | `thermal` / `thermal:<label>` |
| `PROJECT.*` | firmware syntax, invalid scenario, stage failure, skipped SPICE | `project` |
| `GEAR.*`, `SNAP.*`, `REF.*` | gear mesh, snap-fit strain, pin reference | whatever check returns them |
| your own, e.g. `DEMO.*` | user checks via `p.add_check` | `check:<name>` |

`Report`: `.add(code, severity, message, subject="", hint="", **data)`, `.extend()`, `Report.merge()`,
`.ok`, `.errors/.warnings/.infos`, `.has(code)`, `.by_code(prefix)`, `.to_markdown()`, `.to_json()`.
A deliberate what-if that is expected to fail belongs in a check that downgrades it to a WARNING
(flyback-off on an inductive load is graded by the drain peak vs the MOSFET rating: WARNING below
it, ERROR at/above it — demo_gauge runs one as a deliberate WARNING); the build must otherwise be
free of errors.

## Where outputs land (`<project>/build/`, git-ignored)

```
manifest.json            project summary, file lists per group, timings, built_at
report.md / report.json  merged findings (every stage)
scene.json               assembly nodes (matrix = rest pose), joints, driven_by/emissive_from bindings
meshes/*.glb             one GLB per scene node, part frame, mm, Z up
parts/<name>.stl|3mf|step   printable files (STL/3MF in print orientation, on the bed); parts/index.json
renders/assembly_iso.png, assembly_sheet.png, part_<name>.png
elec/bom.csv|md  circuit.json  wiring.json|md|png|svg  pinout.md  config.txt  netlist.net  power.json
sim/<label>.json|png  sim/index.json          SPICE benches
twin/config.json  twin/scenarios.json  twin/results.json   (results only with --scenarios)
thermal.json             only when p.thermal() was called (dict keyed by label with several cases)
```

The build writes to a staging folder and swaps it in at the end, so the GUI never sees a half build.
The GUI (`src/piforge/web`, FastAPI in `src/piforge/server`) only reads this layout: tabs Checks,
Electronics, SPICE (run benches with inputs), Twin (live firmware, device cards, scenarios), Print;
the left tree and 3D viewer support section, measure, X-ray, overhang heat-map and print-bed view.

## Tests

- `.venv/bin/python -m pytest tests/<area> -q` with areas `core elec fab mech spice twin analysis
  render project server gui`. Quick loop: `-m "not slow and not gui"`. `slow` = OCP/build123d
  booleans and tessellation; `spice` = needs ngspice; `gui` = Playwright + headless Chromium
  (`.venv/bin/playwright install chromium`), one server and one page per module.
- Per-test timeout is 900 s. Run areas one at a time (RAM). Tests are written in the same
  directory layout as `src/piforge`; add a test with every new rule/part/bench/device.
- The template and demo_gauge are built by `tests/project`; a change that breaks them breaks the suite.

## Known limitations

- Pi-only: no non-Pi MCUs, no PCB layout/routing/Gerbers, no schematic GUI, no own G-code generator.
  Wiring is point-to-point (jumpers/perfboard); KiCad export is a netlist only.
- SPICE benches are fixed templates for small analog subcircuits (9 listed), not a general
  simulation of the project circuit. Loads available: relay coil, small 5 V fan, small DC motor.
- Thermal: one steady-state lumped model (no transients, no board-level hotspots); Pi SoC rise is a
  per-board open-air measurement scaled by power. No FEA/structural analysis.
- Twin: Python firmware only (no C/Arduino/MicroPython hardware timing); real-time, device models
  are behavioural; a fan is modelled as a `relay`-type on/off device (as in demo_gauge); bus timing is not cycle
  accurate. The shim directory is only on `sys.path` inside the runner.
- Enclosure generator: rectangular base + lid for one Pi; SD-card access is via the lid; modules
  must exist in the module database (`piforge info modules`) — otherwise model them as `PartSpec`s
  or register a `ModuleModel`. STEP/GLB are in the design frame, not the print orientation.
- Printability numbers (time/cost/mass) are estimates; `--slice` needs an external slicer, and
  OrcaSlicer/Bambu Studio need profile files passed via `extra_args`.
- The CAD kernel is slow: `check` ≈ 10 s, `build` 15–40 s; avoid rebuilding in tight loops, use
  `--no-render --no-spice` while iterating on geometry, and `piforge check` first.
