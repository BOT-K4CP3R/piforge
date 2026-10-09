"""Passive parts: resistors, capacitors, potentiometers, LEDs and switches.

Sources:
- [KB-LED] Kingbright 5 mm through-hole LED datasheets (WP7113 series): VF typ 1.85-2.2 V red/yellow/
  green(GaP), IF(max) 25-30 mA; InGaN blue/white/true-green ~3.0-3.3 V. ``if_max_ma`` uses the common
  conservative 20 mA design limit.
- [KW12] Omron SS-5GL / KW12-3 micro switch pinout (COM, NO, NC).
- KiCad footprint names from the KiCad 8 standard libraries.
"""

from __future__ import annotations

from piforge.elec.model import REQUIRED, PartDef, Pin, PinType

P = PinType.PASSIVE

# LED colour -> forward voltage at ~10-20 mA and SPICE model key (Task 5 MODELS)
LED_COLORS = {
    "red": {"vf": 2.0, "spice": "led_red"},       # src: [KB-LED] red VF ~1.85-2.0 V typ (brief uses 2.0 V)
    "orange": {"vf": 2.0, "spice": "led_red"},    # src: [KB-LED] typical (no dedicated SPICE model)
    "yellow": {"vf": 2.1, "spice": "led_red"},    # src: [KB-LED] yellow VF 2.1 V typ
    "green": {"vf": 2.2, "spice": "led_green"},   # src: [KB-LED] GaP green VF 2.2 V typ (InGaN true-green ~3.0 V: pass vf=)
    "blue": {"vf": 3.1, "spice": "led_blue"},     # src: [KB-LED] InGaN blue 3.0-3.3 V
    "white": {"vf": 3.1, "spice": "led_white"},   # src: [KB-LED] InGaN white 3.0-3.3 V
    "ir": {"vf": 1.3, "spice": "led_ir"},         # src: typical 940 nm IR LED 1.2-1.5 V (unverified generic)
}

DEFS: list[PartDef] = [
    PartDef(
        key="resistor", name="Resistor (axial, 0.25 W)", category="passive",
        pins=(Pin("1", "1", P, required=True, aliases=("A",)), Pin("2", "2", P, required=True, aliases=("B",))),
        params={"value": REQUIRED, "power_w": 0.25, "tolerance": 0.05},
        ref_prefix="R", footprint="Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
        notes="value in ohms: c.add('resistor', value='4k7')",
    ),
    PartDef(
        key="capacitor", name="Capacitor", category="passive",
        pins=(Pin("1", "1", P, aliases=("+", "A")), Pin("2", "2", P, aliases=("-", "B"))),
        params={"value": REQUIRED, "voltage_rating": 16.0, "kind": "ceramic"},
        ref_prefix="C", footprint="Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
        notes="value in farads: c.add('capacitor', value='100n'); pin 1 is + for electrolytics",
    ),
    PartDef(
        key="potentiometer", name="Potentiometer (rotary, linear)", category="passive",
        pins=(Pin("CCW", "1", P, aliases=("1",)), Pin("W", "2", P, aliases=("WIPER", "2")),
              Pin("CW", "3", P, aliases=("3",))),
        params={"value": 10_000.0, "position": 0.5},
        ref_prefix="RV", footprint="Potentiometer_THT:Potentiometer_Bourns_PTV09A-1_Single_Vertical",
        notes="value = end-to-end resistance (Ω); position 0..1 = wiper position from CCW",
    ),
    PartDef(
        key="led", name="LED 5 mm", category="led",
        pins=(Pin("K", "1", PinType.PASSIVE, required=True, aliases=("cathode", "-")),
              Pin("A", "2", PinType.PASSIVE, required=True, aliases=("anode", "+"))),
        params={"color": "red", "vf": 2.0, "if_max_ma": 20.0, "spice": "led_red"},
        variants={"color": LED_COLORS},
        sim={"twin": "led", "spice": "led_red", "pins": {"pin": ("A", "K")}},
        mech="led_5mm", ref_prefix="D", footprint="LED_THT:LED_D5.0mm",
        datasheet="Kingbright WP7113 series (typical 5 mm LED)",
        notes="Always needs a series resistor: R = (V - vf) / I.",
    ),
    PartDef(
        key="rgb_led_cc", name="RGB LED 5 mm, common cathode", category="led",
        pins=(Pin("R", "1", P, aliases=("RED",)), Pin("K", "2", P, required=True, aliases=("COM", "cathode", "-")),
              Pin("G", "3", P, aliases=("GREEN",)), Pin("B", "4", P, aliases=("BLUE",))),
        # src: [KB-LED]-class RGB LEDs: red ~2.0 V, green/blue InGaN ~3.0-3.2 V, 20 mA per die
        params={"vf_r": 2.0, "vf_g": 3.0, "vf_b": 3.1, "if_max_ma": 20.0},
        sim={"twin": "rgb_led", "pins": {"red": "R", "green": "G", "blue": "B"}},
        ref_prefix="D", footprint="LED_THT:LED_D5.0mm-4_RGB",
        datasheet="typical 5 mm RGB LED (e.g. Kingbright L-154A4SURKQBDZGW class)",
        notes="Each colour needs its own resistor.",
    ),
    PartDef(
        key="pushbutton", name="Tactile pushbutton 6x6 mm", category="switch",
        pins=(Pin("A", "1", P, aliases=("1",)), Pin("B", "2", P, aliases=("2",))),
        sim={"twin": "button", "pins": {"pin": ("A", "B")}}, mech="tact_6x6", ref_prefix="SW",
        footprint="Button_Switch_THT:SW_PUSH_6mm", datasheet="generic 6x6 mm tact switch (e.g. Omron B3F)",
        notes="Momentary, normally open. The 4-leg body pairs legs internally; modelled as 2 terminals.",
    ),
    PartDef(
        key="pushbutton_12mm", name="Tactile pushbutton 12x12 mm", category="switch",
        pins=(Pin("A", "1", P, aliases=("1",)), Pin("B", "2", P, aliases=("2",))),
        sim={"twin": "button", "pins": {"pin": ("A", "B")}}, mech="pushbutton_12mm", ref_prefix="SW",
        footprint="Button_Switch_THT:SW_PUSH-12mm", datasheet="generic 12x12 mm tact switch (e.g. Omron B3F-4055)",
    ),
    PartDef(
        key="switch_spst", name="Toggle/slide switch SPST", category="switch",
        pins=(Pin("A", "1", P, aliases=("1",)), Pin("B", "2", P, aliases=("2",))),
        sim={"twin": "switch", "pins": {"pin": ("A", "B")}}, ref_prefix="SW",
        footprint="Button_Switch_THT:SW_Slide_1P2T_CK_OS102011MS2Q", datasheet="generic SPST switch",
    ),
    PartDef(
        key="limit_switch", name="Micro limit switch with lever (KW12-3)", category="switch",
        pins=(Pin("COM", "1", P, aliases=("C",)), Pin("NO", "2", P), Pin("NC", "3", P)),
        sim={"twin": "limit_switch", "pins": {"pin": ("NO", "NC", "COM")}}, ref_prefix="SW",
        footprint="Button_Switch_THT:SW_Micro_SPST_Omron_SS-5GL", datasheet="Omron SS-5GL / KW12-3 (COM/NO/NC)",
        notes="Wire COM to GND and NO (or NC) to a GPIO with a pull-up.",
    ),
]
