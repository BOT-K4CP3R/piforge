# Money counter — physical split-flap display (design spec)

Date: 2026-10-08 · Status: approved by the user (go ahead, build it and test it)

## Intent (user)
A real-time money counter showing `000000,00` on physical split-flap modules (digits 0–9) that flip like
the reference GIF. A Raspberry Pi receives the amount over WebSocket (or a similar feed) and updates live.
Decisions: physical flaps; ~50 mm digits; WS mode undecided → support both client and server; on change
the display **counts up** (visible counting). The user must also be able to simulate/test it in the
PiForge GUI.

## Hardware
- Raspberry Pi Zero 2 W (Wi-Fi). PSU 5 V 5 A (barrel jack → 5 V rail feeding Pi + motors).
- 8 split-flap digit modules + 1 fixed comma module between digit 6 and 7.
- Module: spool with **20 flaps** (0–9 twice; 18°/digit), 28BYJ-48 5 V stepper (half-step, 4096
  steps/rev nominal — `# src:` gear ratio ≈ 63.68:1 → 4076 real; calibration absorbs it), magnet in the
  spool + Hall sensor (A3144-type open-collector, pulled up to 3.3 V) for homing.
- Coils: 8 × 4 = 32 outputs via a chain of 4 × 74HC595 on SPI0 (MOSI GPIO10, SCLK GPIO11, latch
  GPIO8/CE0; 74HCT595 for valid 5 V logic from 3.3 V) → 8 × ULN2003 driver boards, one per motor
  (a ULN2003 has 7 channels; the standard 28BYJ-48 kit board; internal flyback diodes via COM). Coils de-energised when idle.
- Hall outputs → 8 GPIOs (GPIO4, 5, 6, 13, 16, 19, 20, 21 — final map by the allocator) with pull-ups.
- Digit window ~50 mm high; module pitch ~48 mm; total width ~45 cm; frame printed in segments that fit
  a 220×220 bed; front bezel with windows.

## Firmware (Python 3, runs unmodified on the Pi and in the twin)
- `config.toml`: mode = "client" (url, json path) | "server" (host, port; WS + HTTP POST /amount);
  max_amount 999999.99; count-up duration limits; per-module home offsets.
- Amount messages: JSON `{"amount": 1234.56}` or a bare number; invalid → logged, ignored.
- Display logic: target value → **count-up animation**: intermediate displayed values advance at a rate
  the modules can follow (module max ≈ 4–5 digits/s); large jumps spin the low digits continuously and
  land exactly. Decrease → modules go forward (wrap) straight to the new digits. Overflow → clamp
  999999.99 + log. Reconnect with backoff in client mode.
- Motion: one stepping thread at ~600 half-steps/s per motor; writes the 32-bit coil word to the 74HC595
  chain via spidev; homing at start (rotate until Hall edge, apply offset).
- Log line per settled value: `SHOW 001234,56`.

## Toolkit additions (PiForge)
1. **mech.splitflap**: parametric module generator — spool (with magnet pocket, flap pin holes),
   flap (two-half digit plates), axle/motor mount for 28BYJ-48, Hall sensor mount, module side frames,
   comma module, front bezel/backbone segments; digit artwork export (SVG/PNG halves for stickers) and an
   optional 2-colour inlay variant. Printability-checked.
2. **twin devices** (canonical type names, binding):
   - `shift_register_74hc595` on SPI (bus spi 0, cs 0): param `length` (chips); output `bits` (int).
   - `stepper_28byj48` gains param `coil_source: {"device": "SR1", "bits": [b0,b1,b2,b3]}` as an
     alternative to GPIO pins.
   - `splitflap`: params `stepper` (device id), `flaps` (20), `steps_per_rev` (4096), `home_angle`,
     `hall_pin` (BCM), `position` (0 = leftmost), `offset_steps`; outputs `angle` (deg), `digit` (0–9
     fully shown), `next_digit`, `flip` (0..1 progress of the falling flap), `hall` (bool). Drives the
     Hall GPIO low when the magnet is under the sensor.
   - `ws_feed`: runs a local WebSocket server inside the twin (port param) that pushes the input
     `amount` (float) as JSON to connected clients; inputs `amount`, `online` (bool); outputs `clients`.
     The firmware in twin mode connects to it via env `MONEY_COUNTER_URL` (set by the device config).
3. **GUI**: Twin tab shows a split-flap display widget (digits from `splitflap` devices ordered by
   `position`, comma after position 5, flip animation like the GIF driven by `digit/next_digit/flip`),
   plus an "Amount" input bound to `ws_feed`. In 3D, spools rotate via joints driven by `splitflap.angle`.
4. **elec library**: `sn74hc595`, `uln2003a` (DIP-16 chip) or reuse `uln2003_board`, `hall_a3144`,
   `psu_dc_5v5a`, `dc_jack_panel`; ERC/power budget covers 8 steppers.

## Project `projects/money_counter`
Circuit, mechanics (8+1 modules, frame, bezel, electronics box for Pi + 74HC595 board), firmware,
scenarios (homing; 0 → 1234,56 counts up and lands exactly; jump 1 → 999999,99 spins low digits;
overflow clamp; malformed message ignored; server down → reconnect), SPICE (ULN2003 + coil flyback),
power budget, README with print list and assembly guide.

## Tasks
- T-A mech.splitflap (+ artwork export) · T-B twin devices + elec parts · T-C GUI widget (contract above)
  — in parallel. T-D project (after A–C). Then review + full suite + present.
