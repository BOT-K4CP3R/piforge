# money_counter — split-flap money counter `000000,00`

A physical split-flap display (like a departure board): 8 digit modules of 50 mm plus a fixed comma
after the sixth digit. A Raspberry Pi Zero 2 W receives the amount over WebSocket and **counts up** —
the higher digits follow the counter, the lower ones spin at full speed, and in the end everything
lands exactly on the amount (0 → 1234,56 in ≈ 2 s, +0,01 PLN in ≈ 0,4 s; see
[Speed vs accuracy](#speed-vs-accuracy)).

```sh
.venv/bin/piforge check projects/money_counter              # ≈ 15 s, writes nothing
.venv/bin/piforge build projects/money_counter --scenarios  # build/ + 9 twin scenarios (≈ 4 min)
.venv/bin/piforge serve projects/money_counter              # GUI: 3D, Twin, SPICE, print
.venv/bin/python -m pytest projects/money_counter/firmware/tests -q   # firmware tests, no Pi needed
```

## Files

| Path | What it is |
|---|---|
| `project.py` | device description: circuit, mechanics, twin, scenarios, SPICE, checks |
| `mc_circuit.py` | electronics (Pi, 4 × 74HCT595, 8 × ULN2003, 8 × 28BYJ-48, 8 × A3144, capacitors) |
| `mc_mech.py` | modules in a single closed housing (`SplitFlapHousing`), electronics inside, display faces for the GUI |
| `mc_twin.py` | twin devices (`SF1…SF8`, `FEED1`) and scenarios |
| `firmware/main.py`, `firmware/moneycounter/` | firmware (Python 3.11+), runs unmodified on the Pi and in the twin |
| `firmware/config.toml` | configuration (mode, URL, ports, speeds, Hall pins, offsets) |
| `firmware/tests/` | firmware unit tests (40 tests, no hardware) |
| `artwork/` | digit stickers: `flap_KK_front|back.svg|png`, sheets `sheet_front|back.svg`, `index.json` |
| `tools/export_artwork.py` | re-export of the stickers |

## Print list

Printer 220 × 220 mm (`generic`), PLA, 0.2 mm, 15 % infill, **no supports**. The STL/3MF files in
`build/parts/` are already rotated to the print position and sit on the bed (STEP is in the project
frame). `piforge print projects/money_counter` shows time/mass/cost per part.

| Part | Qty | Orientation (`print_rotation`) | Mass each | Notes |
|---|---:|---|---:|---|
| `flap` | **160** | (90, 0, 0) | 2.0 g | 20 per module; black PLA, then stickers |
| `spool` | 8 | (0, 90, 0) | 9.0 g | D-shaped socket for the shaft, pocket for a 6 × 3 mm magnet; **black** PLA |
| `spool_cap` | 8 | (0, −90, 0) | 1.7 g | second flange (2.0 mm), pressed onto the D-shaped spigot; black |
| `frame_left` | 8 | (0, −90, 0) | 16.1 g | Ø6 axle, flap stop; black |
| `frame_right` | 8 | (0, 90, 0) | 17.0 g | 28BYJ-48 mount, Hall sensor socket; black |
| `front_0` … `front_3` | 1+1+1+1 | (90, 0, 0) — face down | 58–67 g | front with windows, light tunnels and ribs; `front_2` has the comma pocket |
| `comma_inlay` | 1 | (90, 0, 0) | 0.2 g | 1.2 mm comma, **white**, glued into the `front_2` pocket (or multi-color print) |
| `splice` | 3 | (−90, 0, 0) | 4.5 g | front joint connector, from behind (4 × M3) |
| `side_left`, `side_right` | 1+1 | (0, ∓90, 0) — outer face down | 41.2 g | side walls with ribs |
| `floor_0` … `floor_3` | 1+1+1+1 | (0, 0, 0) | 32–86 g | floor: module foot screws, ULN2003 posts, ventilation, cable-tie mounts, recesses for feet |
| `top_0` … `top_3` | 1+1+1+1 | (180, 0, 0) — outer face down | 25–68 g | top; on the underside, posts for the Pi Zero 2 W (`top_2`) and the perfboard (`top_1`) |
| `back_0` … `back_3` | 1+1+1+1 | (−90, 0, 0) — outer face down | 20–53 g | removable back cover: ventilation slots, DC socket (`back_1`), 2 hanging holes |
| `pillar` | 6 | (0, 90, 0) — on its side | 6.9 g | rear posts on the floor/top joints (in pairs, M3 × 25 + nut) |

Total ≈ 1.68 kg of PLA (black; only the comma is white). All parts fit on a 220 × 220 × 250 mm bed and
print **without supports** (report: 0 ERROR, 0 WARNING; only INFO about short bridges).

The housing (632 × 143 × 110 mm) is closed: from the front you see only the black front panel with 8
windows of 36 × 62.4 mm and the comma. Each window is 2 mm smaller on every side than the visible
area of the flaps (more than the 1.5 mm by which the stops overlap the flap edges), and a black tunnel
behind each window hides the spools, frames and motors — `check:housing` verifies this with ray casts
(`HOUSING.WINDOW_CLEAN`). The front stands 1.5 mm ahead of the arc swept by a falling flap
(`HOUSING.SWING_CLEAR`), so the flaps no longer protrude in front of the front panel.

**Digit stickers** (`artwork/`): the front of flap `k` = the upper half of digit `k mod 10`, the back =
the lower half of digit `k+1`. The files are drawn the way they appear on the display — apply the back
after turning the flap over about the pin axis (pin edge up), without mirroring. `sheet_front.svg` and
`sheet_back.svg` are sheets for double-sided printing (flip along the long edge) on self-adhesive
film; cut along the cell lines.

## BOM (electronics and small parts)

| Qty | Ref. | Item |
|---:|---|---|
| 1 | U1 | Raspberry Pi Zero 2 W (+ microSD card ≥ 8 GB, Raspberry Pi OS Lite 64-bit) |
| 1 | PS1 | 5 V 5 A power supply, 5.5 × 2.1 mm plug |
| 1 | — | 5.5 × 2.1 mm panel-mount DC socket (DC-022B, Ø8 hole) |
| 4 | U2–U5 | 74**HCT**595 (DIP-16) + sockets (HCT, not HC: at 5 V it accepts 3.3 V from the Pi) |
| 8 | U11–U18 | ULN2003 driver board (from the 28BYJ-48 kit) |
| 8 | M1–M8 | 28BYJ-48 stepper motor, **5 V** |
| 8 | H1–H8 | A3144 Hall sensor (TO-92UA) |
| 8 | — | 6 × 3 mm neodymium magnet (N35) — in the spool pocket, CA glue |
| 1 | C1 | 470 µF / 16 V electrolytic (5 V rail) |
| 4 | C2–C5 | 100 nF ceramic (at the VCC of each 595) |
| 1 | — | 70 × 50 mm perfboard, gold-pin strips, 2 two-pin screw terminals (5 V/GND) |
| 8 | — | 6-core wire (IN1–IN4, +5 V, GND) ~30–60 cm to each ULN2003 board |
| 8 | — | 3-core wire (5 V, GND, OUT) to each Hall sensor |
| 32 | — | M3 × 10 (module frame feet from the underside of the floor, heads in countersinks) |
| 32 + 12 + 12 | — | M3 × 10 (floor/top → front ribs; floor/top → side-wall ribs; floor/top → posts) |
| 12 | — | M3 × 12 (`splice` connectors → front joint ribs, from behind) |
| 6 + 6 | — | M3 × 25 + M3 nut (post pairs on the floor/top joints) |
| 6 + 6 | — | Ø3 × 8 pins (steel ISO 8734 or a piece of 3 mm filament) — front and post joints |
| 37 | — | M3 × 8 back cover (into ribs, posts and floor/top lugs) |
| 32 / 4 / 4 | — | M3 × 8 ULN2003 boards on the floor posts; M2.5 × 5 Pi; M2 × 6 perfboard |
| 8 | — | Ø12 rubber feet (recesses in the floor); for hanging: 2 screws with a head ≤ Ø8 (holes in the cover) |
| 16 | — | screws for mounting the 28BYJ-48 (Ø4.2 mm ears) to `frame_right` |

Circuit-derived list: `build/elec/bom.md` / `bom.csv`.

## Wiring

Full connection list: `build/elec/wiring.md` (and `wiring.png`), pinout: `build/elec/pinout.md`,
`config.txt` overlay: `build/elec/config.txt` (`dtparam=spi=on`).

| Pi Zero 2 W | Goes to |
|---|---|
| pin 2/4 (5 V), pin 6 (GND) | 5 V/GND rail on the perfboard (from the DC socket). Do **not** connect micro-USB at the same time |
| GPIO10 / MOSI (pin 19) | SER (14) of the first 74HCT595 (U2) |
| GPIO11 / SCLK (pin 23) | SRCLK (11) of all 595s |
| GPIO8 / CE0 (pin 24) | RCLK (12) of all 595s (latch after each transfer) |
| — | OE (13) → GND, SRCLR (10) → 5 V, QH' (9) → SER of the next chip |
| GPIO 4, 5, 6, 13, 16, 19, 20, 21 | OUT of Hall sensors H1…H8 (from the left), internal pull-up |

Module `i` (0 = leftmost, hundreds of thousands of PLN) = motor `M{i+1}`, board `U{11+i}`, sensor
`H{i+1}`. Inputs IN1…IN4 of its ULN2003 are chain outputs `4i…4i+3`: chip `U{2+i//2}`, outputs QA–QD
(even modules) or QE–QH (odd modules). ULN2003 boards: `+` to 5 V (this is also the COM of the
flyback diodes — they protect against overvoltage from the coils), `−` to GND. A3144 sensors: VCC 5 V,
GND, OUT to GPIO (open collector, so the GPIO never sees 5 V).

**Power:** Pi ≈ 0.35 A + 8 motors × 0.2 A (2 coils × 100 mA in half-step) ≈ 2.0 A peak out of 5 A —
budget in `build/elec/power.json`, voltage drop when all motors start in
`build/sim/eight_motors_start.png` (min. 4.86 V > the Pi's 4.63 V threshold).

## Assembly

1. Print the parts; clean the flap pin holes (Ø2.55) and the spool's D-shaped socket (fit it to the
   28BYJ-48 shaft).
2. Glue the magnet into the `spool` pocket with the **south pole facing out** (the A3144 responds only
   to the south pole — check with the sensor connected before gluing). Press the A3144 into the
   `frame_right` socket (flat side with the marking towards the spool), route the wires to the rear.
3. Apply the stickers to the flaps (`artwork/`), insert 20 flaps in order (0…9, 0…9) between `spool`
   and `spool_cap`, assemble the spool.
4. Screw the 28BYJ-48 to `frame_right`, slide the spool onto the shaft, fit `frame_left` (Ø6 axle).
5. **Front:** lay `front_0…3` face down on the table, insert Ø3 pins into the joints, screw `splice`
   on from behind (4 × M3 × 12). Glue the white `comma_inlay` into the comma pocket (`front_2`).
6. **Floor:** stand `floor_0…3` on the front (the floor rests against the back wall of the front and
   goes under the lower rib), screw M3 × 10 into the rib from below. On the floor joints: `pillar`
   pairs with pins, bolted together with M3 × 25 + nut (ball-end key), M3 × 10 from below through the
   floor.
7. Insert the modules in order (window above window) and screw M3 × 10 into the frame feet from the
   underside of the floor. ULN2003 boards on the floor posts behind each module (M3 × 8), motor plug,
   sensor wires and 6-core wires along the channel behind the boards (cable-tie mounts).
8. **Side walls** `side_left/right`: screw into the lower ribs from below through the floor.
9. **Top:** screw the Pi Zero 2 W (M2.5, ports to the rear) and the perfboard with 4 × 74HCT595 +
   C1…C5 (M2) to the underside of `top_2` / `top_1`, connect the cables, fit `top_0…3` and screw from
   above (front, side walls, posts).
10. **Back cover** `back_0…3` (DC socket in `back_1`, cable to the perfboard): 37 × M3 × 8. It comes off
    for service — the electronics stay in the housing.

## First run on the Pi

```sh
sudo raspi-config nonint do_spi 0                 # SPI0 enabled
sudo apt install python3-websockets python3-rpi-lgpio   # (or: pip install websockets RPi.GPIO)
scp -r projects/money_counter/firmware pi@licznik.local:/home/pi/money_counter
python3 /home/pi/money_counter/main.py            # log: HOME …, SHOW 000000,00
```

systemd service `/etc/systemd/system/money-counter.service`:

```ini
[Unit]
Description=money counter (split-flap)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/money_counter/main.py
Restart=always
User=pi
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now money-counter`, log: `journalctl -u money-counter -f`.

## Calibration

1. **Direction:** after startup each spool should turn so that the upper flaps move towards the
   viewer. If one turns the wrong way, reverse the motor plug / the IN1…IN4 order on that board.
2. **Zero offset (`[motion].offsets`):** during homing the module turns to the edge of the Hall
   sensor signal (the magnet arriving), then `offsets[i]` half-steps further. If after startup a
   module does not show a clean `0`, increase its offset (204.8 half-steps = one flap, ~10 = a fine
   correction), restart.
3. **Gear ratio (`steps_per_rev`):** nominally 4096 half-steps/revolution; a real 28BYJ-48 has ≈ 4076
   (gear ratio 63.68:1). Once per revolution the firmware checks the position on the Hall edge and
   corrects it; corrections up to `resync_tolerance` (24) are silent. If the log constantly shows
   `RESYNC m<i> err=-20` (or a similar constant value), set `steps_per_rev = 4076`.
4. **Speed:** `max_pps`, `start_pps`, `accel` — description and tuning in
   [Speed vs accuracy](#speed-vs-accuracy).

## Speed vs accuracy

**Result (twin, same host, from sending the amount to the `SHOW` log line):**

| Change | Before (constant 600 half-steps/s) | Now (ramp 450 → 850) |
|---|---:|---:|
| 0 → 1234,56 | 5.2 s | ≈ 1.9 s |
| 1234,56 → 1234,57 | 0.9 s | ≈ 0.4 s |
| 1234,57 → 999999,99 | 7.7 s | ≈ 2.8 s |
| startup (homing) | 1.5 s | ≈ 8 s (homing + self-test) |

**How it is faster:**

* **A trapezoidal ramp** on every move: start at `start_pps` (below the 28BYJ-48 pull-in frequency of
  ≈ 500 half-steps/s, so it starts immediately), accelerate at `accel`, cruise at `max_pps` (below the
  loss-of-synchronism limit of ≈ 950 for 5 V), and decelerate so that the last step is again at
  `start_pps` — the module stops exactly on the flap.
* **One loop for 8 motors:** each module has its own deadline for the next step, the loop wakes at
  most once per tick (`1/max_pps`) and sends **one** 32-bit SPI word with the steps of all modules
  whose deadline has arrived. A late step is never "caught up" with a burst — the module slows down
  and accelerates again (a system stall costs time, not steps).
* **Counting up:** the time = the travel of the digit that has to go furthest at full speed (the digits
  arrive together), the lower digits spin at full speed, the higher ones are commanded one flap-time
  earlier (they arrive together with the counter). `count_time_max` (3 s) is the upper bound.

**How it stays exact (closed loop on the Hall sensors):**

* **Self-test at startup** (`selftest = true`): after homing, each module does one revolution at
  `max_pps` and checks the position at the magnet (`SELFTEST m<i>: one turn at 850 …`). If it lost
  steps, the firmware lowers its speed by ×0.85 (`DERATE m<i> -> …`), re-homes and repeats the test —
  every module then runs only at a speed at which it has been verified not to lose steps.
* **Check at every magnet pass:** the position should then be a whole number of revolutions. An error
  ≤ `resync_tolerance` (e.g. a ratio of 4076 instead of 4096) is corrected silently; a larger one →
  correction + log `RESYNC m<i> err=+n` (n > 0 = the motor lost n half-steps) and a new ramp-up; two
  losses in a row → `DERATE`.
* **A large error** (> 1/10 of a revolution) or the counter passing the magnet position by > 1/10 of a
  revolution without a magnet (`missed Hall edge`) → the module **re-homes** (`-> re-home`,
  `HOME module i: re-homed`) and goes on to its digit.
* A module may not stop if it passed the expected magnet position without a magnet or was re-homed /
  slowed down — it first does a **verification revolution** (`VERIFY m<i>`) past the magnet.
  `SHOW …` appears only when all modules are stationary and verified.
* A dead sensor does not cause endless spinning: after 2 revolutions without a magnet, `FAILED` is
  logged and the position is assumed.

**Settings (`firmware/config.toml`):**

| Key | Default | Meaning |
|---|---:|---|
| `[motion] max_pps` | 850 | maximum speed (half-steps/s; 850 ≈ 4.15 flaps/s), ≈ 10 % below the stall limit |
| `[motion] start_pps` | 450 | start/stop speed (no ramp) |
| `[motion] accel` | 3000 | acceleration (half-steps/s²): 450 → 850 in 0.13 s |
| `[motion] resync_tolerance` | 24 | error at the magnet corrected without a log entry (half-steps) |
| `[motion] selftest` | true | verification revolution after homing (measures the safe speed) |
| `[display] count_time_min` / `count_time_max` | 0.25 / 3.0 | limits of the count-up time (s) |

Tuning: if the log shows `DERATE` after startup, the module cannot keep up with `max_pps` — check the
friction of the spool / flaps or reduce `max_pps` (e.g. 750). Without `DERATE` and without
`RESYNC … err=+` you can try a larger `max_pps` (the self-test will still slow down a module that
cannot cope). For a slower, "calmer" animation: a smaller `max_pps` or a larger `count_time_min`.

In the twin the motors have a **stall model** (`stall_model`, in `mc_twin.py`: `max_pps` 950,
`start_pps` 500, `max_accel` 5000 — an estimate for a 5 V 28BYJ-48 with a light spool): steps faster
than the motor can follow are lost (`M*.lost_steps`), and the `slip` input in the motor card simulates
a nudge to the rotor. The test `tests/project/test_money_counter.py::test_overspeed_is_caught_by_the_closed_loop`
sets the firmware to 1100 half-steps/s (above the stall limit): the self-test loses steps, `DERATE` →
935, and all amounts still land exactly.

### Option: 12 V 28BYJ-48 motors (faster)

The default project stays on 5 V. The **12 V** version of the same motor has a higher winding
resistance (≈ 130–200 Ω per phase instead of ≈ 50 Ω, depending on the manufacturer), so at a higher
voltage the current rises faster relative to the step period and the torque at high speed drops later.
Estimated (# src: est, to be measured with the self-test) the stall limit rises from ≈ 950 to
≈ 1300–1500 half-steps/s, i.e. **≈ +40 %**: 0 → 1234,56 in ≈ 1.4 s instead of ≈ 1.9 s, → 999999,99 in
≈ 2.0 s.

Changes:

* 8 × 28BYJ-48 **12 V** instead of 5 V; ULN2003 boards unchanged (ULN2003A: up to 50 V, 500 mA per
  channel); the boards' "+" pin (COM, flyback diodes) to **12 V**, not to 5 V.
* A **12 V ≥ 2 A** power supply (8 motors × 2 phases × ≈ 70–90 mA ≈ 1.4 A + converter) instead of
  5 V 5 A.
* A **12 V → 5 V ≥ 2 A** buck converter (e.g. an MP1584/LM2596 module) powers the Pi (5 V pins), the
  74HCT595s and the Hall sensors — these stay at 5 V (the ULN2003 inputs accept the 5 V logic of the
  595).
* `config.toml`: e.g. `max_pps = 1200`, `start_pps = 550`, `accel = 4000`; the startup self-test will
  slow down a module that cannot cope. In the twin: `MOTOR_LIMITS` in `mc_twin.py` (e.g. `max_pps`
  1400, `start_pps` 600). The circuit (`mc_circuit.py`) would have to be changed to a 12 V supply +
  converter — this has not been done in this project.

## WebSocket configuration (`firmware/config.toml`)

* **Client** (default): `mode = "client"`, `url = "ws://serwer:8765/"`. Each message is an amount:
  `{"amount": 1234.56}` (you can change the key in `json_path`, e.g. `data.total`) or just a number
  `1234.56` / `1234,56`. After a dropped connection it retries after 0.5 s, 1 s, 2 s … up to
  `reconnect_max_s`. The environment variable `MONEY_COUNTER_URL` overrides `url` (this is how the
  twin connects).
* **Server:** `mode = "server"`: WebSocket on `ws_port` (8765) — send `{"amount": x}`, the reply is
  `{"ok": true, "target": "001234,56"}`; HTTP on `http_port` (8080):

  ```sh
  curl -X POST http://licznik.local:8080/amount -d '{"amount": 1234.56}'
  curl http://licznik.local:8080/amount        # state: target, shown, homed, settled …
  ```
* An amount > `max_amount` (999 999,99) shows the maximum, log `CLAMP`. An invalid message (not a
  number, negative, NaN, bad JSON) → log `IGNORED …`, the display is unchanged.
* An increase: counting takes as long as the digit that goes furthest needs at full speed (≤ 9 flaps
  ≈ 2.3 s), within `count_time_min`…`count_time_max` (0.25…3 s); a decrease: the modules turn forward
  straight to the new digits. Once motion stops and positions are verified, the log shows
  `SHOW 001234,56` and the coils are switched off (`hold_ms`).

## Testing in the GUI (digital twin)

```sh
.venv/bin/piforge serve projects/money_counter
```

1. **Twin** tab → **Start**. The firmware starts unmodified on the virtual Pi: the 74HCT595 chain on
   SPI (`U2`), 8 motors (`M1…M8`), 8 split-flap modules (`SF1…SF8`) and the `FEED1` WebSocket server
   (the firmware connects to it through `MONEY_COUNTER_URL`).
2. **Display** section: the spools home and you see `000000,00`.
3. In the **Amount** field type e.g. `1234.56` → **Send** (or Enter, the buttons `+0.01 +1 +100
   +1000`, `random`). The display counts up and stops at `001234,56`; in 3D the spools rotate (joints
   `m{i}_spool` driven by `SF{i+1}.angle`). The **online** switch simulates a server outage — the
   firmware recovers on its own and catches up with the amount. **Fullscreen display** — the display
   alone, full screen.
4. In the device cards: register bits, motor positions/revolutions, coils ON/OFF, Hall sensor state.

Scenarios (`piforge twin test projects/money_counter`, all PASS; time 0 = firmware start, after ≈ 7–9 s
of homing and self-test the actions begin at 15 s):
`homing_to_zero`, `count_up_1234_56` (≤ 3 s, previously 5.2 s), `last_digit_1234_57` (≤ 0.9 s),
`big_jump_999999_99` (≤ 4 s, previously 7.7 s), `overflow_clamps`, `invalid_ignored` (negative
amount), `feed_reconnect`, `rapid_updates` (10 amounts in 2 s → exactly the last one), `slip_resync`
(the `M8` rotor pushed back by 300 half-steps during motion → `RESYNC m7 err=+300` → the digit is
still exact). Most scenarios also check that no motor lost a step (`M*.lost_steps == 0`).

## Checks and simulations in the report

* ERC: only `ERC.RAIL_SHORT` (INFO) — the power supply and the Pi's 5 V pins on one rail; this is
  intentional (the Pi is powered through the GPIO header), which is why you must not also supply power
  to micro-USB.
* `check:module` — flap kinematics, Hall sensor 1.9 mm from the magnet, collisions and a full spool
  revolution (one module; all 8 are identical). `check:housing` — dimensions, fit on the bed, falling
  flap clearance in front of the front panel, front view through the windows = flaps only.
  `check:consistency` — Hall pins / coil bits / offsets consistent between `config.toml`, the circuit
  and the twin.
* SPICE `coil_flyback`: an approximation of one ULN2003 channel + coil (a single NPN instead of the
  Darlington, a 70 Ω/150 mH relay coil instead of a 50 Ω motor phase — more energy, a conservative
  result): 5.8 V peak at the collector thanks to the diode to COM. `eight_motors_start`: the power path
  when 8 motors start.

## Limitations

* The module is 75 mm wide (motor next to the spool; 76 → 75 mm thanks to the thinner 2.0 mm
  `spool_cap` flange and a 0.6 mm gap), so the display is 632 mm, not 45 cm. It cannot get smaller
  without a different motor arrangement (gearing / motor behind the spool).
* At an angle (≈ 8° or more) you can see the edge of the spool flange or frame through the window —
  that is why they are black.
* In 3D the flaps are static (rest position); the spools rotate (under the cover). The digits on the
  closed housing are painted by the GUI on black plates `face_0…7` in the windows (`display_from` →
  `SF1…SF8`).
* The twin model has exactly 4096 steps/revolution; the real gear ratio and backlash are covered by
  calibration.
* The Hall sensor is the only position sensor: lost steps show up only when the magnet passes. A motor
  does not lose steps at the speed verified by the self-test, but **a nudge to a stationary spool** is
  detected and corrected only at its next magnet pass (≤ 1 revolution).
* Startup takes ≈ 7–9 s (homing + one verified revolution); `selftest = false` shortens it to ≈ 2 s
  at the cost of not measuring the safe speed.
* The twin's stall model (`stall_model`) is behavioral (estimated limits, no torque curve or
  resonances); real limits will be measured by the self-test on the Pi.
