"""Raw mechanical data (mm) of common modules used with Raspberry Pi devices. Consumed by
:mod:`piforge.mech.modules`; kernel-free.

Module frame (binding)
  * Modules WITH a PCB: PCB centred on the origin in XY, PCB bottom at z = 0, component side +Z.
  * Modules WITHOUT a PCB (switches, LEDs, motors…): body centred in XY; z = 0 is the seating
    plane named in the record (bottom of the body, or — for ``through`` panel parts — the outer
    panel face the flange rests on).
  * The panel ("front") side is +Z. ``window`` / ``extra_windows`` are the panel openings seen from
    +Z: ``("rect", (x, y), (w, h))`` or ``("circle", (x, y), (d,))`` WITHOUT clearance.

Panel mounting (``panel_mount``)
  behind   the module sits behind the panel; the plane z = ``front_height`` touches the panel's
           INNER face (features above it — lens domes, transducers, plungers — pass through the
           window). PCB modules are screwed to bosses on the panel; PCB-less modules with holes
           are screwed through clearance holes in the panel.
  through  the body passes through the panel hole and is held by a flange + nut; z = 0 is the
           OUTER panel face (flange underside), the body extends to −z.
  surface  mounted flat on an inner face (floor/wall) — PCB modules on standoffs.

Source legend for the ``# src:`` comments: ``DS`` = manufacturer datasheet named in the comment,
``common`` = dimensions published identically by several module vendors/listings (±0.5 mm),
``est`` = engineering estimate — measure your part before printing tight fits.
"""

from __future__ import annotations

import math
from typing import Any

PCB_T = 1.6  # src: common FR4 thickness of hobby modules (1.6 mm)


def B(name: str, x: float, y: float, z0: float, sx: float, sy: float, sz: float) -> tuple:
    """Box component given its bottom z (stored as centre + size)."""
    return (name, "box", (float(x), float(y), z0 + sz / 2.0), (float(sx), float(sy), float(sz)))


def C(name: str, x: float, y: float, z0: float, d: float, h: float) -> tuple:
    """Vertical cylinder component given its bottom z (stored as centre + (d, h))."""
    return (name, "cyl", (float(x), float(y), z0 + h / 2.0), (float(d), float(h)))


def _hdr(name: str, x: float, y: float, n: int, *, along: str = "x", below: bool = True,
         t: float = PCB_T) -> tuple:
    """2.54 mm single-row pin header (housing + pins, 8.5 mm) below or above the PCB."""
    length = 2.54 * n  # src: 2.54 mm (0.1 in) pin pitch
    sx, sy = (length, 2.54) if along == "x" else (2.54, length)
    return B(name, x, y, -8.5 if below else t, sx, sy, 8.5)  # src: common 8.5 mm male header


MODULE_DATA: dict[str, dict[str, Any]] = {}


def _add(rec: dict[str, Any]) -> None:
    MODULE_DATA[rec["key"]] = rec


# ============================================================================================
# Displays
# ============================================================================================
_add({
    "key": "ssd1306_096_i2c", "name": "0.96\" OLED 128×64 I2C (SSD1306)",
    "aliases": ("oled", "ssd1306", "oled_096"),
    "pcb": (27.3, 27.8, 1.2),  # src: common 27.3 × 27.8 mm module outline; thickness est
    "holes": ((-11.65, -11.9), (11.65, -11.9), (-11.65, 11.9), (11.65, 11.9)),  # src: est — Ø2 holes 2 mm from each edge
    "hole_d": 2.0,  # src: common M2 holes
    "components": (
        B("glass", 0, 0, 1.2, 26.7, 19.26, 1.45),  # src: DS UG-2864HSWEG01 panel 26.70 × 19.26 × 1.45; position est
        _hdr("header", 0, 12.4, 4, t=1.2),
    ),
    "window": ("rect", (0.0, 1.5), (21.74, 10.86)),  # src: DS UG-2864 active area 21.74 × 10.86; offset est
    "panel_mount": "behind", "front_height": 2.65,  # PCB 1.2 + glass 1.45
    "elec_key": "ssd1306_096_i2c",
    "source": "common 0.96\" I2C OLED module drawings; panel: WiseChip UG-2864HSWEG01 datasheet",
})
_add({
    "key": "sh1106_13_i2c", "name": "1.3\" OLED 128×64 I2C (SH1106)",
    "aliases": ("oled_13", "sh1106"),
    "pcb": (35.4, 33.5, 1.2),  # src: common 35.4 × 33.5 mm module outline; thickness est
    "holes": ((-15.2, -14.25), (15.2, -14.25), (-15.2, 14.25), (15.2, 14.25)),  # src: est — 2.5 mm from the edges
    "hole_d": 3.0,  # src: common Ø3 holes
    "components": (
        B("glass", 0, -0.5, 1.2, 34.5, 23.0, 1.5),  # src: est — 1.3" 128×64 panel outline
        _hdr("header", 0, 15.5, 4, t=1.2),
    ),
    "window": ("rect", (0.0, 1.0), (29.42, 14.7)),  # src: common SH1106 1.3" active area 29.42 × 14.70
    "panel_mount": "behind", "front_height": 2.7,
    "elec_key": None,
    "source": "common 1.3\" I2C OLED module listings (est)",
})
_add({
    "key": "lcd1602_i2c", "name": "LCD 16×2 (HD44780) with PCF8574 I2C backpack",
    "aliases": ("lcd1602", "lcd16x2"),
    "pcb": (80.0, 36.0, 1.6),  # src: DS 1602A module 80.0 × 36.0, PCB 1.6
    "holes": ((-37.5, -15.5), (37.5, -15.5), (-37.5, 15.5), (37.5, 15.5)),  # src: DS 1602A holes on 75.0 × 31.0
    "hole_d": 2.5,  # src: DS 1602A Ø2.5
    "components": (
        B("bezel", 0, 0, 1.6, 71.2, 24.2, 7.0),  # src: DS 1602A bezel 71.2 × 24.2; height est (module ≈ 8.6 + backlight)
        B("pcf8574_backpack", -12.0, 7.0, -12.0, 42.0, 19.5, 12.0),  # src: est — common backpack 41.5 × 19 incl. pins
        B("header_row", -12.0, 15.0, 1.6, 40.6, 2.5, 2.5),  # src: 16 pins × 2.54; soldered pin stubs, est
    ),
    "window": ("rect", (0.0, 0.0), (64.5, 16.4)),  # src: DS 1602A viewing area 64.5 × 16.4
    "panel_mount": "behind", "front_height": 8.6,
    "elec_key": "lcd1602_i2c",
    "source": "1602A LCD module datasheet (80 × 36 mm, V.A. 64.5 × 16.4); PCF8574 backpack est",
})
_add({
    "key": "ili9341_28_spi", "name": "2.8\" TFT 240×320 SPI (ILI9341, red PCB)",
    "aliases": ("ili9341", "tft28"),
    "pcb": (86.0, 50.0, 1.6),  # src: common MSP2807-style module 86.0 × 50.0
    "holes": ((-40.5, -22.5), (40.5, -22.5), (-40.5, 22.5), (40.5, 22.5)),  # src: est — Ø3 holes 2.5 mm from the edges
    "hole_d": 3.0,  # src: common Ø3 holes
    "components": (
        B("glass", 3.2, 0, 1.6, 69.2, 50.0, 3.9),  # src: common 2.8" panel 50.0 × 69.2; height incl. tape est
        B("header", -41.0, 0, -8.5, 2.54, 35.56, 8.5),  # src: 14-pin header on the short edge, est
        B("sd_socket", 20.0, 0, -2.0, 15.0, 15.0, 2.0),  # src: est — microSD socket on the back
    ),
    "window": ("rect", (4.0, 0.0), (57.6, 43.2)),  # src: common ILI9341 2.8" active area 43.2 × 57.6
    "panel_mount": "behind", "front_height": 5.5,
    "elec_key": None,
    "source": "common 2.8\" SPI TFT (MSP2807) drawing; panel AA from ILI9341 2.8\" panel listings",
})

