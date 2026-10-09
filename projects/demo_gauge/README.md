# demo_gauge: a desk climate gauge

This example project uses every PiForge subsystem. A Raspberry Pi 4B sits in a printed PETG
enclosure. A BME280 measures temperature, humidity and pressure. A 0.96" SSD1306 OLED in a lid
window shows the readings. An SG90 servo turns a printed needle over a printed 0–40 °C dial on the
lid. A push button cycles through four display modes, and a red status LED flashes on every
reading. A 30 mm 5 V intake fan runs above 28 °C. It is switched by an AO3400 low-side MOSFET with
a 1N5819 flyback diode.

```sh
piforge build projects/demo_gauge --scenarios   # ≈ 35 s: parts, renders, docs, SPICE, 4 twin scenarios
piforge serve projects/demo_gauge               # 3D view + interactive twin
```

In `serve`, open the **Twin** tab, press **Start** and move the BME280 *Temperature* input. The
needle turns in 3D. Above 28 °C the fan turns on: the `M2` card shows *Energised* and the fan
glows blue in the 3D view. **Press** on `SW1` changes the OLED page, and the console logs
`mode=1`, `mode=2` and so on.

## Files

| Path | What it is |
|---|---|
| `project.py` | circuit, enclosure layout, assembly, thermal cases, twin scenarios, SPICE benches |
| `parts.py` | lid with servo posts, grilled base, dial (seven-segment numerals), needle, module tweaks |
| `firmware/main.py` | gpiozero + `adafruit_bme280` + `adafruit_ssd1306` with a built-in 5×7 font; 2 Hz loop |

## Design

- **Enclosure** (`Enclosure`, PETG, prusa_mk4): 128 × 64 × 38 mm closed. The box is 36 mm longer
  than the Pi on −X. That compartment holds the servo, the fan and the sensor. All Pi edge ports
  except the SD card are cut out. To change the card, take the lid off.
- **Lid**: the OLED window and the 12 mm button are cut by `PanelItem`s. The 5 mm LED and the
  exhaust slots over the Ethernet jack are also generated. `parts.gauge_lid` adds two posts that
  hold the SG90 by its tabs. They put the tab tops 4.5 mm below the lid, so only the Ø11.8 gear
  turret passes through a Ø12.6 hole. It also adds two bosses for the M2 screws that hold the
  dial.
- **Dial / needle**: the needle node has a revolute joint about +Z, `driven_by` the servo twin
  device `M1.angle`. The dial maps 0 °C → +70° (left), 20 °C → 0° (up) and 40 °C → −70°. The
  firmware uses the same mapping. The dial is printed face up with a filament change at 1.2 mm,
  so the raised marks come out dark. The numerals are seven-segment strokes 0.9 mm wide, because
  font text has strokes thinner than 0.8 mm (`PRINT.THIN_WALL`).
- **Fan**: 30 mm fan on the front wall in the compartment, blowing in. The round opening has
  vertical grille bars that print upright. The enclosure cuts the opening from the
  `fan_30mm` panel item: its teardrop roof is flattened one wall thickness below the rim of the
  34 mm wall, and `parts.gauge_base` only adds the grille bars. Inlet slots on the end wall sit next to the BME280. Warm air leaves
  through slots high in the back wall and in the lid.
- **Electronics**: BME280 (0x76) and OLED (0x3C) are on I2C1. The servo signal is on GPIO18 and
  its power on 5 V. The button goes from GPIO17 to GND with the internal pull-up. The LED circuit
  is GPIO27 → 330 Ω → red LED. The fan circuit is GPIO22 → 100 Ω → AO3400 gate, with a 100 kΩ gate
  pull-down and a 1N5819 across the fan. PSU: the official 15 W USB-C supply. ERC is clean. The
  5 V rail budget is 208 mA typical and 870 mA worst case (servo stall), out of 2400 mA available.
- **Twin**: the devices come from the circuit (`U2` BME280, `U3` OLED, `M1` servo, `SW1`, `D1`).
  The fan has no twin model, so `M2` is a relay-type output on GPIO22: *Energised* means the
  MOSFET is driven.

## Scenarios (`piforge twin test`)

| Scenario | Checks |
|---|---|
| `needle_at_22C` | servo angle ≈ −7° (map(22 °C)) ± 3°, fan off, log `T=22.x … fan=0 mode=0` |
| `fan_on_above_28C` | T 22 → 31 °C: fan on within 1.5 s, needle ≈ −38.5° ± 3°, log `fan=1` |
| `button_cycles_mode` | two presses → log `mode=1`, then `mode=2` |
| `display_and_status_led` | OLED on with > 100 lit pixels; status LED flashes |

## SPICE

| Label | Bench | Result |
|---|---|---|
| `status_led` | led_driver, 330 Ω, red | 3.9 mA |
| `fan_switch` | mosfet_lowside, AO3400, `fan_5v` load, 1N5819 flyback | 100 mA on, drain peak 5.4 V at turn-off |
| `fan_switch_no_flyback` | the same without D2 (what-if) | drain peak 14.4 V: WARNING (below the AO3400's 30 V) |
| `i2c_bus` | i2c_rise_time, 1.8 kΩ, 100 pF | t_r 153 ns (limit 1000 ns) |
| `servo_stall` | power_path, 5.1 V, 0.15 Ω, 0.75 → 1.4 A | minimum 4.84 V (INFO: within 5 % of 4.63 V) |

The fan load is the bench's `fan_5v` model: a 3010 5 V BLDC fan seen from its wires (50 Ω →
100 mA, 2 mH, 100 nF driver input capacitance).

## Thermal (Pi 4 at 3.5 W typical, 25 °C room)

- **Fan off** (`thermal.json["fan off"]`, vents only): inside air 48 °C and SoC ≈ 79 °C, just under the 80 °C
  throttle point. That is why the fan exists.
- **Fan on** (`thermal.json["fan on"]`): inside air 27.4 °C and SoC ≈ 46 °C.

The BME280 sits inside the box in the intake stream. It reads box air: about +2.4 K over room
temperature with the fan running, and more with the fan off. For a precise room reading, mount it
outside or subtract an offset in `firmware/main.py`.

## Warnings in the build report

| Code | Why it is expected |
|---|---|
| `SPICE.FLYBACK_OVERVOLTAGE` (`spice:fan_switch_no_flyback`) | A deliberate what-if run of the fan switch **without** D2. The drain rings to ≈ 14 V at turn-off: unclamped (WARNING) but below the AO3400's 30 V rating (that would be an ERROR). The built design has D2 (bench `fan_switch`, 5.4 V). |

No other warnings.
