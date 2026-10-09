"""Raw Raspberry Pi board dimensions (mm) with their sources. Consumed by :mod:`piforge.mech.boards`.

Board frame: origin at the PCB's lower-left corner seen from the top with the 40-pin GPIO header
along the TOP long edge; x along the long edge, y along the short edge, z = 0 at the PCB bottom.

Source legend used in the ``# src:`` comments
  DWG       labelled dimension on the official Raspberry Pi Ltd mechanical drawing of that board:
            Pi 5      RP-008347-DS-1  https://datasheets.raspberrypi.com/rpi5/raspberry-pi-5-mechanical-drawing.pdf
            Pi 4 B    RP-008343-DS-1  https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-mechanical-drawing.pdf
            Pi 3 B+   RP-008337-DS-2  https://datasheets.raspberrypi.com/rpi3/raspberry-pi-3-b-plus-mechanical-drawing.pdf
            Zero 2 W  RP-008358-DS-1  https://datasheets.raspberrypi.com/rpizero2/raspberry-pi-zero-2-w-mechanical-drawing.pdf
  DWG-meas  measured on that (to-scale, vector) drawing but not labelled; ±0.2 mm
  NOP       NopSCADlib (github.com/nophead/NopSCADlib) vitamins/pcbs.scad board tables
            (RPI4, RPI3 = 3 B layout, RPI0 = Zero layout) and the connector modules in pcb.scad
  est       engineering estimate (typical part / cable catalogue values) — NOT verified on a drawing

Port record keys
  edge     "-x" (SD-card end), "+x" (USB/Ethernet end), "-y" (bottom long edge), "+y", "+z" (top)
  pos      edge ports: coordinate along the edge (x for ±y edges, y for ±x edges); "+z": (x, y)
  mouth    edge ports: how far the connector mouth sits OUTSIDE the board edge (negative = recessed)
  z        edge ports: mouth-centre height above the PCB top (or below the PCB bottom when
           side == "bottom"); "+z" ports: height of the part's top above the PCB top
  body     edge ports: (width along the edge, depth inwards from the mouth, height);
           "+z" ports: (size x, size y, height)
  opening  connector face outline (width along edge or x, height or y) without clearance
  plug     envelope of a typical mating plug / card / finger outside the mouth (w, h, depth)
Component records: (name, (x, y), (size x, size y, height), side "top" | "bottom").
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------------------------
# Plug envelopes shared by several boards (cable overmolds, from catalogue drawings) — all est.
# ---------------------------------------------------------------------------------------------
PLUG_USB_C = (12.5, 7.0, 30.0)  # src: est — typical USB-C overmold 12–12.5 × 6.5–7 mm
PLUG_MICRO_HDMI = (11.5, 7.5, 30.0)  # src: est — typical micro-HDMI (type D) overmold
PLUG_MINI_HDMI = (13.5, 7.5, 30.0)  # src: est — typical mini-HDMI (type C) overmold
PLUG_HDMI = (21.0, 11.5, 35.0)  # src: est — typical HDMI type A overmold
PLUG_MICRO_USB = (10.5, 6.5, 25.0)  # src: est — typical micro-USB B overmold
PLUG_USB_A_DUAL = (16.0, 17.0, 30.0)  # src: est — two stacked USB-A overmolds (~16 × 8.5 each)
PLUG_RJ45 = (16.0, 14.0, 35.0)  # src: est — RJ45 plug + boot incl. latch; boots 15–16 wide, = jack width 16
PLUG_AUDIO = (9.0, 9.0, 30.0)  # src: est — 3.5 mm TRRS plug overmold
PLUG_SD = (12.0, 3.0, 8.0)  # src: est — microSD 11 × 1 mm card sticking out + fingertip access
PLUG_HEADER = (51.0, 5.1, 14.0)  # src: est — 2×20 DuPont/IDC housings above the pins
PLUG_BUTTON = (6.0, 6.0, 10.0)  # src: est — fingertip / printed plunger above a tact button
PLUG_FFC_22 = (12.0, 1.0, 15.0)  # src: est — 22-pin 0.5 mm camera ribbon (11.5 × 0.3) leaving the edge

GPIO_HEADER_BODY = (50.8, 5.08, 8.5)  # src: 2 × 20 pins at 2.54 mm pitch; height DWG "Z=8.5" (Pi 4/3B+)

# =============================================================================================
# Raspberry Pi 4 Model B
# =============================================================================================
RPI4B: dict[str, Any] = {
    "key": "rpi4b",
    "name": "Raspberry Pi 4 Model B",
    "aliases": ("rpi4", "pi4", "pi4b", "4b", "raspberrypi4", "raspberrypi4b", "raspberrypi4modelb"),
    "source": "Raspberry Pi Ltd mechanical drawing RP-008343-DS-1 (raspberry-pi-4-mechanical-drawing.pdf);"
              " connector bodies cross-checked with NopSCADlib vitamins/pcbs.scad RPI4",
    "length": 85.0,  # src: DWG "85"
    "width": 56.0,  # src: DWG "56"
    "thickness": 1.4,  # src: NOP RPI4 (1.4); not on the drawing; Pi 5 DWG side view measures 1.39
    "corner_radius": 3.0,  # src: DWG "CORNER RADIUS = 3.0mm"
    "holes": ((3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)),  # src: DWG 3.5 / 58 / 49
    "hole_d": 2.7,  # src: DWG "2.7" (NOP uses 2.75)
    "pad_d": 6.0,  # src: DWG "6" (keep-out ring around each hole)
    "extra_holes": (),
    "bottom_clearance": 2.0,  # src: est — no bottom view on the DWG; through-hole leads/RJ45 pegs ≈ 2 mm (Pi 5 DWG side view: 2.0)
    "notes": "USB/Ethernet stand 3.0 mm proud of the PCB edge per the DWG geometry (NOP: 2.0).",
    "ports": (
        {"name": "power", "kind": "usb-c", "edge": "-y",
         "pos": 11.2,  # src: DWG 3.5 + 7.7
         "mouth": 1.3,  # src: DWG-meas 1.26 (NOP 1.6)
         "z": 1.6,  # src: DWG "Z=3.2" → centre at half height
         "body": (8.9, 7.4, 3.2),  # src: DWG-meas 8.7 × 7.4; NOP usb_C 8.94 × 7.35; DWG Z=3.2
         "opening": (8.94, 3.26),  # src: NOP usb_C shell w × h
         "plug": PLUG_USB_C},
        {"name": "hdmi0", "kind": "micro-hdmi", "edge": "-y",
         "pos": 26.0,  # src: DWG 11.2 + 14.8
         "mouth": 1.4,  # src: DWG-meas 1.43 at the flange (NOP 1.75)
         "z": 1.5,  # src: DWG "Z=3.0"
         "body": (7.2, 7.9, 3.0),  # src: DWG-meas 6.2 body / 7.2 flange × 7.9 deep; NOP depth 8.5
         "opening": (7.2, 3.0),  # src: DWG-meas flange width; DWG Z=3.0
         "plug": PLUG_MICRO_HDMI},
        {"name": "hdmi1", "kind": "micro-hdmi", "edge": "-y",
         "pos": 39.5,  # src: DWG 26.0 + 13.5
         "mouth": 1.4, "z": 1.5, "body": (7.2, 7.9, 3.0), "opening": (7.2, 3.0),  # src: as hdmi0
         "plug": PLUG_MICRO_HDMI},
        {"name": "audio", "kind": "audio-jack", "edge": "-y",
         "pos": 54.0,  # src: DWG 39.5 + 7 + 7.5
         "mouth": 2.5,  # src: DWG-meas 2.53 barrel tip (NOP 2.5)
         "z": 3.0,  # src: DWG "Z=6.0", barrel axis at half height (NOP)
         "body": (7.0, 15.0, 6.0),  # src: DWG-meas 7.0 × 12.5 body + 2.5 barrel; NOP jack 7 × 12 × 6
         "opening": (6.0, 6.0),  # src: barrel Ø6 (NOP d = 6; DWG-meas 6.0)
         "plug": PLUG_AUDIO},
        {"name": "usb2", "kind": "usb-a-dual", "edge": "+x",
         "pos": 9.0,  # src: DWG "9" (black USB 2.0 stack)
         "mouth": 3.0,  # src: DWG-meas 3.0 (NOP 2.0 — disagreement, larger value kept)
         "z": 8.0,  # src: DWG "Z=16.0"
         "body": (13.2, 17.5, 16.0),  # src: DWG-meas 13.15 × 17.5; NOP usb_Ax2 13.25 × 17 × 15.6
         "opening": (14.5, 16.0),  # src: DWG-meas incl. flange tabs; DWG Z=16.0
         "plug": PLUG_USB_A_DUAL},
        {"name": "usb3", "kind": "usb-a-dual", "edge": "+x",
         "pos": 27.0,  # src: DWG "27" (blue USB 3.0 stack)
         "mouth": 3.0, "z": 8.0, "body": (13.2, 17.5, 16.0), "opening": (14.5, 16.0),  # src: as usb2
         "plug": PLUG_USB_A_DUAL},
        {"name": "ethernet", "kind": "rj45", "edge": "+x",
         "pos": 45.75,  # src: DWG "45.75"
         "mouth": 3.0,  # src: DWG-meas 3.0 (NOP 2.0)
         "z": 6.75,  # src: DWG "Z=13.5"
         "body": (16.0, 21.4, 13.5),  # src: DWG-meas 15.5 × 21.4; NOP rj45 16 × 21 × 13.5
         "opening": (16.0, 13.5),  # src: NOP rj45 w × h
         "plug": PLUG_RJ45},
        {"name": "sdcard", "kind": "microsd", "edge": "-x", "side": "bottom",
         "pos": 28.0,  # src: NOP RPI4 uSD y = 28 (not dimensioned on the DWG)
         "mouth": -2.0,  # src: NOP socket x 7.75 − 11.5/2 → front 2.0 inside the edge
         "z": 0.65,  # src: NOP socket 1.28 thick → card centre below the PCB
         "body": (12.0, 11.5, 1.3),  # src: NOP uSD [12, 11.5, 1.28]
         "opening": (12.0, 1.3),  # src: NOP
         "plug": PLUG_SD},
        {"name": "gpio", "kind": "header", "edge": "+z",
         "pos": (32.5, 52.5),  # src: DWG 3.5 + 29, on the top hole line
         "z": 8.5,  # src: DWG "Z=8.5"
         "body": GPIO_HEADER_BODY, "opening": (51.0, 5.1), "plug": PLUG_HEADER},
    ),
    "components": (
        ("soc", (29.25, 32.5), (15.0, 15.0, 2.4), "top"),  # src: DWG 3.5 + 25.75, 32.5, "Z=2.4"; DWG-meas 14.9 sq (NOP 14)
        ("ram", (44.6, 32.5), (11.4, 14.9, 1.0), "top"),  # src: DWG-meas outline; height est
        ("usb_controller", (59.2, 24.1), (8.0, 8.0, 1.0), "top"),  # src: DWG-meas outline; height est
        ("wifi_shield", (12.0, 42.5), (10.8, 13.0, 1.6), "top"),  # src: DWG-meas outline; height est
        ("csi", (47.0, 11.5), (4.0, 22.4, 5.5), "top"),  # src: DWG "7" (slot at 46.5), "11.5", "Z=5.5"; DWG-meas outline 45.0–49.0
        ("dsi", (3.5, 28.0), (4.0, 22.4, 5.5), "top"),  # src: DWG "4" (slot), 3.5 + 24.5, "Z=5.5"; DWG-meas outline 1.5–5.4
        ("poe_header", (61.5, 46.36), (5.0, 5.0, 8.5), "top"),  # src: DWG 52.5 − 6.14, DWG-meas x; height est (= GPIO)
    ),
}

# =============================================================================================
# Raspberry Pi 5 — not in NopSCADlib; second opinion: anchorscad models/cases/rpi/rpi5_outline.py
# =============================================================================================
RPI5: dict[str, Any] = {
    "key": "rpi5",
    "name": "Raspberry Pi 5",
    "aliases": ("pi5", "5", "raspberrypi5", "rpi5b", "pi5b"),
    "source": "Raspberry Pi Ltd mechanical drawing RP-008347-DS-1 (raspberry-pi-5-mechanical-drawing.pdf,"
              " top + front views); positions not on the drawing cross-checked with anchorscad rpi5_outline.py",
    "length": 85.0,  # src: DWG "85"
    "width": 56.0,  # src: DWG "56"
    "thickness": 1.4,  # src: DWG-meas side view 1.39 (anchorscad 1.5)
    "corner_radius": 3.0,  # src: DWG-meas ≈ 3.0
    "holes": ((3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)),  # src: DWG 3.5 / 58 / 49
    "hole_d": 2.7,  # src: DWG "ø2.7"
    "pad_d": 6.0,  # src: DWG-meas 5.8–6.0 (Pi 4 DWG labels 6)
    "extra_holes": ((3.5, 9.5, 3.0), (61.5, 46.5, 3.0)),  # src: DWG "ø3", each 6 from a mounting hole (Active Cooler push pins)
    "bottom_clearance": 2.0,  # src: DWG-meas side view: RJ45 pegs 2.0 below the PCB
    "notes": "Power button and status LED sit on the SD-card edge; RTC/UART/fan/PCIe connectors are internal.",
    "ports": (
        {"name": "power", "kind": "usb-c", "edge": "-y",
         "pos": 11.2,  # src: DWG "11.2"
         "mouth": 1.4,  # src: DWG-meas 1.39
         "z": 1.6,  # src: DWG side view "3.2"
         "body": (8.9, 7.5, 3.2),  # src: DWG-meas 8.85 × 7.5; DWG "3.2"
         "opening": (8.94, 3.26),  # src: NOP usb_C shell
         "plug": PLUG_USB_C},
        {"name": "hdmi0", "kind": "micro-hdmi", "edge": "-y",
         "pos": 25.8,  # src: DWG "25.8"
         "mouth": 1.8,  # src: DWG-meas 1.79 incl. flange lip
         "z": 1.7,  # src: DWG side view "3.4"
         "body": (7.2, 8.8, 3.4),  # src: DWG-meas 6.6 body / 7.2 lip × 8.8; DWG "3.4"
         "opening": (7.2, 3.4),  # src: DWG-meas lip width; DWG "3.4"
         "plug": PLUG_MICRO_HDMI},
        {"name": "hdmi1", "kind": "micro-hdmi", "edge": "-y",
         "pos": 39.2,  # src: DWG "39.2"
         "mouth": 1.8, "z": 1.7, "body": (7.2, 8.8, 3.4), "opening": (7.2, 3.4),  # src: as hdmi0
         "plug": PLUG_MICRO_HDMI},
        {"name": "usb3", "kind": "usb-a-dual", "edge": "+x",
         "pos": 29.1,  # src: DWG "29.1"; blue USB 3.0 stack stays in the middle (RPi forum t=395651)
         "mouth": 2.9,  # src: DWG-meas 2.9 at the flange
         "z": 8.0,  # src: DWG-meas side view 16.0
         "body": (13.2, 16.9, 16.0),  # src: DWG-meas 13.2 × (71.0 … 87.9)
         "opening": (14.8, 16.0),  # src: DWG-meas incl. flange tabs
         "plug": PLUG_USB_A_DUAL},
        {"name": "usb2", "kind": "usb-a-dual", "edge": "+x",
         "pos": 47.0,  # src: DWG "47"
         "mouth": 2.9, "z": 8.0, "body": (13.2, 16.9, 16.0), "opening": (14.8, 16.0),  # src: as usb3
         "plug": PLUG_USB_A_DUAL},
        {"name": "ethernet", "kind": "rj45", "edge": "+x",
         "pos": 10.2,  # src: DWG "10.2"
         "mouth": 3.0,  # src: DWG side view "3"
         "z": 7.0,  # src: DWG-meas side view: top 14.0 above the PCB
         "body": (16.0, 21.2, 14.0),  # src: DWG-meas 16.0 × (66.8 … 88.0) × 14.0
         "opening": (16.0, 14.0),  # src: DWG-meas
         "plug": PLUG_RJ45},
        {"name": "sdcard", "kind": "microsd", "edge": "-x", "side": "bottom",
         "pos": 28.1,  # src: DWG-meas card outline visible past the edge, y 22.5–33.8 (anchorscad 28.0)
         "mouth": -2.2,  # src: DWG-meas side view: socket x 2.2–13.7, card sticks out to −1.8
         "z": 0.7,  # src: DWG-meas side view: socket 1.45 below the PCB
         "body": (12.0, 11.5, 1.45),  # src: DWG-meas
         "opening": (12.0, 1.45),  # src: DWG-meas
         "plug": PLUG_SD},
        {"name": "power_button", "kind": "button", "edge": "-x",
         "pos": 18.4,  # src: DWG "18.4" (anchorscad power_sw 56 − 18.4 from the other edge)
         "mouth": 0.45,  # src: DWG side view "0.45" (plunger proud of the edge)
         "z": 1.6,  # src: DWG-meas side view: plunger 0.5–2.7 above the PCB
         "body": (4.6, 3.45, 3.5),  # src: DWG-meas body x 0.3–3.0, y 16.2–20.8, 3.5 tall
         "opening": (2.0, 2.2),  # src: DWG-meas plunger
         "plug": PLUG_BUTTON},
        {"name": "gpio", "kind": "header", "edge": "+z",
         "pos": (32.5, 52.5),  # src: DWG 3.5 + 29, on the top hole line
         "z": 8.5,  # src: DWG-meas side view ≈ 8.5–8.8 (Pi 4 DWG "Z=8.5")
         "body": GPIO_HEADER_BODY, "opening": (51.0, 5.1), "plug": PLUG_HEADER},
    ),
    "components": (
        ("soc", (33.1, 22.85), (17.1, 17.0, 2.4), "top"),  # src: DWG-meas outline; height est
        ("ram", (33.15, 39.0), (14.5, 10.0, 1.0), "top"),  # src: DWG-meas outline; height est
        ("rp1", (58.45, 34.95), (12.1, 12.1, 1.0), "top"),  # src: DWG-meas outline; height est
        ("wifi_shield", (12.4, 42.35), (10.6, 13.1, 1.6), "top"),  # src: DWG-meas outline; height est
        ("pcie_fpc", (2.8, 30.1), (3.1, 10.7, 4.1), "top"),  # src: DWG-meas outline; side view "4.1"
        ("mipi0", (48.75, 8.4), (3.1, 15.5, 4.1), "top"),  # src: DWG-meas outline; side view "4.1"
        ("mipi1", (55.0, 8.4), (3.0, 15.5, 4.1), "top"),  # src: DWG-meas outline; side view "4.1"
        ("uart", (32.35, 3.9), (5.5, 3.2, 4.4), "top"),  # src: DWG-meas outline; side view "4.4"
        ("fan_connector", (66.7, 52.0), (3.0, 6.0, 4.3), "top"),  # src: DWG-meas outline; height est (JST-SH vertical)
        ("status_led", (0.8, 13.3), (1.6, 3.4, 1.2), "top"),  # src: DWG "13.3"; DWG-meas outline; height est
        ("poe_header", (61.5, 9.5), (5.0, 5.0, 8.5), "top"),  # src: DWG "6" above the hole; height est (= GPIO)
        ("rtc_battery", (19.0, 4.7), (4.0, 2.9, 3.0), "top"),  # src: DWG-meas outline (between USB-C and HDMI0); height est (JST-SH)
        ("aux_conn_67_44", (67.5, 44.0), (4.3, 7.3, 4.4), "top"),  # src: DWG-meas outline (unlabelled part left of USB2); height est ≈ 4.4 (side view)
    ),
}

# =============================================================================================
# Raspberry Pi 3 Model B+
# =============================================================================================
RPI3BP: dict[str, Any] = {
    "key": "rpi3bp",
    "name": "Raspberry Pi 3 Model B+",
    "aliases": ("rpi3b+", "pi3b+", "pi3bp", "rpi3bplus", "3b+", "raspberrypi3b+", "raspberrypi3modelb+",
                "rpi3", "pi3"),
    "source": "Raspberry Pi Ltd mechanical drawing RP-008337-DS-2 (raspberry-pi-3-b-plus-mechanical-drawing.pdf);"
              " connector bodies cross-checked with NopSCADlib vitamins/pcbs.scad RPI3",
    "length": 85.0,  # src: DWG "85"
    "width": 56.0,  # src: DWG "56"
    "thickness": 1.4,  # src: NOP RPI3 (1.4); not on the drawing
    "corner_radius": 3.0,  # src: DWG "CORNER RADIUS = 3.0mm"
    "holes": ((3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)),  # src: DWG 3.5 / 58 / 49
    "hole_d": 2.75,  # src: DWG "2.75"
    "pad_d": 6.0,  # src: DWG "6"
    "extra_holes": (),
    "bottom_clearance": 2.0,  # src: est (as Pi 4: through-hole leads, RJ45 pegs; RAM is on the bottom)
    "notes": "All four USB-A ports are USB 2.0.",
    "ports": (
        {"name": "power", "kind": "micro-usb", "edge": "-y",
         "pos": 10.6,  # src: DWG "10.6" (NOP 10.6)
         "mouth": 1.3,  # src: DWG-meas 1.29 at the flange
         "z": 1.35,  # src: NOP usb_uA h 2.65 → centre
         "body": (7.5, 6.0, 2.65),  # src: DWG-meas 7.5 × 6.0; NOP usb_uA l 6, h 2.65
         "opening": (8.0, 3.0),  # src: NOP usb_uA flange 8 × 3 (DWG-meas 8.05)
         "plug": PLUG_MICRO_USB},
        {"name": "hdmi", "kind": "hdmi", "edge": "-y",
         "pos": 32.0,  # src: DWG "32"
         "mouth": 1.5,  # src: DWG-meas 1.5
         "z": 3.25,  # src: DWG "Z-Height=6.5"
         "body": (14.6, 12.1, 6.5),  # src: DWG-meas 14.55 × 12.1 (NOP depth 12); DWG 6.5
         "opening": (14.6, 6.5),  # src: DWG-meas
         "plug": PLUG_HDMI},
        {"name": "audio", "kind": "audio-jack", "edge": "-y",
         "pos": 53.5,  # src: DWG "53.5"
         "mouth": 2.6,  # src: DWG-meas 2.63 barrel tip (NOP 2.5)
         "z": 3.0,  # src: DWG "Z-Height=6"
         "body": (7.0, 15.0, 6.0),  # src: DWG-meas 7.0 × 12.45 + 2.6 barrel
         "opening": (6.0, 6.0),  # src: DWG-meas barrel Ø6 (NOP d = 6)
         "plug": PLUG_AUDIO},
        {"name": "usb_a1", "kind": "usb-a-dual", "edge": "+x",
         "pos": 29.0,  # src: DWG "29" (NOP 29)
         "mouth": 2.0,  # src: DWG-meas 2.04 (NOP 2.0)
         "z": 8.0,  # src: DWG "Z-Height=16.0"
         "body": (13.2, 17.8, 16.0),  # src: DWG-meas 13.15 × 17.8
         "opening": (14.5, 16.0),  # src: DWG-meas incl. flange tabs
         "plug": PLUG_USB_A_DUAL},
        {"name": "usb_a2", "kind": "usb-a-dual", "edge": "+x",
         "pos": 47.0,  # src: DWG "47" (NOP 47)
         "mouth": 2.0, "z": 8.0, "body": (13.2, 17.8, 16.0), "opening": (14.5, 16.0),  # src: as usb_a1
         "plug": PLUG_USB_A_DUAL},
        {"name": "ethernet", "kind": "rj45", "edge": "+x",
         "pos": 10.25,  # src: DWG "10.25" (NOP 10.25)
         "mouth": 2.0,  # src: DWG-meas 2.04 (NOP 2.0)
         "z": 6.75,  # src: DWG "Z-Height=13.5"
         "body": (16.0, 21.4, 13.5),  # src: DWG-meas 15.5 × 21.4; NOP rj45 16 × 21 × 13.5
         "opening": (16.0, 13.5),  # src: NOP rj45 w × h
         "plug": PLUG_RJ45},
        {"name": "sdcard", "kind": "microsd", "edge": "-x", "side": "bottom",
         "pos": 28.0,  # src: NOP RPI3 uSD y = 28 (not dimensioned on the DWG)
         "mouth": -2.0,  # src: NOP socket front 2.0 inside the edge
         "z": 0.65,  # src: NOP socket 1.28 thick
         "body": (12.0, 11.5, 1.3),  # src: NOP uSD [12, 11.5, 1.28]
         "opening": (12.0, 1.3),  # src: NOP
         "plug": PLUG_SD},
        {"name": "gpio", "kind": "header", "edge": "+z",
         "pos": (32.5, 52.5),  # src: DWG 3.5 + 29, on the top hole line
         "z": 8.5,  # src: DWG "Z-Height=8.5"
         "body": GPIO_HEADER_BODY, "opening": (51.0, 5.1), "plug": PLUG_HEADER},
    ),
    "components": (
        ("soc", (27.0, 31.4), (14.0, 14.0, 1.6), "top"),  # src: NOP chip [27, 56 − 24.6] 14 × 14 (DWG-meas 27.05, 31.35); height est (heat spreader)
        ("wifi_shield", (12.0, 42.95), (10.4, 13.7, 2.0), "top"),  # src: DWG-meas outline; height est
        ("csi", (45.0, 11.5), (4.0, 22.4, 5.5), "top"),  # src: DWG 32 + 13, "11.5", "Z-Height=5.5"; NOP flex [45, 11.5]
        ("dsi", (3.6, 28.0), (4.0, 22.4, 5.5), "top"),  # src: DWG "28", "Z-Height=5.5"; NOP flex [3.6, 28]
        ("poe_header", (61.5, 46.38), (5.0, 5.0, 8.5), "top"),  # src: DWG 85 − 23.5, 56 − 9.623; height est (= GPIO)
        ("ram", (27.0, 31.4), (12.0, 12.0, 1.0), "bottom"),  # src: est (LPDDR2 under the SoC on the 3 B/B+)
    ),
}

# =============================================================================================
# Raspberry Pi Zero 2 W
# =============================================================================================
RPIZERO2W: dict[str, Any] = {
    "key": "rpizero2w",
    "name": "Raspberry Pi Zero 2 W",
    "aliases": ("zero2w", "pizero2w", "rpizero2", "zero2", "raspberrypizero2w", "zero2wh", "rpizero2wh"),
    "source": "Raspberry Pi Ltd mechanical drawing RP-008358-DS-1 (raspberry-pi-zero-2-w-mechanical-drawing.pdf);"
              " connector bodies cross-checked with NopSCADlib vitamins/pcbs.scad RPI0",
    "length": 65.0,  # src: DWG "65"
    "width": 30.0,  # src: DWG "30"
    "thickness": 1.4,  # src: NOP RPI0 (1.4) — unverified for the Zero 2 W
    "corner_radius": 3.0,  # src: DWG-meas 3.0
    "holes": ((3.5, 3.5), (61.5, 3.5), (3.5, 26.5), (61.5, 26.5)),  # src: DWG 3.5 from each edge, "23"
    "hole_d": 2.75,  # src: DWG-meas 2.75 (NOP RPI0 2.75); not labelled
    "pad_d": 6.0,  # src: DWG-meas 6.0
    "extra_holes": (),
    "bottom_clearance": 1.5,  # src: est — SMD-only bottom; tails of a soldered GPIO header ≈ 1.5 mm
    "notes": "Modelled WITH a 2×20 GPIO header fitted (Zero 2 WH); a bare Zero 2 W is ~3.3 mm tall.",
    "ports": (
        {"name": "hdmi", "kind": "mini-hdmi", "edge": "-y",
         "pos": 12.4,  # src: DWG "12.4"
         "mouth": 0.6,  # src: DWG-meas 0.58
         "z": 1.6,  # src: NOP hdmi_mini outside height 3.2
         "body": (10.9, 7.5, 3.2),  # src: DWG-meas 10.9 × 7.5 (NOP depth 7.5, h 3.2)
         "opening": (10.9, 3.2),  # src: DWG-meas width; NOP height
         "plug": PLUG_MINI_HDMI},
        {"name": "usb", "kind": "micro-usb", "edge": "-y",
         "pos": 41.4,  # src: DWG "41.4" (USB OTG data)
         "mouth": 1.3,  # src: DWG-meas 1.3 at the flange
         "z": 1.35,  # src: NOP usb_uA h 2.65
         "body": (7.6, 6.0, 2.65),  # src: DWG-meas 7.55 × 6.0; NOP usb_uA
         "opening": (8.0, 3.0),  # src: DWG-meas flange 8.1; NOP flange 8 × 3
         "plug": PLUG_MICRO_USB},
        {"name": "power", "kind": "micro-usb", "edge": "-y",
         "pos": 54.0,  # src: DWG "54"
         "mouth": 1.3, "z": 1.35, "body": (7.6, 6.0, 2.65), "opening": (8.0, 3.0),  # src: as usb
         "plug": PLUG_MICRO_USB},
        {"name": "csi", "kind": "fpc", "edge": "+x",
         "pos": 15.0,  # src: DWG-meas 14.97 (NOP flat_flex y 15)
         "mouth": 0.0,  # src: DWG-meas connector x 61.6–65.0, flush with the edge
         "z": 0.6,  # src: NOP small_ff height 1.2
         "body": (16.2, 3.4, 1.2),  # src: DWG-meas 16.2 × 3.4 (NOP small_ff 17 × 4.1 × 1.2)
         "opening": (16.2, 1.2),  # src: DWG-meas
         "plug": PLUG_FFC_22},
        {"name": "sdcard", "kind": "microsd", "edge": "-x",
         "pos": 16.85,  # src: DWG-meas 10.85–22.85 (NOP RPI0 16.7)
         "mouth": -1.7,  # src: DWG-meas socket front 1.7 inside the edge (NOP 1.5)
         "z": 0.7,  # src: NOP uSD 1.4 thick, on TOP of the Zero
         "body": (12.0, 11.45, 1.4),  # src: DWG-meas 12.0 × 11.45; NOP [12, 11.5, 1.4]
         "opening": (12.0, 1.4),  # src: NOP
         "plug": PLUG_SD},
        {"name": "gpio", "kind": "header", "edge": "+z",
         "pos": (32.5, 26.5),  # src: DWG 3.5 + 29, on the top hole line
         "z": 8.5,  # src: est — header not fitted on a Zero 2 W; standard 8.5 mm male header assumed
         "body": GPIO_HEADER_BODY, "opening": (51.0, 5.1), "plug": PLUG_HEADER},
    ),
    "components": (
        ("rp3a0", (27.3, 14.0), (15.0, 15.0, 1.3), "top"),  # src: DWG-meas outline; height est
        ("wifi_shield", (43.8, 13.6), (12.2, 12.2, 1.5), "top"),  # src: DWG-meas outline; height est
    ),
}

BOARD_DATA: dict[str, dict[str, Any]] = {b["key"]: b for b in (RPI5, RPI4B, RPI3BP, RPIZERO2W)}