# ============================================================================================
# Camera and sensors
# ============================================================================================
_add({
    "key": "pi_camera_v3", "name": "Raspberry Pi Camera Module 3",
    "aliases": ("camera", "picam", "camera_v3"),
    "pcb": (25.0, 24.0, 1.0),  # src: RPi Camera Module 3 product brief 25 × 24 × 11.5 mm; PCB est
    "holes": ((-10.5, -10.0), (10.5, -10.0), (-10.5, 2.5), (10.5, 2.5)),  # src: RPi camera drawing: 21 mm × 12.5 mm hole pattern, 2 mm from the sides
    "hole_d": 2.2,  # src: RPi camera drawing (M2)
    "components": (
        B("lens_base", 0, 2.5, 1.0, 8.5, 8.5, 5.0),  # src: est
        C("lens", 0, 2.5, 6.0, 7.5, 5.5),  # src: overall height 11.5 (product brief); Ø est
        B("fpc_connector", 0, -9.0, -2.5, 16.0, 5.5, 2.5),  # src: est — 22-pin FPC socket on the back
    ),
    "window": ("circle", (0.0, 2.5), (8.0,)),  # src: est — lens barrel + margin
    "panel_mount": "behind", "front_height": 11.5,
    "elec_key": "pi_camera_v3",
    "source": "Raspberry Pi Camera Module 3 product brief + camera board mechanical drawing",
})
_add({
    "key": "hcsr04", "name": "HC-SR04 ultrasonic distance sensor",
    "aliases": ("ultrasonic", "hc_sr04"),
    "pcb": (45.0, 20.0, PCB_T),  # src: DS Elecfreaks HC-SR04 45 × 20 × 15 mm
    "holes": ((-21.0, -8.0), (21.0, -8.0), (-21.0, 8.0), (21.0, 8.0)),  # src: est — corner holes
    "hole_d": 1.8,  # src: est
    "components": (
        C("transducer_t", -13.0, 0, PCB_T, 16.0, 12.0),  # src: DS Ø16 transducers; 26 mm pitch est
        C("transducer_r", 13.0, 0, PCB_T, 16.0, 12.0),
        B("crystal", 0, 6.0, PCB_T, 10.0, 4.0, 3.5),  # src: est HC-49S
        B("header", 0, -11.5, PCB_T, 10.16, 8.5, 2.5),  # src: est — right-angle pins past the edge
    ),
    "window": ("circle", (-13.0, 0.0), (16.0,)),
    "extra_windows": (("circle", (13.0, 0.0), (16.0,)),),
    "panel_mount": "behind", "front_height": 11.6,  # seat 10 mm above the PCB: transducers poke ~2 mm through
    "elec_key": "hcsr04",
    "source": "Elecfreaks HC-SR04 datasheet (45 × 20 × 15 mm, Ø16 transducers); holes est",
})
_add({
    "key": "pir_hcsr501", "name": "HC-SR501 PIR motion sensor",
    "aliases": ("pir", "hc_sr501"),
    "pcb": (32.3, 24.3, 1.2),  # src: common HC-SR501 32.3 × 24.3 mm
    "holes": ((-14.0, 0.0), (14.0, 0.0)),  # src: common 28 mm hole pitch
    "hole_d": 2.0,  # src: common
    "components": (
        C("fresnel_dome", 0, 0, 1.2, 23.0, 13.0),  # src: common Ø23 lens; height est
        B("pots_jumper", 0, 0, -8.0, 24.0, 16.0, 8.0),  # src: est — trimmers, jumper, pins on the back
    ),
    "window": ("circle", (0.0, 0.0), (23.0,)),
    "panel_mount": "behind", "front_height": 4.2,  # dome collar seats 3 mm above the PCB, dome pokes through
    "elec_key": "pir_hcsr501",
    "source": "common HC-SR501 module listings (32 × 24 mm PCB, Ø23 lens, 28 mm holes)",
})
_add({
    "key": "bme280_breakout", "name": "BME280 breakout (GY-BME280, 4-pin)",
    "aliases": ("bme280",),
    "pcb": (15.5, 11.5, PCB_T),  # src: est — common purple GY-BME280 ≈ 15 × 11.5 mm
    "holes": ((-4.5, 2.5),),  # src: est
    "hole_d": 3.0,  # src: est
    "components": (
        B("sensor", 3.5, 1.5, PCB_T, 2.5, 2.5, 0.93),  # src: DS Bosch BME280 LGA 2.5 × 2.5 × 0.93
        _hdr("header", 0, -4.2, 4),
    ),
    "window": None, "panel_mount": "surface", "front_height": PCB_T + 0.93,
    "elec_key": "bme280_breakout",
    "source": "Bosch BME280 datasheet (sensor); breakout outline est",
})
_add({
    "key": "dht22", "name": "DHT22 / AM2302 temperature-humidity sensor",
    "aliases": ("am2302",),
    "pcb": None,
    "holes": ((0.0, 10.0),),  # src: DS Aosong AM2302: Ø3 hole in the mounting ear
    "hole_d": 3.0,
    "components": (
        B("body", 0, -2.5, 0, 15.1, 20.0, 7.7),  # src: DS AM2302 25.1 × 15.1 × 7.7 incl. ear
        B("ear", 0, 10.0, 0, 15.1, 5.1, 1.6),  # src: DS AM2302; ear thickness est
        B("pins", 0, -12.5, -8.0, 10.16, 0.6, 8.0),  # src: 4 pins 2.54 mm pitch; length est
    ),
    "window": None, "panel_mount": "surface", "front_height": 7.7,
    "elec_key": "dht22",
    "source": "Aosong AM2302 (DHT22) datasheet",
})
_add({
    "key": "ds18b20_to92", "name": "DS18B20 temperature sensor (TO-92)",
    "aliases": ("ds18b20",),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("body", 0, 0, 0, 4.8, 4.8),  # src: JEDEC TO-92 body ≈ Ø4.8 × 4.8 (flat side not modelled)
        B("leads", 0, 0, -13.0, 3.04, 0.5, 13.0),  # src: TO-92 leads 1.27 mm pitch, 13 mm est
    ),
    "window": None, "panel_mount": "surface", "front_height": 4.8,
    "elec_key": "ds18b20",
    "source": "Maxim DS18B20 datasheet (TO-92 package)",
})
_add({
    "key": "ir_breakbeam_3mm", "name": "IR break-beam sensor half, 3 mm (emitter or receiver)",
    "aliases": ("ir_breakbeam", "breakbeam"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("housing", 0, 0, 0, 10.0, 6.0, 7.5),  # src: est — Adafruit #2167 style housing
        C("lens", 0, 0, 7.5, 3.0, 1.5),  # src: 3 mm LED lens
        B("leads", 0, 0, -10.0, 3.0, 0.5, 10.0),  # src: est
    ),
    "window": ("circle", (0.0, 0.0), (3.0,)),
    "panel_mount": "behind", "front_height": 7.5,
    "elec_key": "ir_breakbeam_rx",
    "source": "est — 3 mm IR break-beam pair (Adafruit #2167 style); one half, use two",
})
_add({
    "key": "tcrt5000_module", "name": "TCRT5000 reflective IR sensor module",
    "aliases": ("tcrt5000",),
    "pcb": (32.0, 14.0, PCB_T),  # src: common 32 × 14 mm module
    "holes": ((-9.5, 0.0),),  # src: est
    "hole_d": 3.0,  # src: common Ø3 hole
    "components": (
        B("sensor", 11.0, 0, PCB_T, 10.2, 5.8, 7.0),  # src: DS Vishay TCRT5000 10.2 × 5.8 × 7.0
        B("trimmer", 2.0, -3.0, PCB_T, 6.5, 6.5, 5.0),  # src: est
        B("lm393", 2.0, 4.0, PCB_T, 5.0, 4.0, 1.5),  # src: SOIC-8 est
        B("header", -14.7, 0, PCB_T, 2.54, 10.16, 8.5),  # src: 4 pins 2.54 mm, est
    ),
    "window": ("rect", (11.0, 0.0), (10.2, 5.8)),
    "panel_mount": "behind", "front_height": PCB_T + 7.0,
    "elec_key": "tcrt5000_module",
    "source": "Vishay TCRT5000 datasheet; module outline common listings (est)",
})
_add({
    "key": "hx711_module", "name": "HX711 load-cell amplifier module",
    "aliases": ("hx711",),
    "pcb": (29.0, 17.0, PCB_T),  # src: est — common green HX711 board ≈ 29 × 17 mm
    "holes": (), "hole_d": 0.0,
    "components": (
        B("hx711", 0, 0, PCB_T, 10.0, 6.0, 1.6),  # src: SOP-16 est
        _hdr("header_load", -13.0, 0, 6, along="y"),
        _hdr("header_mcu", 13.0, 0, 4, along="y"),
    ),
    "window": None, "panel_mount": "surface", "front_height": PCB_T + 1.6,
    "elec_key": "hx711_module",
    "source": "est — common HX711 module listings",
})
_add({
    "key": "load_cell_5kg_bar", "name": "Load cell 5 kg, aluminium bar (TAL220-style)",
    "aliases": ("load_cell", "loadcell"),
    "pcb": (80.0, 12.7, 12.7),  # src: DS SparkFun TAL220 80 × 12.7 × 12.7 (bar treated as the "PCB")
    "pcb_color": "#c0c4c8",
    "holes": ((-35.0, 0.0), (-20.0, 0.0), (20.0, 0.0), (35.0, 0.0)),  # src: DS TAL220: holes 5 and 20 mm from each end
    "hole_d": 4.2,  # src: DS TAL220 M4/M5 threads — Ø4.2 drawn (tap drill M5)
    "components": (
        B("gauge_cover", 0, 0, 12.7, 14.0, 10.0, 1.0),  # src: est — potting over the strain gauges
    ),
    "window": None, "panel_mount": "surface", "front_height": 13.7,
    "elec_key": "load_cell",
    "source": "SparkFun TAL220 load cell datasheet (80 × 12.7 × 12.7 mm)",
})

