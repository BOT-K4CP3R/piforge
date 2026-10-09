"""Actuators and power modules: servos, stepper + ULN2003, H-bridges, DC motor, relays, buzzers, fan, LM2596.

Switched load terminals (motor, coil, buzzer, fan) are PASSIVE pins on purpose: a GND-type pin would
mark a low-side-switched node as ground.

Sources:
- [SG90] TowerPro SG90 datasheet: operating voltage 4.8 V (~5 V); currents not published (unverified).
- [MG996R] TowerPro MG996R: 4.8-7.2 V, running 500-900 mA (6 V), stall 2.5 A (6 V).
- [28BYJ] Kiatronics 28BYJ-48 5 V: rated 5 V DC, 4 phases, 50 Ω ±7 % per phase, 1/64 reduction,
  stride 5.625°/64 (4096 half-steps per output revolution).
- [ULN2003A] TI ULN2003A: 2.7 kΩ series base resistor, VI(on) 2.4 V max @ 200 mA, 500 mA/ch,
  integrated clamp diodes (COM).
- [TB6612] Toshiba TB6612FNG (2007-06-30): VCC 2.7-5.5 V, VM 4.5-13.5 V (abs 15 V), VIN -0.2..6 V,
  VIH 0.7 VCC, VIL 0.3 VCC, ICC 1.1/1.8 mA @ 3 V and 1.5/2.2 mA @ 5.5 V, Iout 1.2 A avg / 3.2 A peak,
  1.0 A operating (VM >= 5 V), 200 kΩ input pull-downs.
- [L298] ST L298: VS <= 46 V (abs 50 V), VSS 4.5-7 V, ViH >= 2.3 V, ViL <= 1.5 V, VI abs -0.3..7 V,
  IS 13/22 (Vi=L) and 50/70 mA (Vi=H), ISS 24/36 mA, IO 2 A DC per channel (2.5 A rep., 3 A peak).
- [SRD] Songle SRD-05VDC-SL-C: 5 V coil, 71.4 mA, 70 Ω ±10 %, pull-in 75 % (3.75 V),
  drop-out 10 %, max 120 % (6 V); contacts 10 A 250 VAC / 10 A 28 VDC.
- [TMB12A05] LCSC C96093: active buzzer rated 5 V, 4-8 V, 30 mA.
- [LM2596] TI LM2596 (SNVS124G): 3 A output, VIN up to 40 V, adjustable 1.2-37 V, 5 V version
  efficiency 80 % (VIN 12 V, 3 A), standby IQ 80 µA.
"""

from __future__ import annotations

from piforge.elec.model import PartDef, Pin, PinType, Supply

PI, GN, IN, OUT, OD, P = (PinType.POWER_IN, PinType.GND, PinType.INPUT, PinType.OUTPUT, PinType.OPEN_DRAIN,
                          PinType.PASSIVE)
HDR = "Connector_PinHeader_2.54mm:PinHeader_1x{n:02d}_P2.54mm_Vertical"


def _servo(key: str, name: str, supply: Supply, mech: str, ds: str, notes: str) -> PartDef:
    return PartDef(
        key=key, name=name, category="motor",
        pins=(Pin("GND", "1", GN, required=True, aliases=("BROWN", "-")),
              Pin("V+", "2", PI, required=True, aliases=("VCC", "+", "PWR", "5V", "RED")),
              Pin("SIG", "3", IN, required=True, functions=("PWM_IN",), aliases=("PWM", "SIGNAL", "S", "ORANGE"))),
        supply=supply, params={"motor_pins": ("V+",)}, features=("servo",),
        sim={"twin": "servo", "pins": {"pin": "SIG"}}, mech=mech, ref_prefix="M",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", datasheet=ds, notes=notes,
    )


