"""Semiconductors: diodes, BJT, MOSFETs, the MCP3008 ADC and the BSS138 level shifter.

Sources:
- [1N4148] Vishay 1N4148 (doc 81857): VR 75 V, IF 300 mA, IF(AV) 150 mA, VF <= 1 V @ 10 mA.
- [1N4007] Vishay 1N4001-1N4007 (doc 88503): IF(AV) 1.0 A, VRRM 1000 V, VF 1.1 V.
- [1N5819] Vishay 1N5817-1N5819 (doc 88525): IF(AV) 1.0 A, VRRM 40 V, VF 0.60 V @ 1 A.
- [PN2222A] Fairchild/onsemi PN2222A: VCEO 40 V, IC 1.0 A, hFE 100-300 @ 150 mA, VBE(sat) 0.6-1.2 V,
  TO-92 pinout E-B-C.
- [2N7000] onsemi 2N7000/2N7002/NDS7002A: VDSS 60 V, ID 200 mA, VGS(th) 0.8/2.1/3.0 V,
  RDS(on) <= 5 Ω @ 10 V and <= 5.3 Ω @ 4.5 V; TO-92: 1 S, 2 G, 3 D.
- [AO3400A] Alpha & Omega AO3400A: 30 V, 5.7 A, VGS(th) 0.65/1.05/1.45 V, RDS(on) < 26.5 mΩ @ 10 V,
  < 32 mΩ @ 4.5 V, < 48 mΩ @ 2.5 V; SOT-23: 1 G, 2 S, 3 D.
- [IRLZ44N] Infineon IRLZ44NPbF: 55 V, 47 A, VGS(th) 1.0-2.0 V, RDS(on) 0.022 Ω @ 10 V,
  0.025 Ω @ 5 V, 0.035 Ω @ 4 V; TO-220 G-D-S.
- [IRF540N] Infineon IRF540NPbF: 100 V, 33 A, VGS(th) 2.0-4.0 V, RDS(on) 44 mΩ @ 10 V only.
- [BSS138] onsemi BSS138: 50 V, 0.22 A, VGS(th) 0.8-1.5 V. [ADA757] Adafruit 757 4-ch converter:
  "works down to 1.8V on the low side, and up to 10V on the high side", 10 kΩ pull-ups both sides.
- [MCP3008] Microchip DS21295: VDD 2.7-5.5 V, IDD 425 µA typ / 550 µA max @ 5 V, VREF 100/150 µA,
  VIH 0.7 VDD, VIL 0.3 VDD, VOH >= 4.1 V @ VDD 4.5 V; PDIP-16 pinout CH0-7, DGND, CS, DIN, DOUT, CLK,
  AGND, VREF, VDD.
"""

from __future__ import annotations

from piforge.elec.model import PartDef, Pin, PinType, Supply

P = PinType.PASSIVE


def _diode(key: str, name: str, vf: float, vf_max: float, if_ma: float, vr: float, spice: str, ds: str,
           footprint: str, category: str = "diode") -> PartDef:
    return PartDef(
        key=key, name=name, category=category,
        pins=(Pin("K", "1", P, required=True, aliases=("cathode", "-")),
              Pin("A", "2", P, required=True, aliases=("anode", "+"))),
        params={"vf": vf, "vf_max": vf_max, "if_max_ma": if_ma, "vr_max": vr},
        sim={"spice": spice}, ref_prefix="D", datasheet=ds, footprint=footprint,
        notes="Flyback use: cathode to the supply side of the coil, anode to the switched side.",
    )