# ============================================================================================
# Buttons, switches, encoders, potentiometers, LEDs, buzzer
# ============================================================================================
_add({
    "key": "tact_6x6", "name": "Tactile switch 6×6×7 mm",
    "aliases": ("tact", "button_6x6"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("body", 0, 0, 0, 6.0, 6.0, 3.5),  # src: DS Omron B3F-1000 6 × 6 × 3.5 body
        C("plunger", 0, 0, 3.5, 3.5, 3.5),  # src: DS Omron B3F-1052 7 mm height, Ø3.5 plunger
        B("leads", 0, 0, -3.5, 6.5, 4.5, 3.5),  # src: DS Omron B3F 6.5 × 4.5 lead pitch
    ),
    "window": ("circle", (0.0, 0.0), (3.5,)),
    "panel_mount": "behind", "front_height": 3.5,  # body top against the panel, plunger pokes through
    "elec_key": "pushbutton",
    "source": "Omron B3F datasheet (6 × 6 mm, 7 mm tall variant)",
})
_add({
    "key": "pushbutton_12mm", "name": "Tactile switch 12×12 mm with round Ø11.6 cap",
    "aliases": ("button_12mm", "tact_12x12"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("body", 0, 0, 0, 12.0, 12.0, 3.5),  # src: DS Omron B3F-4055 12 × 12 × 3.5 body
        C("plunger", 0, 0, 3.5, 3.8, 0.8),  # src: est — stem below the cap
        C("cap", 0, 0, 4.3, 11.6, 5.0),  # src: est — common round cap for 12 × 12 switches
        B("leads", 0, 0, -3.5, 12.5, 5.0, 3.5),  # src: DS Omron B3F-4055 12.5 × 5.0 lead pitch
    ),
    "window": ("circle", (0.0, 0.0), (11.6,)),
    "panel_mount": "behind", "front_height": 3.5,
    "elec_key": "pushbutton_12mm",
    "source": "Omron B3F-4055 datasheet (12 × 12 mm); cap est",
})
_add({
    "key": "pushbutton_16mm_panel", "name": "Panel pushbutton 16 mm (metal, momentary)",
    "aliases": ("button_16mm", "panel_button"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("bezel", 0, 0, 0, 18.0, 2.0),  # src: est — typical 16 mm metal button, Ø18 head
        C("actuator", 0, 0, 2.0, 12.0, 1.5),  # src: est
        C("thread_body", 0, 0, -18.0, 16.0, 18.0),  # src: est — M16 thread body behind the panel
        B("terminals", 0, 0, -24.0, 8.0, 8.0, 6.0),  # src: est
    ),
    "window": ("circle", (0.0, 0.0), (16.0,)),
    "panel_mount": "through", "front_height": 3.5,
    "elec_key": "pushbutton",
    "source": "est — generic 16 mm metal panel pushbutton (Ø16 mounting hole)",
})
_add({
    "key": "rocker_switch_kcd1", "name": "Rocker switch KCD1 (snap-in)",
    "aliases": ("rocker", "kcd1"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("bezel", 0, 0, 0, 21.0, 15.0, 2.0),  # src: DS KCD1-101 bezel 21 × 15
        B("rocker", 0, 0, 2.0, 17.0, 11.0, 4.0),  # src: est
        B("body", 0, 0, -12.0, 18.6, 12.3, 12.0),  # src: DS KCD1-101 body (snap clips not modelled)
        B("terminals", 0, 0, -20.0, 10.0, 6.0, 8.0),  # src: est
    ),
    # src: DS KCD1-101 panel cutout 19.2 × 12.9 minus the default 2 × 0.3 mm cutout clearance
    "window": ("rect", (0.0, 0.0), (18.6, 12.3)),
    "panel_mount": "through", "front_height": 6.0,
    "elec_key": "switch_spst",
    "source": "KCD1-101 rocker switch datasheet (panel cutout 19.2 × 12.9 mm)",
})
_add({
    "key": "rotary_encoder_ky040", "name": "KY-040 rotary encoder module (EC11)",
    "aliases": ("ky040", "encoder"),
    "pcb": (26.0, 19.0, PCB_T),  # src: est — KY-040 board ≈ 26 × 19 mm (without pins)
    "holes": (), "hole_d": 0.0,  # held by the M7 bushing nut
    "components": (
        B("encoder", -2.0, 0, PCB_T, 12.4, 13.2, 6.5),  # src: DS Alps EC11 body 12.4 × 13.2, 6.5 tall
        C("bushing", -2.0, 0, PCB_T + 6.5, 7.0, 7.0),  # src: DS EC11 M7 × 0.75 bushing, 7 mm
        C("shaft", -2.0, 0, PCB_T + 13.5, 6.0, 13.0),  # src: DS EC11 Ø6 shaft (20 mm from the body)
        B("header", 15.0, 0, PCB_T, 8.0, 12.7, 2.5),  # src: est — right-angle pins past the edge
    ),
    "window": ("circle", (-2.0, 0.0), (7.0,)),
    "panel_mount": "behind", "front_height": PCB_T + 6.5,
    "elec_key": "rotary_encoder_ky040",
    "source": "Alps EC11 datasheet (encoder); KY-040 board outline est",
})
_add({
    "key": "pot_rv09", "name": "Potentiometer RV09 (9 mm, vertical, Ø6 shaft)",
    "aliases": ("pot", "potentiometer", "rv09"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("body", 0, 0, 0, 9.8, 11.0, 6.5),  # src: est — RV09 vertical 9.8 × 11 × 6.5
        C("shaft", 0, 0, 6.5, 6.0, 12.5),  # src: est — Ø6 shaft, 12.5 mm
        B("legs", 0, 0, -3.5, 7.0, 3.0, 3.5),  # src: est
    ),
    "window": ("circle", (0.0, 0.0), (6.0,)),
    "panel_mount": "behind", "front_height": 6.5,
    "elec_key": "potentiometer",
    "source": "est — common RV09 vertical potentiometer",
})
_add({
    "key": "pot_16mm", "name": "Potentiometer 16 mm with M7 bushing",
    "aliases": ("pot16", "rv16"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("body", 0, 0, 0, 16.0, 9.0),  # src: DS Alpha RV16AF Ø16 × 9
        C("bushing", 0, 0, 9.0, 7.0, 7.0),  # src: DS Alpha RV16AF M7 bushing 7 mm
        C("shaft", 0, 0, 16.0, 6.0, 15.0),  # src: DS Alpha RV16AF Ø6 shaft, 15 mm
        B("terminals", 0, -9.5, -6.0, 11.0, 3.0, 6.0),  # src: est
    ),
    "window": ("circle", (0.0, 0.0), (7.0,)),
    "panel_mount": "behind", "front_height": 9.0,
    "elec_key": "potentiometer",
    "source": "Alpha RV16AF-series potentiometer datasheet",
})
_add({
    "key": "led_5mm", "name": "LED 5 mm (T-1¾)",
    "aliases": ("led5", "led"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("flange", 0, 0, 0, 5.8, 1.0),  # src: common T-1¾: Ø5.8 flange
        C("body", 0, 0, 1.0, 5.0, 5.1),  # src: common Ø5.0 body
        C("dome", 0, 0, 6.1, 4.0, 2.5),  # src: 8.6 mm overall (dome approximated)
        B("leads", 0, 0, -6.0, 3.04, 0.5, 6.0),  # src: est — 2.54 mm lead spacing, trimmed to 6 mm
    ),
    "window": ("circle", (0.0, 0.0), (5.0,)),
    "panel_mount": "behind", "front_height": 1.0,  # flange against the panel
    "elec_key": "led",
    "source": "common 5 mm (T-1¾) LED package dimensions",
})
_add({
    "key": "led_3mm", "name": "LED 3 mm (T-1)",
    "aliases": ("led3",),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("flange", 0, 0, 0, 3.8, 1.0),  # src: common T-1: Ø3.8 flange
        C("body", 0, 0, 1.0, 3.0, 2.8),  # src: common Ø3.0 body
        C("dome", 0, 0, 3.8, 2.4, 1.5),  # src: 5.3 mm overall (dome approximated)
        B("leads", 0, 0, -6.0, 3.04, 0.5, 6.0),  # src: est — trimmed to 6 mm
    ),
    "window": ("circle", (0.0, 0.0), (3.0,)),
    "panel_mount": "behind", "front_height": 1.0,
    "elec_key": "led",
    "source": "common 3 mm (T-1) LED package dimensions",
})
_add({
    "key": "buzzer_12mm", "name": "Buzzer Ø12 × 9.5 mm (active, TMB12A05)",
    "aliases": ("buzzer",),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("body", 0, 0, 0, 12.0, 9.5),  # src: DS TMB12A05 Ø12 × 9.5
        B("pins", 0, 0, -6.0, 8.2, 0.6, 6.0),  # src: DS TMB12A05 7.6 mm pin pitch
    ),
    "window": ("circle", (0.0, 0.0), (3.0,)),  # src: est — sound outlet (Ø2) + margin
    "panel_mount": "behind", "front_height": 9.5,
    "elec_key": "buzzer_active",
    "source": "TMB12A05 active buzzer datasheet",
})