DEFS: list[PartDef] = [
    _servo("sg90_servo", "TowerPro SG90 micro servo",
           # src: [SG90] 4.8 V nominal; 6.0 V upper limit and 100/650 mA currents unverified (typical measurements)
           Supply(4.8, 6.0, 100.0, 650.0, pin="V+"), "sg90_servo",
           "http://www.ee.ic.ac.uk/pcheung/teaching/DE1_EE/stores/sg90_datasheet.pdf",
           "Power from 5 V (not 3V3); the 3.3 V signal is normally accepted."),
    _servo("mg996r_servo", "TowerPro MG996R metal-gear servo",
           Supply(4.8, 7.2, 500.0, 2500.0, pin="V+"),  # src: [MG996R] 4.8-7.2 V, running 500 mA+, stall 2.5 A
           "mg996r_servo", "https://www.electronicoscaldas.com/datasheet/MG996R_Tower-Pro.pdf",
           "Stall current 2.5 A: use a separate 5-6 V supply, common GND with the Pi."),
    PartDef(
        key="stepper_28byj48", name="28BYJ-48 5 V geared stepper motor", category="motor",
        pins=(Pin("D", "1", P, aliases=("BLUE",)), Pin("C", "2", P, aliases=("PINK",)),
              Pin("B", "3", P, aliases=("YELLOW",)), Pin("A", "4", P, aliases=("ORANGE",)),
              Pin("COM", "5", PI, required=True, aliases=("RED", "+"))),
        # src: [28BYJ] 5 V, 50 Ω/phase -> 100 mA per energised phase, 2 phases on = 200 mA; 4.5-5.5 V unverified
        supply=Supply(4.5, 5.5, 150.0, 200.0, pin="COM"),
        params={"loads": tuple(("COM", x, 50.0) for x in "ABCD"), "coils": tuple(("COM", x) for x in "ABCD"),
                "gear_ratio": 64, "half_steps_per_rev": 4096, "motor_pins": ("COM", "A", "B", "C", "D")},
        features=("motor", "inductive", "stepper"),
        sim={"twin": "stepper_28byj48", "pins": {"in1": "A:in", "in2": "B:in", "in3": "C:in", "in4": "D:in"}},
        mech="stepper_28byj48", ref_prefix="M", footprint="Connector_JST:JST_XH_B5B-XH-A_1x05_P2.50mm_Vertical",
        datasheet="https://components101.com/sites/default/files/component_datasheet/28byj48-step-motor-datasheet.pdf",
        notes="Drive through the ULN2003 board (IN1..IN4 = orange, yellow, pink, blue).",
    ),
    PartDef(
        key="uln2003_board", name="ULN2003 stepper driver board", category="driver",
        pins=tuple(Pin(f"IN{i}", str(i), IN, vih=2.4, v_max=30.0) for i in range(1, 5))  # src: [ULN2003A] VI(on) 2.4 V
        + (Pin("+", "5", PI, required=True, aliases=("VCC", "5V", "V+")), Pin("-", "6", GN, required=True, aliases=("GND",)))
        + tuple(Pin(x, str(6 + i), OD) for i, x in enumerate("ABCD", start=1))
        + (Pin("M+", "11", P, aliases=("MOTOR+", "RED")),),
        supply=Supply(5.0, 12.0, 6.0, 12.0, pin="+"),  # src: unverified (board listing 5-12 V; 4 LEDs ~3 mA each)
        params={
            "input_loads": {f"IN{i}": (2700.0, 1.4, "-") for i in range(1, 5)},  # src: [ULN2003A] 2.7 kΩ + 2 Vbe
            "load_outputs": ("A", "B", "C", "D"), "load_supply": "+",
            "passthrough": {"in": {"A": "IN1", "B": "IN2", "C": "IN3", "D": "IN4"}},
            "i_out_ma": 500.0,  # src: [ULN2003A] 500 mA per Darlington
        },
        ties=(("+", "M+"),), features=("driver", "clamped_outputs"), mech="uln2003_board", ref_prefix="U",
        footprint=HDR.format(n=6), datasheet="https://www.ti.com/lit/ds/symlink/uln2003a.pdf",
        notes="COM of the ULN2003A is tied to '+' on the board, so its clamp diodes protect the coils.",
    ),
]