DEFS: list[PartDef] = [
    _diode("diode_1n4148", "1N4148 small-signal diode", 0.7, 1.0, 300.0, 75.0, "d1n4148",  # src: [1N4148]
           "https://www.vishay.com/docs/81857/1n4148.pdf", "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal"),
    _diode("diode_1n4007", "1N4007 rectifier diode 1 A 1000 V", 0.7, 1.1, 1000.0, 1000.0, "d1n4007",  # src: [1N4007]
           "https://www.vishay.com/docs/88503/1n4001.pdf", "Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal"),
    _diode("schottky_1n5819", "1N5819 Schottky diode 1 A 40 V", 0.4, 0.6, 1000.0, 40.0, "d1n5819",  # src: [1N5819]
           "https://www.vishay.com/docs/88525/1n5817.pdf", "Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal"),
    PartDef(
        key="npn_2n2222", name="PN2222A NPN transistor (TO-92)", category="transistor",
        pins=(Pin("E", "1", P, aliases=("emitter",)), Pin("B", "2", PinType.INPUT, aliases=("base",)),
              Pin("C", "3", P, aliases=("collector",))),
        # src: [PN2222A] VCEO 40 V, IC 1.0 A abs max, hFE >= 100 @ 150 mA, VBE ~0.7 V, VCE(sat) 0.3 V @ 150 mA
        params={"kind": "npn", "vbe_on": 0.7, "vce_sat": 0.3, "hfe_min": 100, "ic_max_ma": 1000.0, "vceo_max": 40.0,
                "passthrough": {"in": {"C": "B"}}},
        sim={"spice": "q2n2222"}, ref_prefix="Q", footprint="Package_TO_SOT_THT:TO-92_Inline",
        datasheet="https://www.onsemi.com/pdf/datasheet/pn2222a-d.pdf",
        notes="Pinout E-B-C (PN2222A). The metal-can 2N2222 and onsemi P2N2222A use C-B-E - check your part. "
              "Base needs a resistor: Rb = (3.3 V - 0.7 V) / Ib.",
    ),
]


def _nmos(key: str, name: str, order: tuple[str, str, str], *, vth: tuple[float, float], vgs_rated: float,
          rds: float, vds: float, id_ma: float, vgs_max: float, spice: str, ds: str, footprint: str,
          notes: str) -> PartDef:
    pins = []
    for i, role in enumerate(order, start=1):
        if role == "G":
            pins.append(Pin("G", str(i), PinType.INPUT, v_max=vgs_max, aliases=("gate",)))
        else:
            pins.append(Pin(role, str(i), P, aliases=({"D": "drain", "S": "source"}[role],)))
    return PartDef(
        key=key, name=name, category="transistor", pins=tuple(pins),
        params={"kind": "nmos", "vgs_th_min": vth[0], "vgs_th_max": vth[1], "vgs_rated": vgs_rated,
                "rds_on_ohm": rds, "vds_max": vds, "id_max_ma": id_ma, "passthrough": {"in": {"D": "G"}}},
        sim={"spice": spice}, ref_prefix="Q", datasheet=ds, footprint=footprint, notes=notes,
    )


DEFS += [
    # vgs_rated = lowest VGS at which the datasheet guarantees RDS(on)
    _nmos("nmos_2n7000", "2N7000 N-MOSFET 60 V 200 mA (TO-92)", ("S", "G", "D"),
          vth=(0.8, 3.0), vgs_rated=4.5, rds=5.3, vds=60.0, id_ma=200.0, vgs_max=20.0, spice="nmos_2n7000",  # src: [2N7000]
          ds="https://www.onsemi.com/pdf/datasheet/nds7002a-d.pdf", footprint="Package_TO_SOT_THT:TO-92_Inline",
          notes="RDS(on) only specified at 4.5 V and 10 V: marginal from a 3.3 V GPIO."),
    _nmos("nmos_ao3400", "AO3400A logic-level N-MOSFET 30 V 5.7 A (SOT-23)", ("G", "S", "D"),
          vth=(0.65, 1.45), vgs_rated=2.5, rds=0.048, vds=30.0, id_ma=5700.0, vgs_max=12.0, spice="nmos_ao3400",  # src: [AO3400A]
          ds="https://www.aosmd.com/res/datasheets/AO3400A.pdf", footprint="Package_TO_SOT_SMD:SOT-23",
          notes="Fully enhanced from 3.3 V (RDS(on) specified at VGS = 2.5 V). VGS max ±12 V."),
    _nmos("nmos_irlz44n", "IRLZ44N logic-level N-MOSFET 55 V 47 A (TO-220)", ("G", "D", "S"),
          vth=(1.0, 2.0), vgs_rated=4.0, rds=0.035, vds=55.0, id_ma=47000.0, vgs_max=16.0, spice="nmos_irlz44n",  # src: [IRLZ44N]
          ds="https://www.infineon.com/dgdl/irlz44npbf.pdf", footprint="Package_TO_SOT_THT:TO-220-3_Vertical",
          notes="'Logic level' for 5 V: RDS(on) specified down to VGS = 4 V, not 3.3 V."),
    _nmos("nmos_irf540n", "IRF540N N-MOSFET 100 V 33 A (TO-220)", ("G", "D", "S"),
          vth=(2.0, 4.0), vgs_rated=10.0, rds=0.044, vds=100.0, id_ma=33000.0, vgs_max=20.0, spice="nmos_irf540n",  # src: [IRF540N]
          ds="https://www.infineon.com/dgdl/irf540npbf.pdf", footprint="Package_TO_SOT_THT:TO-220-3_Vertical",
          notes="Standard-level gate (RDS(on) at 10 V, VGS(th) up to 4 V): not for direct 3.3 V GPIO drive."),
]