# ============================================================================================
# Motors, servos, drivers
# ============================================================================================
_add({
    "key": "sg90_servo", "name": "Micro servo SG90",
    "aliases": ("sg90",),
    "pcb": None,
    "holes": ((-13.9, 0.0), (13.9, 0.0)),  # src: est — tab holes 27.8 mm apart (common SG90 drawings)
    "hole_d": 2.0,
    "components": (
        B("body", 0, 0, 0, 22.8, 12.2, 22.7),  # src: common SG90 22.8 × 12.2 body (DS: 22.2 × 11.8 nominal)
        B("tabs", 0, 0, 15.9, 32.2, 12.2, 2.5),  # src: DS TowerPro SG90 tab span 32.2, tab underside 15.9
        C("gear_top", 5.5, 0, 22.7, 11.8, 4.0),  # src: DS SG90 shaft 5.9 from the end; est
        C("spline", 5.5, 0, 26.7, 4.8, 3.0),  # src: DS SG90 overall 31 incl. spline; Ø4.8 spline
    ),
    "window": None, "panel_mount": "surface", "front_height": 29.7,
    "elec_key": "sg90_servo",
    "source": "TowerPro SG90 datasheet drawing (tab span 32.2 mm)",
})
_add({
    "key": "mg996r_servo", "name": "Standard servo MG996R",
    "aliases": ("mg996r",),
    "pcb": None,
    "holes": ((-24.75, -5.0), (-24.75, 5.0), (24.75, -5.0), (24.75, 5.0)),  # src: est — 49.5 × 10 mm hole pattern
    "hole_d": 4.2,  # src: est
    "components": (
        B("body", 0, 0, 0, 40.7, 19.7, 37.0),  # src: DS TowerPro MG996R 40.7 × 19.7 (42.9 overall)
        B("tabs", 0, 0, 26.6, 54.0, 19.7, 2.5),  # src: DS MG996R tab span 54; height est
        C("gear_top", 10.2, 0, 37.0, 19.7, 2.6),  # src: est
        C("spline", 10.2, 0, 39.6, 5.9, 3.3),  # src: DS MG996R 42.9 overall; spline Ø est
    ),
    "window": None, "panel_mount": "surface", "front_height": 42.9,
    "elec_key": "mg996r_servo",
    "source": "TowerPro MG996R datasheet (40.7 × 19.7 × 42.9 mm, 54 mm tabs)",
})
_add({
    "key": "stepper_28byj48", "name": "Stepper motor 28BYJ-48",
    "aliases": ("28byj48", "byj48"),
    "pcb": None,
    "holes": ((-17.5, 0.0), (17.5, 0.0)),  # src: DS 28BYJ-48: Ø4.2 holes, 35 mm pitch
    "hole_d": 4.2,
    "components": (
        C("body", 0, 0, 0, 28.0, 19.0),  # src: DS 28BYJ-48 Ø28 × 19
        B("tabs", 0, 0, 18.2, 42.0, 7.0, 0.8),  # src: DS 42 mm tab span, 0.8 mm plate
        C("boss", 0, 8.0, 19.0, 9.0, 1.5),  # src: DS shaft 8 mm off-centre, Ø9 boss
        C("shaft", 0, 8.0, 20.5, 5.0, 10.0),  # src: DS Ø5 shaft (3 mm flats), 10 mm
        B("wire_cover", 0, -15.5, 0, 14.6, 6.0, 16.5),  # src: est — blue cable cover
    ),
    # the factory lead leaves the cable cover's end face (−y): 5 wires side by side, pin 1 … 5 =
    # blue D, pink C, yellow B, orange A, red COM (common 28BYJ-48 5 V drawings), 26 AWG (est),
    # 230 mm to a JST XH-5 plug (src: est — common 28BYJ-48 listings quote 23–25 cm; worst case kept)
    "anchors": tuple(
        (pin, num, ((k - 2) * 1.3, -18.5, 8.0), (0, -1, 0), "lead",
         {"color": col, "gauge_awg": 26, "lead_mm": 230.0, "cable": "28BYJ-48 lead", "label": f"{col} ({pin})"})
        for k, (pin, num, col) in enumerate((("D", "1", "blue"), ("C", "2", "pink"), ("B", "3", "yellow"),
                                             ("A", "4", "orange"), ("COM", "5", "red")))),
    "window": ("circle", (0.0, 8.0), (9.0,)),
    "panel_mount": "behind", "front_height": 19.0,  # mounting tabs against the panel, shaft through
    "elec_key": "stepper_28byj48",
    "source": "28BYJ-48 datasheet (Kiatronics/Mikroe drawing)",
})
_add({
    "key": "uln2003_board", "name": "ULN2003 stepper driver board",
    "aliases": ("uln2003",),
    "pcb": (35.0, 32.0, PCB_T),  # src: est — common ULN2003 board ≈ 35 × 32 mm
    "holes": ((-14.5, -13.0), (14.5, -13.0), (-14.5, 13.0), (14.5, 13.0)),  # src: est
    "hole_d": 3.0,
    "components": (
        B("uln2003", 0, 2.0, PCB_T, 19.3, 6.35, 4.0),  # src: DIP-16 body (TI ULN2003A PDIP) in socket est
        B("motor_socket", 0, -10.0, PCB_T, 12.4, 5.8, 7.0),  # src: JST XH 5-pin 12.4 × 5.75 × 7.0 (JST XH DS)
        B("leds", 0, 10.0, PCB_T, 14.0, 3.0, 5.0),  # src: est
        _hdr("header_in", -15.5, 2.0, 4, along="y", below=False),
        _hdr("header_pwr", 15.5, -2.0, 2, along="y", below=False),  # src: est — "5-12V" − + pins
    ),
    # wire anchors: IN1…IN4 / − + on the male headers (pin tips, DuPont from above), the motor's
    # JST XH plug in the vertical socket (pin 1 = blue D … pin 5 = red M+, 2.5 mm pitch — JST XH DS)
    "anchors": (
        *[(f"IN{k + 1}", str(k + 1), (-15.5, 2.0 + (1.5 - k) * 2.54, PCB_T + 8.5), (0, 0, 1), "dupont")
          for k in range(4)],
        ("-", "6", (15.5, -2.0 - 1.27, PCB_T + 8.5), (0, 0, 1), "dupont"),
        ("+", "5", (15.5, -2.0 + 1.27, PCB_T + 8.5), (0, 0, 1), "dupont"),
        *[(coil, num, ((k - 2) * 2.5, -10.0, PCB_T + 7.0), (0, 0, 1), "jst_xh")
          for k, (coil, num) in enumerate((("D", "10"), ("C", "9"), ("B", "8"), ("A", "7"), ("M+", "11")))],
    ),
    "window": None, "panel_mount": "surface", "front_height": PCB_T + 7.0,
    "elec_key": "uln2003_board",
    "source": "est — common ULN2003 stepper driver board; JST XH datasheet for the socket",
})
_add({
    "key": "nema17", "name": "Stepper motor NEMA 17 (42 × 40 mm)",
    "aliases": ("nema_17", "17hs4401"),
    "pcb": None,
    "holes": ((-15.5, -15.5), (15.5, -15.5), (-15.5, 15.5), (15.5, 15.5)),  # src: NEMA ICS 16: 31.0 mm hole pitch, M3
    "hole_d": 3.0,
    "components": (
        B("body", 0, 0, 0, 42.3, 42.3, 40.0),  # src: NEMA 17 42.3 mm square; 40 mm = 17HS4401 length
        C("boss", 0, 0, 40.0, 22.0, 2.0),  # src: NEMA 17 Ø22 × 2 pilot
        C("shaft", 0, 0, 42.0, 5.0, 24.0),  # src: Ø5 × 24 shaft (17HS4401 DS)
    ),
    "window": ("circle", (0.0, 0.0), (22.0,)),
    "panel_mount": "behind", "front_height": 40.0,
    "elec_key": None,
    "source": "NEMA ICS 16 frame size 17; 17HS4401 datasheet",
})
_add({
    "key": "tb6612_breakout", "name": "TB6612FNG dual motor driver breakout",
    "aliases": ("tb6612",),
    "pcb": (20.3, 20.3, PCB_T),  # src: est — 0.8 × 0.8 in breakout
    "holes": (), "hole_d": 0.0,
    "components": (
        B("tb6612", 0, 0, PCB_T, 7.8, 7.6, 1.6),  # src: DS Toshiba TB6612FNG SSOP24 body
        B("header_l", -8.89, 0, -8.5, 2.54, 20.32, 8.5),  # src: 8 pins 2.54 mm
        B("header_r", 8.89, 0, -8.5, 2.54, 20.32, 8.5),
    ),
    "window": None, "panel_mount": "surface", "front_height": PCB_T + 1.6,
    "elec_key": "tb6612_breakout",
    "source": "Toshiba TB6612FNG datasheet; breakout outline est",
})
_add({
    "key": "l298n_module", "name": "L298N dual H-bridge module",
    "aliases": ("l298n",),
    "pcb": (43.0, 43.0, PCB_T),  # src: common L298N module 43 × 43 × 27 mm
    "holes": ((-18.5, -18.5), (18.5, -18.5), (-18.5, 18.5), (18.5, 18.5)),  # src: common Ø3 holes on 37 mm pitch
    "hole_d": 3.0,
    "components": (
        B("heatsink", 0, 10.0, PCB_T, 23.0, 16.0, 25.4),  # src: est — 27 mm overall height (common)
        B("term_a", -17.5, 0, PCB_T, 7.5, 10.0, 10.0),  # src: est — 5.08 mm terminal blocks
        B("term_b", 17.5, 0, PCB_T, 7.5, 10.0, 10.0),
        B("term_pwr", -6.0, -17.5, PCB_T, 15.2, 7.5, 10.0),
        C("cap", 10.0, -10.0, PCB_T, 8.0, 11.0),  # src: est
    ),
    "window": None, "panel_mount": "surface", "front_height": 27.0,
    "elec_key": "l298n_module",
    "source": "common L298N module listings (43 × 43 × 27 mm, 37 mm holes)",
})
_add({
    "key": "relay_1ch_5v", "name": "Relay module 1-channel 5 V (SRD-05VDC)",
    "aliases": ("relay", "relay_module"),
    "pcb": (50.0, 26.0, PCB_T),  # src: common 1-ch relay module 50 × 26 × 18.5 mm
    "holes": ((-22.0, -10.0), (22.0, -10.0), (-22.0, 10.0), (22.0, 10.0)),  # src: est — Ø3 corner holes
    "hole_d": 3.0,
    "components": (
        B("relay", 2.0, 0, PCB_T, 19.0, 15.5, 15.3),  # src: DS Songle SRD-05VDC-SL-C 19.0 × 15.5 × 15.3
        B("terminal", 19.5, 0, PCB_T, 7.5, 15.2, 10.0),  # src: est — 3-way 5.08 mm terminal block
        B("header", -21.0, 0, PCB_T, 2.54, 7.62, 8.5),  # src: 3 pins 2.54 mm
    ),
    "window": None, "panel_mount": "surface", "front_height": PCB_T + 15.3,
    "elec_key": "relay_1ch_5v",
    "source": "Songle SRD-05VDC-SL-C datasheet; module outline common listings",
})