_TB_IN = ("PWMA", "AIN2", "AIN1", "STBY", "BIN1", "BIN2", "PWMB")
DEFS += [
    PartDef(
        key="tb6612_breakout", name="TB6612FNG dual H-bridge breakout", category="driver",
        pins=(Pin("VM", "1", PI, required=True, aliases=("VMOT",)), Pin("VCC", "2", PI, required=True),
              Pin("GND", "3", GN, required=True), Pin("AO1", "4", OUT, aliases=("A01",)),
              Pin("AO2", "5", OUT, aliases=("A02",)), Pin("BO2", "6", OUT, aliases=("B02",)),
              Pin("BO1", "7", OUT, aliases=("B01",)), Pin("GND", "8", GN))
        + tuple(Pin(n, str(9 + i), IN, vih_ratio=0.7, vil_ratio=0.3, v_max=6.0) for i, n in enumerate(_TB_IN))  # src: [TB6612]
        + (Pin("GND", "16", GN),),
        supply=Supply(2.7, 5.5, 1.1, 2.2, pin="VCC"),  # src: [TB6612] VCC range, ICC
        params={
            "extra_supplies": {"VM": (4.5, 13.5)},  # src: [TB6612] operating range
            "pullups": {n: (200_000.0, 0.0) for n in _TB_IN},  # src: [TB6612] internal 200 kΩ pull-downs
            "load_outputs": ("AO1", "AO2", "BO1", "BO2"), "load_supply": "VM",
            "passthrough": {"in": {"AO1": "AIN1", "AO2": "AIN2", "BO1": "BIN1", "BO2": "BIN2"},
                            "pwm": {"AO1": "PWMA", "AO2": "PWMA", "BO1": "PWMB", "BO2": "PWMB"}},
            "i_out_ma": 1000.0,  # src: [TB6612] 1.0 A operating per channel (VM >= 5 V)
        },
        logic_from="VCC", features=("driver", "clamped_outputs"), mech="tb6612_breakout", ref_prefix="U",
        footprint=HDR.format(n=16), datasheet="https://cdn.sparkfun.com/datasheets/Robotics/TB6612FNG.pdf",
        notes="Power VCC from 3V3 so 3.3 V GPIOs meet VIH = 0.7 x VCC. STBY must be high to run.",
    ),
    PartDef(
        key="l298n_module", name="L298N dual H-bridge module", category="driver",
        pins=(Pin("12V", "1", PI, required=True, aliases=("VS", "VIN", "+12V", "VMS")),
              Pin("GND", "2", GN, required=True), Pin("5V", "3", PI, aliases=("VSS", "+5V")))
        + tuple(Pin(n, str(4 + i), IN, vih=2.3, vil=1.5, v_max=7.0)  # src: [L298] ViH 2.3, ViL 1.5, VI abs 7 V
                for i, n in enumerate(("ENA", "IN1", "IN2", "IN3", "IN4", "ENB")))
        + tuple(Pin(f"OUT{i}", str(9 + i), OUT) for i in range(1, 5)),
        # src: [L298] IS 13 + ISS 24 mA typ, 70 + 36 mA max; 7 V min for the on-board 78M05 (unverified module)
        supply=Supply(7.0, 35.0, 37.0, 106.0, pin="12V"),
        params={
            "load_outputs": ("OUT1", "OUT2", "OUT3", "OUT4"), "load_supply": "12V",
            "passthrough": {"in": {"OUT1": "IN1", "OUT2": "IN2", "OUT3": "IN3", "OUT4": "IN4"},
                            "pwm": {"OUT1": "ENA", "OUT2": "ENA", "OUT3": "ENB", "OUT4": "ENB"}},
            "i_out_ma": 2000.0,  # src: [L298] 2 A DC per channel
            "v_drop": 2.0,       # src: unverified (L298 total saturation ~1.8-3.2 V at 1 A)
        },
        features=("driver", "clamped_outputs"), mech="l298n_module", ref_prefix="U", footprint=HDR.format(n=13),
        datasheet="https://www.st.com/resource/en/datasheet/l298.pdf",
        notes="With the 5V-EN jumper fitted the '5V' pin OUTPUTS 5 V from the 78M05 (needs VS >= 7 V): never tie "
              "it to the Pi 5 V. ENA/ENB jumpers hold the bridges enabled; remove them to PWM from a GPIO.",
    ),
    PartDef(
        key="dc_motor", name="Small brushed DC motor (TT gear motor class)", category="motor",
        pins=(Pin("M+", "1", P, aliases=("+", "A")), Pin("M-", "2", P, aliases=("-", "B"))),
        # src: unverified (TT gear motor listings: 3-6 V, ~150 mA no-load, ~1.5 A stall, ~4 Ω winding)
        supply=Supply(3.0, 6.0, 150.0, 1500.0, pin="M+"),
        params={"load_ohms": 4.0, "load_pins": ("M+", "M-"), "coils": (("M+", "M-"),)},
        features=("motor", "inductive"),
        sim={"twin": "dc_motor", "spice": "dc_motor_small", "pins": {"in1": "M+:in", "in2": "M-:in", "pwm": "M+:pwm"}},
        ref_prefix="M", footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        datasheet="generic 3-6 V TT gear motor (unverified)",
        notes="Never on a GPIO: drive with an H-bridge (TB6612/L298N) or a MOSFET + flyback diode.",
    ),
    PartDef(
        key="relay_1ch_5v", name="1-channel 5 V relay module (transistor driver + flyback diode)", category="relay",
        pins=(Pin("VCC", "1", PI, required=True, aliases=("DC+", "+")), Pin("GND", "2", GN, required=True, aliases=("DC-", "-")),
              Pin("IN", "3", IN, required=True, vih=1.5, aliases=("S", "SIG")),
              Pin("COM", "4", P), Pin("NO", "5", P), Pin("NC", "6", P)),
        # src: [SRD] coil 71.4 mA @ 5 V; module LED + driver ~4 mA and 4.5-5.5 V window unverified
        supply=Supply(4.5, 5.5, 75.0, 80.0, pin="VCC"),
        params={"trigger": "high", "input_loads": {"IN": (1000.0, 0.7, "GND")},  # src: unverified (1 kΩ + NPN base)
                "contacts": "10 A 250 VAC / 10 A 28 VDC",  # src: [SRD]
                "isolated_pins": ("COM", "NO", "NC")},  # contacts are galvanically isolated from the coil
        features=("driver", "flyback", "relay"), sim={"twin": "relay", "pins": {"pin": "IN"}}, mech="relay_1ch_5v",
        ref_prefix="K", footprint=HDR.format(n=3), datasheet="Songle SRD-05VDC-SL-C relay on a typical module",
        notes="Modelled as the high-level-trigger NPN type. Low-level-trigger/optocoupler modules may not switch "
              "off from a 3.3 V GPIO - check the module.",
    ),
    PartDef(
        key="relay_srd05vdc", name="Songle SRD-05VDC-SL-C relay (bare, 5 V coil)", category="relay",
        pins=(Pin("COIL1", "1", P, required=True), Pin("COIL2", "2", P, required=True),
              Pin("COM", "3", P), Pin("NO", "4", P), Pin("NC", "5", P)),
        supply=Supply(3.75, 6.0, 71.4, 71.4, pin="COIL1"),  # src: [SRD] pull-in 75 %, max 120 %, 71.4 mA
        params={"load_ohms": 70.0, "load_pins": ("COIL1", "COIL2"), "coils": (("COIL1", "COIL2"),),  # src: [SRD]
                "motor_pins": ("COIL1", "COIL2"), "contacts": "10 A 250 VAC / 10 A 28 VDC",
                "isolated_pins": ("COM", "NO", "NC")},
        features=("inductive", "relay"), sim={"twin": "relay", "pins": {"pin": "COIL2:in"}}, ref_prefix="K",
        footprint="Relay_THT:Relay_SPDT_SANYOU_SRD_Series_Form_C",
        datasheet="https://www.circuitbasics.com/wp-content/uploads/2015/11/SRD-05VDC-SL-C-Datasheet.pdf",
        notes="Drive the coil with a transistor and put a diode across it (cathode to +).",
    ),
    PartDef(
        key="buzzer_active", name="Active buzzer 5 V (TMB12A05)", category="audio",
        pins=(Pin("+", "1", P, required=True, aliases=("VCC", "POS")), Pin("-", "2", P, required=True, aliases=("GND", "NEG"))),
        supply=Supply(4.0, 8.0, 30.0, 30.0, pin="+"),  # src: [TMB12A05] 4-8 V, 30 mA
        params={"load_ohms": 167.0, "load_pins": ("+", "-")},  # src: derived 5 V / 30 mA (linear approximation)
        sim={"twin": "buzzer", "pins": {"pin": ("+", "-:in")}}, mech="buzzer_12mm", ref_prefix="BZ",
        footprint="Buzzer_Beeper:Buzzer_12x9.5RM7.6", datasheet="https://www.lcsc.com/product-detail/C96093.html",
        notes="30 mA exceeds a GPIO's 16 mA: switch it with a transistor.",
    ),
    PartDef(
        key="buzzer_passive", name="Passive magnetic buzzer / transducer 12 mm", category="audio",
        pins=(Pin("+", "1", P, required=True), Pin("-", "2", P, required=True)),
        supply=Supply(3.0, 5.0, 30.0, 60.0, pin="+"),  # src: unverified (typical 12085 transducer)
        params={"load_ohms": 16.0, "load_pins": ("+", "-"), "coils": (("+", "-"),)},  # src: unverified (16 Ω coil)
        features=("inductive",), sim={"twin": "buzzer", "pins": {"pin": ("+", "-:in")}}, mech="buzzer_12mm",
        ref_prefix="BZ", footprint="Buzzer_Beeper:Buzzer_12x9.5RM7.6", datasheet="generic 12 mm magnetic transducer",
        notes="Needs a PWM tone and a transistor driver with a flyback diode.",
    ),
    PartDef(
        key="fan_5v", name="30 mm 5 V fan", category="motor",
        pins=(Pin("+", "1", P, required=True, aliases=("RED", "VCC")), Pin("-", "2", P, required=True, aliases=("BLACK", "GND"))),
        supply=Supply(4.5, 5.5, 100.0, 200.0, pin="+"),  # src: unverified (typical 3010 5 V fan 0.1 A, 2x inrush)
        params={"load_ohms": 50.0, "load_pins": ("+", "-"), "coils": (("+", "-"),)},
        features=("motor", "inductive"), mech="fan_30mm", ref_prefix="M",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", datasheet="generic 30x30x10 mm 5 V fan",
        notes="Switch with a MOSFET + flyback diode, or power from 5 V directly.",
    ),
    PartDef(
        key="lm2596_module", name="LM2596 adjustable buck converter module", category="regulator",
        pins=(Pin("IN+", "1", PI, required=True, aliases=("VIN", "IN")), Pin("IN-", "2", GN, required=True),
              Pin("OUT+", "3", PinType.POWER_OUT, voltage=5.0, aliases=("VOUT", "OUT")), Pin("OUT-", "4", GN)),
        supply=Supply(4.5, 40.0, 5.0, 10.0, pin="IN+"),  # src: [LM2596] VIN <= 40 V; quiescent 5-10 mA unverified
        params={"vout_pin": "OUT+", "vout": 5.0, "efficiency": 0.8,  # src: [LM2596] 5 V version 80 % @ 12 V, 3 A
                "i_out_max_ma": 3000.0, "requires_input": "IN+", "dropout_v": 1.5},  # src: [LM2596] 3 A; dropout unverified
        ties=(("IN-", "OUT-"),), features=("regulator",), mech="lm2596_module", ref_prefix="U",
        footprint=HDR.format(n=4), datasheet="https://www.ti.com/lit/ds/symlink/lm2596.pdf",
        notes="Set vout= to the trimmed output voltage. Input must exceed vout by ~1.5 V.",
    ),
]