_LV = tuple(Pin(f"LV{i}", str(n), PinType.BIDIR, aliases=(f"A{i}",)) for i, n in ((1, 2), (2, 3), (3, 5), (4, 6)))
_HV = tuple(Pin(f"HV{i}", str(n), PinType.BIDIR, aliases=(f"B{i}",)) for i, n in ((1, 8), (2, 9), (3, 11), (4, 12)))

DEFS += [
    PartDef(
        key="bss138_level_shifter", name="4-channel bidirectional level shifter (BSS138)", category="interface",
        pins=(Pin("LV", "1", PinType.POWER_IN, required=True, aliases=("VL", "3V3")), _LV[0], _LV[1],
              Pin("GND", "4", PinType.GND, required=True), _LV[2], _LV[3],
              Pin("HV", "7", PinType.POWER_IN, required=True, aliases=("VH", "5V")), _HV[0], _HV[1],
              Pin("GND", "10", PinType.GND), _HV[2], _HV[3]),
        params={
            # src: [ADA757] 10 kΩ pull-ups on both sides
            "pullups": {**{f"LV{i}": (10_000.0, "LV") for i in range(1, 5)},
                        **{f"HV{i}": (10_000.0, "HV") for i in range(1, 5)}},
            "logic_map": {**{f"LV{i}": "LV" for i in range(1, 5)}, **{f"HV{i}": "HV" for i in range(1, 5)}},
            "lv_range": (1.8, 5.0), "hv_max": 10.0,  # src: [ADA757]
        },
        features=("level_shifter",), sim={"spice": "nmos_bss138"}, ref_prefix="U",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x12_P2.54mm_Vertical",
        datasheet="https://www.adafruit.com/product/757 ; BSS138 onsemi datasheet",
        notes="Open-drain style translator: fine for I2C, UART, slow SPI (<2 MHz); not for driving loads.",
    ),
    PartDef(
        key="mcp3008", name="MCP3008 8-channel 10-bit ADC (SPI, DIP-16)", category="adc",
        pins=tuple(Pin(f"CH{i}", str(i + 1), PinType.ANALOG) for i in range(8)) + (
            Pin("DGND", "9", PinType.GND, required=True),
            Pin("CS", "10", PinType.INPUT, required=True, vih_ratio=0.7, vil_ratio=0.3,
                functions=("SPI_CS",), aliases=("CS/SHDN", "SHDN", "CE")),
            Pin("DIN", "11", PinType.INPUT, required=True, vih_ratio=0.7, vil_ratio=0.3,
                functions=("SPI_MOSI",), aliases=("MOSI", "SDI")),
            Pin("DOUT", "12", PinType.OUTPUT, required=True, functions=("SPI_MISO",), aliases=("MISO", "SDO")),
            Pin("CLK", "13", PinType.INPUT, required=True, vih_ratio=0.7, vil_ratio=0.3,
                functions=("SPI_SCLK",), aliases=("SCLK", "SCK")),
            Pin("AGND", "14", PinType.GND, required=True),
            Pin("VREF", "15", PinType.POWER_IN, required=True),
            Pin("VDD", "16", PinType.POWER_IN, required=True, aliases=("VCC",)),
        ),
        supply=Supply(2.7, 5.5, 0.425 + 0.1, 0.55 + 0.15, pin="VDD"),  # src: [MCP3008] IDD + IREF typ/max
        logic_from="VDD", ref_prefix="U",
        sim={"twin": "mcp3008", "pins": {"clk": "CLK", "mosi": "DIN", "miso": "DOUT", "cs": "CS"}},
        mech="mcp3008_dip16", footprint="Package_DIP:DIP-16_W7.62mm",
        datasheet="https://ww1.microchip.com/downloads/en/DeviceDoc/21295d.pdf",
        notes="Power from 3V3 when DOUT goes straight to the Pi (DOUT high = VDD).",
    ),
]