# ============================================================================================
# Fans, power
# ============================================================================================
_add({
    "key": "fan_30mm", "name": "Fan 30 × 30 × 10 mm",
    "aliases": ("fan30", "fan_3010"),
    "pcb": None,
    "holes": ((-12.0, -12.0), (12.0, -12.0), (-12.0, 12.0), (12.0, 12.0)),  # src: 3010 fans: 24 mm hole pitch (e.g. Sunon MF30100V)
    "hole_d": 3.2,
    "components": (B("frame", 0, 0, 0, 30.0, 30.0, 10.0),),  # src: 30 × 30 × 10
    "window": ("circle", (0.0, 0.0), (28.0,)),  # src: est — blade diameter
    "panel_mount": "behind", "front_height": 10.0,
    "elec_key": "fan_5v",
    "source": "standard 3010 fan dimensions (Sunon MF30100V datasheet)",
})
_add({
    "key": "fan_40mm", "name": "Fan 40 × 40 × 10 mm",
    "aliases": ("fan40", "fan_4010"),
    "pcb": None,
    "holes": ((-16.0, -16.0), (16.0, -16.0), (-16.0, 16.0), (16.0, 16.0)),  # src: 4010 fans: 32 mm hole pitch (e.g. Noctua NF-A4x10)
    "hole_d": 3.4,
    "components": (B("frame", 0, 0, 0, 40.0, 40.0, 10.0),),  # src: 40 × 40 × 10
    "window": ("circle", (0.0, 0.0), (38.0,)),  # src: est — blade diameter
    "panel_mount": "behind", "front_height": 10.0,
    "elec_key": "fan_5v",
    "source": "standard 4010 fan dimensions (Noctua NF-A4x10 datasheet)",
})
_add({
    "key": "lm2596_module", "name": "LM2596 buck converter module",
    "aliases": ("lm2596",),
    "pcb": (43.0, 21.0, PCB_T),  # src: common LM2596S module 43 × 21 × 14 mm
    "holes": ((-15.2, 6.6), (15.2, -6.6)),  # src: est — two diagonal Ø3 holes
    "hole_d": 3.0,
    "components": (
        C("cap_in", -13.0, -4.0, PCB_T, 8.0, 11.0),  # src: est — 14 mm overall (common)
        C("cap_out", 13.0, 4.0, PCB_T, 8.0, 11.0),
        B("inductor", 0, 3.0, PCB_T, 12.0, 12.0, 7.5),  # src: est
        B("trimmer", 2.0, -6.5, PCB_T, 9.5, 4.5, 10.0),  # src: est — 3296W trimmer
    ),
    "window": None, "panel_mount": "surface", "front_height": PCB_T + 11.0,
    "elec_key": "lm2596_module",
    "source": "common LM2596S module listings (43 × 21 × 14 mm); holes est",
})
_add({
    "key": "mp1584_module", "name": "MP1584EN mini buck converter",
    "aliases": ("mp1584",),
    "pcb": (22.0, 17.0, 1.2),  # src: common MP1584EN module 22 × 17 × 4 mm
    "holes": (), "hole_d": 0.0,
    "components": (
        B("inductor", -4.0, 0, 1.2, 6.0, 6.0, 3.0),  # src: est
        B("mp1584", 3.0, 3.0, 1.2, 3.0, 3.0, 1.0),  # src: SOIC-8E est
        B("trimmer", 5.0, -4.0, 1.2, 3.5, 3.5, 2.0),  # src: est
    ),
    "window": None, "panel_mount": "surface", "front_height": 4.2,
    "elec_key": None,
    "source": "common MP1584EN module listings (22 × 17 × 4 mm)",
})
_add({
    "key": "dc_jack_panel_55x21", "name": "DC barrel jack 5.5 × 2.1 mm, panel mount (DC-022B)",
    "aliases": ("dc_jack", "barrel_jack"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        C("shoulder", 0, 0, 0, 10.0, 2.0),  # src: est — DC-022B front shoulder Ø10
        C("thread", 0, 0, -9.0, 8.0, 9.0),  # src: est — M8 thread, 8 mm panel hole
        B("rear", 0, 0, -14.0, 9.0, 9.0, 5.0),  # src: est
        B("terminals", 0, 0, -20.0, 6.0, 3.0, 6.0),  # src: est
    ),
    # solder lugs at the rear (centre pin = V+, sleeve = GND; the switch lug is unused)
    "anchors": (("V+", "1", (-1.8, 0.0, -20.0), (0, 0, -1), "lug", {"label": "centre pin (+)"}),
                ("GND", "2", (1.8, 0.0, -20.0), (0, 0, -1), "lug", {"label": "sleeve (−)"})),
    "window": ("circle", (0.0, 0.0), (8.0,)),
    "panel_mount": "through", "front_height": 2.0,
    "elec_key": None,
    "source": "est — DC-022B panel-mount barrel jack (Ø8 mounting hole)",
})
_add({
    "key": "usb_c_breakout", "name": "USB-C receptacle breakout, vertical (panel window)",
    "aliases": ("usbc_breakout", "usb_c"),
    "pcb": (24.0, 10.0, 1.0),  # src: est — generic vertical USB-C breakout; check your part
    "holes": ((-10.0, 0.0), (10.0, 0.0)),  # src: est
    "hole_d": 2.2,
    "components": (
        B("receptacle", 0, 0, 1.0, 8.94, 3.26, 6.5),  # src: USB Type-C spec receptacle 8.94 × 3.26 (as NopSCADlib usb_C)
    ),
    "window": ("rect", (0.0, 0.0), (12.5, 7.0)),  # src: est — USB-C plug overmold 12.5 × 7 must pass the panel
    "panel_mount": "behind", "front_height": 7.5,
    "elec_key": None,
    "source": "est — generic vertical USB-C breakout; receptacle per USB Type-C spec",
})

# ============================================================================================
# Misc
# ============================================================================================
_add({
    "key": "mcp3008_dip16", "name": "MCP3008 ADC (PDIP-16)",
    "aliases": ("mcp3008",),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("body", 0, 0, 0.38, 19.05, 6.35, 3.3),  # src: DS Microchip MCP3008 PDIP-16 body 19.05 × 6.35
        B("leads", 0, 0, -3.3, 18.5, 8.1, 3.7),  # src: DS 7.62 mm row spacing, 3.3 mm leads
    ),
    "window": None, "panel_mount": "surface", "front_height": 3.68,
    "elec_key": "mcp3008",
    "source": "Microchip MCP3008 datasheet (PDIP-16 package)",
})
_add({
    "key": "ws2812_ring_12", "name": "WS2812B ring, 12 LEDs",
    "aliases": ("neopixel_ring_12", "ws2812b_ring_12"),
    "pcb": (37.0, 37.0, PCB_T),  # src: Adafruit NeoPixel Ring 12: Ø37 outer
    "pcb_round": True,
    "pcb_hole_d": 23.0,  # src: Adafruit NeoPixel Ring 12: Ø23 inner
    "holes": (), "hole_d": 0.0,
    "components": tuple(
        B(f"led{i}", 15.0 * math.cos(math.radians(30 * i)),
          15.0 * math.sin(math.radians(30 * i)), PCB_T, 5.0, 5.0, 1.6)
        for i in range(12)),  # src: DS WS2812B 5.0 × 5.0 × 1.6 on a 30 mm circle (est)
    "window": None, "panel_mount": "behind", "front_height": PCB_T + 1.6,
    "elec_key": "ws2812b_ring_12",
    "source": "Adafruit NeoPixel Ring 12 (Ø37/Ø23 mm); WS2812B datasheet",
})
_add({
    "key": "hall_a3144", "name": "Hall switch A3144 (TO-92 UA)",
    "aliases": ("a3144", "hall_switch"),
    "pcb": None, "holes": (), "hole_d": 0.0,
    "components": (
        B("body", 0, 0, 0, 4.06, 1.52, 3.0),  # src: DS Allegro A3141-4 UA package 4.06 × 1.52 × 3.0
        B("leads", 0, 0, -14.0, 2.97, 0.43, 14.0),  # src: DS UA leads 1.27 mm pitch, 0.43 thick; 14 mm (est, uncut)
    ),
    "window": None, "panel_mount": "surface", "front_height": 3.0,
    "elec_key": "hall_a3144",
    # lead tips (pin 1 VCC, 2 GND, 3 OUT seen from the branded face — DS UA pinout), wire soldered on
    "anchors": tuple((pin, str(k + 1), ((k - 1) * 1.27, 0.0, -14.0), (0, 0, -1), "solder")
                     for k, pin in enumerate(("VCC", "GND", "OUT"))),
    "source": "Allegro A3141/2/3/4 datasheet (UA package drawing)",
})
_add({
    "key": "perfboard_50x70", "name": "Perfboard 50 × 70 mm",
    "aliases": ("perfboard", "protoboard"),
    "pcb": (70.0, 50.0, PCB_T),  # src: common 5 × 7 cm prototype board
    "holes": ((-33.0, -23.0), (33.0, -23.0), (-33.0, 23.0), (33.0, 23.0)),  # src: est — Ø2 holes 2 mm from the edges
    "hole_d": 2.0,
    "components": (),
    "window": None, "panel_mount": "surface", "front_height": PCB_T,
    "elec_key": None,
    "source": "common 5 × 7 cm double-sided prototype board",
})