# --------------------------------------------------------------------------- money-counter ICs
# Sources:
# - [HC595] TI SN74HC595 (SCLS041J): VCC 2-6 V; VIH 1.5 / 3.15 / 4.2 V at VCC 2 / 4.5 / 6 V (= 0.7 VCC),
#   VIL 0.5 / 1.35 / 1.8 V (= 0.3 VCC); ICC 80 µA max (static, VCC 6 V); continuous output ±35 mA,
#   VCC/GND ±70 mA; IOH/IOL ±6 mA at VCC 4.5 V for the VOH/VOL spec. DIP-16 pinout: 1-7 QB-QH, 8 GND,
#   9 QH', 10 SRCLR, 11 SRCLK, 12 RCLK, 13 OE, 14 SER, 15 QA, 16 VCC.
# - [HCT595] TI SN74HCT595 (SCLS594): VCC 4.5-5.5 V, VIH 2 V, VIL 0.8 V (TTL levels), same pinout.
# - [ULN2003A] TI ULN2003A (SLRS027): 7 Darlingtons, 2.7 kΩ base resistor, VI(on) 2.4 V max (IC 200 mA),
#   IC 500 mA peak per channel, VCE 50 V, input 30 V max; clamp diodes from each output to COM.
#   DIP-16: 1-7 = 1B-7B, 8 = E (GND), 9 = COM, 10-16 = 7C-1C.
# - [A3144] Allegro A3141-A3144 datasheet: VCC 4.5-24 V, ICC 4.4 mA typ (output off) / 10 mA max
#   (output on, VCC 24 V), open-collector output 25 mA max, VOUT(sat) 175 mV typ / 400 mV max @ 20 mA,
#   pinout (branded face) 1 VCC, 2 GND, 3 OUT.

_SR_OUT = tuple(f"Q{x}" for x in "ABCDEFGH")


def _x595(key: str, name: str, *, vih: dict, supply: Supply, ds: str, notes: str) -> PartDef:
    def inp(n: str, num: str, **kw) -> Pin:
        return Pin(n, num, PinType.INPUT, required=True, **vih, **kw)

    return PartDef(
        key=key, name=name, category="ic",
        pins=(Pin("QB", "1", PinType.OUTPUT), Pin("QC", "2", PinType.OUTPUT), Pin("QD", "3", PinType.OUTPUT),
              Pin("QE", "4", PinType.OUTPUT), Pin("QF", "5", PinType.OUTPUT), Pin("QG", "6", PinType.OUTPUT),
              Pin("QH", "7", PinType.OUTPUT), Pin("GND", "8", PinType.GND, required=True),
              Pin("QH'", "9", PinType.OUTPUT, aliases=("QHS", "QH_PRIME", "SER_OUT", "Q7S")),
              inp("SRCLR", "10", aliases=("~SRCLR", "MR", "SRCLR_N")),
              inp("SRCLK", "11", functions=("SPI_SCLK",), aliases=("SHCP", "SCK", "CLK")),
              inp("RCLK", "12", functions=("SPI_CS",), aliases=("STCP", "LATCH", "CS")),
              inp("OE", "13", aliases=("~OE", "OE_N", "G")),
              inp("SER", "14", functions=("SPI_MOSI",), aliases=("DS", "DATA", "SI")),
              Pin("QA", "15", PinType.OUTPUT), Pin("VCC", "16", PinType.POWER_IN, required=True)),
        supply=supply, logic_from="VCC", ref_prefix="U", footprint="Package_DIP:DIP-16_W7.62mm",
        params={"shift_register": {"outputs": _SR_OUT, "data_in": "SER", "data_out": "QH'", "clock": "SRCLK",
                                   "latch": "RCLK", "oe": "OE"},
                "i_out_max_ma": 35.0},  # src: [HC595] continuous output current ±35 mA
        sim={"twin": "shift_register_74hc595",
             "pins": {"data": "SER", "clock": "SRCLK", "latch": "RCLK", "oe": "OE"}},
        datasheet=ds, notes=notes,
    )


DEFS += [
    _x595("sn74hc595", "74HC595 8-bit shift register with output latches (DIP-16)",
          vih={"vih_ratio": 0.7, "vil_ratio": 0.3},                    # src: [HC595] 3.15 V at 4.5 V = 0.7 VCC
          supply=Supply(2.0, 6.0, 0.04, 0.08, pin="VCC"),              # src: [HC595] ICC 80 µA max; typ unverified
          ds="https://www.ti.com/lit/ds/symlink/sn74hc595.pdf",
          notes="CMOS inputs: VIH = 0.7 x VCC (3.5 V at 5 V), so 3.3 V GPIOs are marginal on a 5 V supply. "
                "Power it from 3V3 or use the 74HCT595. Daisy-chain QH' -> next SER; OE to GND, SRCLR to VCC."),
    _x595("sn74hct595", "74HCT595 8-bit shift register, TTL inputs (DIP-16)",
          vih={"vih": 2.0, "vil": 0.8},                                # src: [HCT595] VIH 2 V, VIL 0.8 V
          supply=Supply(4.5, 5.5, 0.04, 0.08, pin="VCC"),              # src: [HCT595] VCC 4.5-5.5 V; ICC typ unverified
          ds="https://www.ti.com/lit/ds/symlink/sn74hct595.pdf",
          notes="TTL-level inputs accept 3.3 V GPIOs while the outputs swing 5 V (ULN2003, LEDs)."),
    PartDef(
        key="uln2003a", name="ULN2003A 7-channel Darlington array (DIP-16)", category="driver",
        pins=tuple(Pin(f"{i}B", str(i), PinType.INPUT, vih=2.4, v_max=30.0, aliases=(f"IN{i}", f"I{i}"))
                   for i in range(1, 8))                                # src: [ULN2003A] VI(on) 2.4 V, VI 30 V max
        + (Pin("E", "8", PinType.GND, required=True, aliases=("GND",)),
           Pin("COM", "9", PinType.PASSIVE, aliases=("K", "CLAMP")))
        + tuple(Pin(f"{i}C", str(17 - i), PinType.OPEN_DRAIN, i_max_ma=500.0, aliases=(f"OUT{i}", f"O{i}"))
                for i in range(7, 0, -1)),                              # src: [ULN2003A] 500 mA per channel
        params={
            "input_loads": {f"{i}B": (2700.0, 1.4, "E") for i in range(1, 8)},  # src: [ULN2003A] 2.7 kΩ + 2 Vbe
            "passthrough": {"in": {f"{i}C": f"{i}B" for i in range(1, 8)}},
            "clamp_pin": "COM", "i_out_ma": 500.0, "vce_max": 50.0,           # src: [ULN2003A]
        },
        features=("driver", "clamped_outputs"), ref_prefix="U", footprint="Package_DIP:DIP-16_W7.62mm",
        datasheet="https://www.ti.com/lit/ds/symlink/uln2003a.pdf",
        notes="Tie COM to the motor/coil supply: only then do the internal clamp diodes catch the turn-off "
              "spikes (ERC.INDUCTIVE_NO_FLYBACK checks it). Inputs accept 3.3 V and 5 V logic.",
    ),
    PartDef(
        key="hall_a3144", name="A3144 unipolar Hall-effect switch, open collector (TO-92UA)", category="sensor",
        pins=(Pin("VCC", "1", PinType.POWER_IN, required=True, aliases=("VDD", "+")),
              Pin("GND", "2", PinType.GND, required=True, aliases=("-",)),
              Pin("OUT", "3", PinType.OPEN_DRAIN, required=True, i_max_ma=25.0, vol=0.4,
                  aliases=("DO", "SIG", "S"))),                          # src: [A3144] 25 mA, VOUT(sat) 0.4 V max
        supply=Supply(4.5, 24.0, 4.4, 10.0, pin="VCC"),                  # src: [A3144] VCC range, ICC typ/max
        features=("sensor", "hall"), ref_prefix="U", footprint="Package_TO_SOT_THT:TO-92-3_Inline",
        datasheet="https://www.allegromicro.com/-/media/files/datasheets/a3141-2-3-4-datasheet.ashx",
        notes="Output pulls LOW while a south pole faces the branded side; it is open collector, so power it "
              "from 5 V and pull OUT up to 3V3 (the Pi's internal pull-up works) — never to 5 V.",
    ),
]
