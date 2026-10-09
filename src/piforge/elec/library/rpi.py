"""Raspberry Pi boards (40-pin header with alternate functions and GPIO electrical data) and PSUs.

Sources (abbreviations used in the ``# src:`` comments):

- [DOCS-GPIO] raspberrypi/documentation asciidoc/computers/raspberry-pi/gpio-on-raspberry-pi.adoc
  ("GPIO and the 40-pin header", voltage specification tables for BCM2835/6/7+RP3A0 and BCM2711).
- [DOCS-PADS] .../gpio-pad-controls.adoc ("Safe current", drive strength list, ~3 mA/pin 3V3 design).
- [DOCS-PSU] .../power-supplies.adoc ("Typical power requirements" table, workload table, 4.63 V
  brown-out detector, "GPIO pins can draw 50mA safely; each pin can individually draw up to 16mA",
  "The Camera Module requires 250mA").
- [PI4-DS] Raspberry Pi 4 Model B datasheet rel. 1.1 (Mar 2024): Table 2, Table 3 (DC
  characteristics), Table 5 (GPIO alternate functions + default pulls).
- [BCM2835] BCM2835 ARM Peripherals (2012) Table 6-31 (alt functions; BCM2837/RP3A0 identical).
- [BCM2711] BCM2711 ARM Peripherals Table 94 (alt functions, PWM0_0/PWM0_1 naming).
- [RP1] RP1 Peripherals (2023) Table 4 (function select a0..a8), §3.1.3 pads (2/4/8/12 mA).
- [CM5-DS] Compute Module 5 datasheet Tables 7-8 (RP1 GPIO = Pi 5 header GPIO; DC characteristics,
  abs max VGPIO_VREF + 0.5 V, "GPIO2 and GPIO3 include 1.8 kΩ pull-up resistors", 50 mA total).
- [SCHEM] Raspberry Pi 4 reduced schematics (2019): "ID_SD and ID_SC PINS ... DO NOT USE these pins
  for anything other than attaching an I2C ID EEPROM".
- [PINOUT] pinout.xyz (community): "fixed 1.8 kΩ pull-up resistor to 3.3v" on I2C1; "All Raspberry
  Pi models since the B+ can provide up to 500mA on the 3v3 pins" (no official figure exists).
- [GEERLING] Jeff Geerling power measurements (pidramble.com benchmarks; Pi 5 2024 blog; Zero 2 W review).
- [RPI-PSU] raspberrypi.com product pages: 15W USB-C PSU 5.1 V/3.0 A; 27W USB-C PSU 5.1 V/5.0 A;
  micro USB PSU 5.1 V/2.5 A.
"""

from __future__ import annotations

import re

from piforge.elec.model import PartDef, Pin, PinType, Supply

# --------------------------------------------------------------------------------------------
# Physical header layout (identical on every 40-pin board) — src: [DOCS-GPIO] pinout diagram, [PI4-DS] Fig. 3
HEADER: dict[int, str | int] = {
    1: "3V3", 2: "5V", 3: 2, 4: "5V", 5: 3, 6: "GND", 7: 4, 8: 14, 9: "GND", 10: 15,
    11: 17, 12: 18, 13: 27, 14: "GND", 15: 22, 16: 23, 17: "3V3", 18: 24, 19: 10, 20: "GND",
    21: 9, 22: 25, 23: 11, 24: 8, 25: "GND", 26: 7, 27: 0, 28: 1, 29: 5, 30: "GND",
    31: 6, 32: 12, 33: 13, 34: "GND", 35: 19, 36: 16, 37: 26, 38: 20, 39: "GND", 40: 21,
}

# Alternate functions ALT0..ALT5 per BCM GPIO, header GPIOs 0-27 — src: [BCM2835] Table 6-31
# (BCM2837 in the Pi 3B+ and RP3A0 in the Zero 2 W share the BCM2835 GPIO block).
ALT_BCM2835: dict[int, tuple] = {
    0: ("SDA0", "SA5"), 1: ("SCL0", "SA4"), 2: ("SDA1", "SA3"), 3: ("SCL1", "SA2"),
    4: ("GPCLK0", "SA1", None, None, None, "ARM_TDI"), 5: ("GPCLK1", "SA0", None, None, None, "ARM_TDO"),
    6: ("GPCLK2", "SOE_N", None, None, None, "ARM_RTCK"), 7: ("SPI0_CE1_N", "SWE_N"),
    8: ("SPI0_CE0_N", "SD0"), 9: ("SPI0_MISO", "SD1"), 10: ("SPI0_MOSI", "SD2"), 11: ("SPI0_SCLK", "SD3"),
    12: ("PWM0", "SD4", None, None, None, "ARM_TMS"), 13: ("PWM1", "SD5", None, None, None, "ARM_TCK"),
    14: ("TXD0", "SD6", None, None, None, "TXD1"), 15: ("RXD0", "SD7", None, None, None, "RXD1"),
    16: (None, "SD8", None, "CTS0", "SPI1_CE2_N", "CTS1"), 17: (None, "SD9", None, "RTS0", "SPI1_CE1_N", "RTS1"),
    18: ("PCM_CLK", "SD10", None, "BSCSL_SDA", "SPI1_CE0_N", "PWM0"),
    19: ("PCM_FS", "SD11", None, "BSCSL_SCL", "SPI1_MISO", "PWM1"),
    20: ("PCM_DIN", "SD12", None, "BSCSL_MISO", "SPI1_MOSI", "GPCLK0"),
    21: ("PCM_DOUT", "SD13", None, "BSCSL_CE_N", "SPI1_SCLK", "GPCLK1"),
    22: (None, "SD14", None, "SD1_CLK", "ARM_TRST"), 23: (None, "SD15", None, "SD1_CMD", "ARM_RTCK"),
    24: (None, "SD16", None, "SD1_DAT0", "ARM_TDO"), 25: (None, "SD17", None, "SD1_DAT1", "ARM_TCK"),
    26: (None, None, None, "SD1_DAT2", "ARM_TDI"), 27: (None, None, None, "SD1_DAT3", "ARM_TMS"),
}

# src: [BCM2711] Table 94 (cross-checked with [PI4-DS] Table 5)
ALT_BCM2711: dict[int, tuple] = {
    0: ("SDA0", "SA5", "PCLK", "SPI3_CE0_N", "TXD2", "SDA6"),
    1: ("SCL0", "SA4", "DE", "SPI3_MISO", "RXD2", "SCL6"),
    2: ("SDA1", "SA3", "LCD_VSYNC", "SPI3_MOSI", "CTS2", "SDA3"),
    3: ("SCL1", "SA2", "LCD_HSYNC", "SPI3_SCLK", "RTS2", "SCL3"),
    4: ("GPCLK0", "SA1", "DPI_D0", "SPI4_CE0_N", "TXD3", "SDA3"),
    5: ("GPCLK1", "SA0", "DPI_D1", "SPI4_MISO", "RXD3", "SCL3"),
    6: ("GPCLK2", "SOE_N", "DPI_D2", "SPI4_MOSI", "CTS3", "SDA4"),
    7: ("SPI0_CE1_N", "SWE_N", "DPI_D3", "SPI4_SCLK", "RTS3", "SCL4"),
    8: ("SPI0_CE0_N", "SD0", "DPI_D4", "BSCSL_CE_N", "TXD4", "SDA4"),
    9: ("SPI0_MISO", "SD1", "DPI_D5", "BSCSL_MISO", "RXD4", "SCL4"),
    10: ("SPI0_MOSI", "SD2", "DPI_D6", "BSCSL_SDA", "CTS4", "SDA5"),
    11: ("SPI0_SCLK", "SD3", "DPI_D7", "BSCSL_SCL", "RTS4", "SCL5"),
    12: ("PWM0_0", "SD4", "DPI_D8", "SPI5_CE0_N", "TXD5", "SDA5"),
    13: ("PWM0_1", "SD5", "DPI_D9", "SPI5_MISO", "RXD5", "SCL5"),
    14: ("TXD0", "SD6", "DPI_D10", "SPI5_MOSI", "CTS5", "TXD1"),
    15: ("RXD0", "SD7", "DPI_D11", "SPI5_SCLK", "RTS5", "RXD1"),
    16: (None, "SD8", "DPI_D12", "CTS0", "SPI1_CE2_N", "CTS1"),
    17: (None, "SD9", "DPI_D13", "RTS0", "SPI1_CE1_N", "RTS1"),
    18: ("PCM_CLK", "SD10", "DPI_D14", "SPI6_CE0_N", "SPI1_CE0_N", "PWM0_0"),
    19: ("PCM_FS", "SD11", "DPI_D15", "SPI6_MISO", "SPI1_MISO", "PWM0_1"),
    20: ("PCM_DIN", "SD12", "DPI_D16", "SPI6_MOSI", "SPI1_MOSI", "GPCLK0"),
    21: ("PCM_DOUT", "SD13", "DPI_D17", "SPI6_SCLK", "SPI1_SCLK", "GPCLK1"),
    22: ("SD0_CLK", "SD14", "DPI_D18", "SD1_CLK", "ARM_TRST", "SDA6"),
    23: ("SD0_CMD", "SD15", "DPI_D19", "SD1_CMD", "ARM_RTCK", "SCL6"),
    24: ("SD0_DAT0", "SD16", "DPI_D20", "SD1_DAT0", "ARM_TDO", "SPI3_CE1_N"),
    25: ("SD0_DAT1", "SD17", "DPI_D21", "SD1_DAT1", "ARM_TCK", "SPI4_CE1_N"),
    26: ("SD0_DAT2", None, "DPI_D22", "SD1_DAT2", "ARM_TDI", "SPI5_CE1_N"),
    27: ("SD0_DAT3", None, "DPI_D23", "SD1_DAT3", "ARM_TMS", "SPI6_CE1_N"),
}

# RP1 function select a0..a4 and a8 (a5-a7 are SYS_RIO/PROC_RIO/PIO on every pin) — src: [RP1] Table 4
ALT_RP1: dict[int, tuple] = {
    0: ("SPI0_SIO[3]", "DPI_PCLK", "UART1_TX", "I2C0_SDA", None, "SPI2_CSn[0]"),
    1: ("SPI0_SIO[2]", "DPI_DE", "UART1_RX", "I2C0_SCL", None, "SPI2_SIO[1]"),
    2: ("SPI0_CSn[3]", "DPI_VSYNC", "UART1_CTS", "I2C1_SDA", "UART0_IR_RX", "SPI2_SIO[0]"),
    3: ("SPI0_CSn[2]", "DPI_HSYNC", "UART1_RTS", "I2C1_SCL", "UART0_IR_TX", "SPI2_SCLK"),
    4: ("GPCLK[0]", "DPI_D[0]", "UART2_TX", "I2C2_SDA", "UART0_RI", "SPI3_CSn[0]"),
    5: ("GPCLK[1]", "DPI_D[1]", "UART2_RX", "I2C2_SCL", "UART0_DTR", "SPI3_SIO[1]"),
    6: ("GPCLK[2]", "DPI_D[2]", "UART2_CTS", "I2C3_SDA", "UART0_DCD", "SPI3_SIO[0]"),
    7: ("SPI0_CSn[1]", "DPI_D[3]", "UART2_RTS", "I2C3_SCL", "UART0_DSR", "SPI3_SCLK"),
    8: ("SPI0_CSn[0]", "DPI_D[4]", "UART3_TX", "I2C0_SDA", None, "SPI4_CSn[0]"),
    9: ("SPI0_SIO[1]", "DPI_D[5]", "UART3_RX", "I2C0_SCL", None, "SPI4_SIO[0]"),
    10: ("SPI0_SIO[0]", "DPI_D[6]", "UART3_CTS", "I2C1_SDA", None, "SPI4_SIO[1]"),
    11: ("SPI0_SCLK", "DPI_D[7]", "UART3_RTS", "I2C1_SCL", None, "SPI4_SCLK"),
    12: ("PWM0[0]", "DPI_D[8]", "UART4_TX", "I2C2_SDA", "AUDIO_OUT_L", "SPI5_CSn[0]"),
    13: ("PWM0[1]", "DPI_D[9]", "UART4_RX", "I2C2_SCL", "AUDIO_OUT_R", "SPI5_SIO[1]"),
    14: ("PWM0[2]", "DPI_D[10]", "UART4_CTS", "I2C3_SDA", "UART0_TX", "SPI5_SIO[0]"),
    15: ("PWM0[3]", "DPI_D[11]", "UART4_RTS", "I2C3_SCL", "UART0_RX", "SPI5_SCLK"),
    16: ("SPI1_CSn[2]", "DPI_D[12]", "MIPI0_DSI_TE", "UART0_CTS", None, None),
    17: ("SPI1_CSn[1]", "DPI_D[13]", "MIPI1_DSI_TE", "UART0_RTS", None, None),
    18: ("SPI1_CSn[0]", "DPI_D[14]", "I2S0_SCLK", "PWM0[2]", "I2S1_SCLK", "GPCLK[1]"),
    19: ("SPI1_SIO[1]", "DPI_D[15]", "I2S0_WS", "PWM0[3]", "I2S1_WS", None),
    20: ("SPI1_SIO[0]", "DPI_D[16]", "I2S0_SDI[0]", "GPCLK[0]", "I2S1_SDI[0]", None),
    21: ("SPI1_SCLK", "DPI_D[17]", "I2S0_SDO[0]", "GPCLK[1]", "I2S1_SDO[0]", None),
    22: ("SDIO0_CLK", "DPI_D[18]", "I2S0_SDI[1]", "I2C3_SDA", "I2S1_SDI[1]", None),
    23: ("SDIO0_CMD", "DPI_D[19]", "I2S0_SDO[1]", "I2C3_SCL", "I2S1_SDO[1]", None),
    24: ("SDIO0_DAT[0]", "DPI_D[20]", "I2S0_SDI[2]", None, "I2S1_SDI[2]", "SPI2_CSn[1]"),
    25: ("SDIO0_DAT[1]", "DPI_D[21]", "I2S0_SDO[2]", "AUDIO_IN_CLK", "I2S1_SDO[2]", "SPI3_CSn[1]"),
    26: ("SDIO0_DAT[2]", "DPI_D[22]", "I2S0_SDI[3]", "AUDIO_IN_DAT0", "I2S1_SDI[3]", "SPI5_CSn[1]"),
    27: ("SDIO0_DAT[3]", "DPI_D[23]", "I2S0_SDO[3]", "AUDIO_IN_DAT1", "I2S1_SDO[3]", "SPI1_CSn[1]"),
}

_RP1_I2S = {"I2S0_SCLK": "PCM_CLK", "I2S0_WS": "PCM_FS", "I2S0_SDI[0]": "PCM_DIN", "I2S0_SDO[0]": "PCM_DOUT"}


def normalize_function(raw: str, soc: str) -> list[str]:
    """Canonical names for a datasheet alt-function name (e.g. ``SDA1`` -> ``I2C1_SDA``, ``SDA1``)."""
    out: list[str] = []
    m = re.fullmatch(r"(SDA|SCL)(\d)", raw)
    if m:
        return [f"I2C{m.group(2)}_{m.group(1)}", raw]
    m = re.fullmatch(r"I2C(\d)_(SDA|SCL)", raw)
    if m:
        return [raw, f"{m.group(2)}{m.group(1)}"]
    m = re.fullmatch(r"SPI(\d)_CE(\d)_N", raw)
    if m:
        return [f"SPI{m.group(1)}_CE{m.group(2)}", raw]
    m = re.fullmatch(r"SPI(\d)_CSn\[(\d)\]", raw)
    if m:
        return [f"SPI{m.group(1)}_CE{m.group(2)}"]
    m = re.fullmatch(r"SPI(\d)_SIO\[(\d)\]", raw)
    if m:
        role = {"0": "MOSI", "1": "MISO"}.get(m.group(2))
        return [f"SPI{m.group(1)}_{role}"] if role else [f"SPI{m.group(1)}_SIO{m.group(2)}"]
    m = re.fullmatch(r"(TXD|RXD|CTS|RTS)(\d)", raw)
    if m:
        role = {"TXD": "TX", "RXD": "RX", "CTS": "CTS", "RTS": "RTS"}[m.group(1)]
        return [f"UART{m.group(2)}_{role}", raw]
    if soc in ("BCM2835", "BCM2837") and raw in ("PWM0", "PWM1"):
        return [raw, f"PWM0_{raw[-1]}"]           # BCM2835 "PWM0/PWM1" are channels 0/1 of PWM0
    m = re.fullmatch(r"PWM0_(\d)", raw)
    if m:                                          # BCM2711 naming; [PI4-DS] calls them PWM0/PWM1
        return [raw, f"PWM{m.group(1)}"]
    m = re.fullmatch(r"PWM0\[(\d)\]", raw)
    if m:                                          # RP1: PWM0 block, channel n
        return [f"PWM0_{m.group(1)}", "PWM0"]
    if raw in _RP1_I2S:
        return [raw, _RP1_I2S[raw]]
    if re.fullmatch(r"SD\d+", raw) or raw in ("SA0", "SA1", "SA2", "SA3", "SA4", "SA5", "SOE_N", "SWE_N"):
        return [f"SMI_{raw}"]                      # secondary memory interface (not the SD card)
    out.append(raw.replace("[", "").replace("]", ""))
    return out


def _gpio_functions(bcm: int, soc: str) -> tuple[str, ...]:
    table = {"BCM2835": ALT_BCM2835, "BCM2837": ALT_BCM2835, "BCM2711": ALT_BCM2711, "RP1": ALT_RP1}[soc]
    names = [f"GPIO{bcm}", f"BCM{bcm}"]
    for raw in table[bcm]:
        if raw:
            for n in normalize_function(raw, soc):
                if n not in names:
                    names.append(n)
    return tuple(names)


# Header labels used on every 40-pin pinout (matched before alt functions, so they stay unambiguous on
# the Pi 5 where RP1 also offers I2C1 on GPIO10/11) - src: [DOCS-GPIO] pinout diagram, [PINOUT]
_GPIO_ALIASES = {0: ("ID_SD",), 1: ("ID_SC",), 2: ("SDA", "SDA1"), 3: ("SCL", "SCL1"), 7: ("CE1",),
                 8: ("CE0",), 9: ("MISO",), 10: ("MOSI",), 11: ("SCLK", "SCK"), 14: ("TXD", "TX", "TXD0"),
                 15: ("RXD", "RX", "RXD0")}

# GPIO pad electrical data per SoC family (V, mA, Ω)
_ELEC = {
    # src: [DOCS-GPIO] table "BCM2835, BCM2836, BCM2837 and RP3A0": VIL 0.9 max, VIH 1.6 min (hysteresis),
    # VOL 0.14 V @ 2 mA, VOH 3.0 V @ 2 mA (default 8 mA drive), pulls 50-65 kΩ.
    "BCM2837": dict(vil=0.9, vih=1.6, vol=0.14, voh=3.0, pull_ohms=50_000.0),
    # src: [PI4-DS] Table 3: VIL 0.8, VIH 2.0, VOL 0.4 V @ 2 mA, VOH VDD_IO-0.4 V @ 2 mA, pulls 18/47/73 kΩ
    # (the docs table gives VOH 2.6 V @ 4 mA - both official, different load currents).
    "BCM2711": dict(vil=0.8, vih=2.0, vol=0.4, voh=2.9, pull_ohms=47_000.0),
    # src: [CM5-DS] Table 8 (RP1 @ 3.3 V): VIL 0.8, VIH 2.0, VOL 0.4, VOH VREF-0.4, pulls 37/55/86 kΩ.
    "RP1": dict(vil=0.8, vih=2.0, vol=0.4, voh=2.9, pull_ohms=55_000.0),
}
# Absolute maximum GPIO input voltage.
# src: [CM5-DS] Table 7 Vgpio max = VGPIO_VREF + 0.5 V = 3.8 V (RP1 / Pi 5).
# src: unverified (BCM283x/BCM2711 publish no GPIO abs max; [DOCS-GPIO] only says "3.3V-tolerant";
#      3.6 V = typical LVCMOS VDDIO + 0.3 V assumption).
_V_MAX = {"BCM2837": 3.6, "BCM2711": 3.6, "RP1": 3.8}
GPIO_PIN_MAX_MA = 16.0  # src: [DOCS-PSU] "each pin can individually draw up to 16mA"; [DOCS-PADS] "Safe current 16 mA"
GPIO_TOTAL_MA = 50.0    # src: [DOCS-PSU] "Combined, the GPIO pins can draw 50mA safely"; [CM5-DS] "Don't exceed 50 mA"
GPIO_VDD = 3.3          # src: [DOCS-GPIO] "outputs are set to 3.3 V"
I2C1_PULLUP_OHMS = 1800.0  # src: [CM5-DS] §2.9 "GPIO2 and GPIO3 include 1.8 kΩ pull-up resistors"; [PINOUT]


def header_pins(soc: str) -> tuple[Pin, ...]:
    """The 40 header pins in physical order with functions and electrical data for ``soc``."""
    e = _ELEC[soc if soc != "BCM2835" else "BCM2837"]
    pins = []
    for num in range(1, 41):
        what = HEADER[num]
        n = str(num)
        if what == "3V3":
            pins.append(Pin("3V3", n, PinType.POWER_OUT, voltage=3.3, aliases=("3.3V", "+3V3", "+3.3V", "VDD_3V3")))
        elif what == "5V":
            pins.append(Pin("5V", n, PinType.POWER_OUT, voltage=5.0, aliases=("+5V", "5.0V", "5V0")))
        elif what == "GND":
            pins.append(Pin("GND", n, PinType.GND, aliases=("0V", "VSS", "GROUND")))
        else:
            bcm = int(what)
            pins.append(Pin(
                f"GPIO{bcm}", n, PinType.BIDIR, voltage=GPIO_VDD, v_max=_V_MAX[soc if soc != "BCM2835" else "BCM2837"],
                vih=e["vih"], vil=e["vil"], voh=e["voh"], vol=e["vol"], i_max_ma=GPIO_PIN_MAX_MA,
                functions=_gpio_functions(bcm, soc), aliases=_GPIO_ALIASES.get(bcm, ()),
            ))
    return tuple(pins)


def _defaults_pulls() -> dict[str, str]:
    # Reset pulls: GPIO0-8 high, GPIO9-27 low. src: [PI4-DS] Table 5 "Default Pull"; [BCM2835] Table 6-31
    return {f"GPIO{b}": ("up" if b <= 8 else "down") for b in range(28)}


_COMMON = dict(
    gpio_vdd=GPIO_VDD, gpio_total_ma=GPIO_TOTAL_MA, gpio_pin_max_ma=GPIO_PIN_MAX_MA,
    pullups={"GPIO2": (I2C1_PULLUP_OHMS, "3V3"), "GPIO3": (I2C1_PULLUP_OHMS, "3V3")},
    reserved=("GPIO0", "GPIO1"),  # src: [SCHEM] ID_SD/ID_SC note; [DOCS-GPIO] "reserved for advanced use"
    # src: [PINOUT] "up to 500mA on the 3v3 pins" — unverified (Raspberry Pi publishes no figure;
    # forum reports 800 mA tested on Pi 4). Used as a conservative design budget.
    rail_3v3_budget_ma=500.0,
    rail_3v3_efficiency=0.85,  # src: unverified (PMIC buck efficiency assumption for 3V3 loads seen at 5 V)
    brownout_v=4.63,           # src: [DOCS-PSU] "low-voltage detection ... below 4.63 V (±5%)"
    camera_ma=250.0,           # src: [DOCS-PSU] "The Camera Module requires 250mA"
    default_pulls=_defaults_pulls(),
    has_bt=True,
)

_BCM_PWM = {"PWM0_0": (12, 18), "PWM0_1": (13, 19)}  # src: overlays README "pwm-2chan" legal pin table
_PWM_FUNC = {12: 4, 13: 4, 18: 2, 19: 2}             # src: overlays README: 12,4(Alt0) 18,2(Alt5) 13,4 19,2


def _board(key: str, name: str, soc: str, *, psu_ma: float, usb_ma: float, typical: float, idle: float,
           maximum: float, extra: dict, datasheet: str, notes: str) -> PartDef:
    params = dict(_COMMON)
    params.update(soc=soc, pull_ohms=_ELEC[soc if soc != "BCM2835" else "BCM2837"]["pull_ohms"],
                  psu_recommended_ma=psu_ma, usb_max_ma=usb_ma, typical_ma=typical, idle_ma=idle,
                  max_ma=maximum, pwm_func=_PWM_FUNC)
    params.update(extra)
    return PartDef(
        key=key, name=name, category="board", pins=header_pins(soc),
        # src: [DOCS-PSU] brown-out at 4.63 V; [PI4-DS] Table 2 VIN abs max 6.0 V; 5.25 V = USB 5 V + 5 %
        supply=Supply(4.63, 5.25, typical, maximum, pin="5V"),
        params=params, datasheet=datasheet, notes=notes, ref_prefix="U",
        footprint="Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
        sim={"board": key},
    )


DEFS: list[PartDef] = [
    _board(
        "rpi5", "Raspberry Pi 5", "RP1",
        psu_ma=5000, typical=800,     # src: [DOCS-PSU] table: 5.0A / 1.6A (600mA w/ 3A PSU) / 800mA
        idle=540,     # src: [GEERLING] Pi 5 idle ~2.7 W at 5.1 V (unverified, non-official)
        maximum=1920,  # src: [GEERLING] stress-ng 9.8 W (4/8 GB) at 5.1 V (unverified, non-official)
        extra=dict(usb_max_ma_3a=600, overlay_suffix="-pi5",  # src: [DOCS-PSU] note on 3 A supplies
                   pwm_channels={"PWM0_0": (12,), "PWM0_1": (13,), "PWM0_2": (18, 14), "PWM0_3": (19, 15)},  # src: [RP1] Table 4
                   uarts={"uart0": (14, 15), "uart1": (0, 1), "uart2": (4, 5), "uart3": (8, 9), "uart4": (12, 13)},
                   gpio_drive_ma=(2, 4, 8, 12)),  # src: [RP1] §3.1.3
        usb_ma=1600,
        datasheet="https://datasheets.raspberrypi.com/rpi5/raspberry-pi-5-product-brief.pdf",
        notes="GPIO via RP1; electrical data from the CM5 datasheet (same RP1 GPIO bank).",
    ),
    _board(
        "rpi4b", "Raspberry Pi 4 Model B", "BCM2711",
        psu_ma=3000, usb_ma=1200, typical=600,  # src: [DOCS-PSU] table: 3.0A / 1.2A / 600mA
        idle=540,      # src: [GEERLING] pidramble: idle 540 mA (2.7 W)
        maximum=1250,  # src: [DOCS-PSU] workload table: Stress Max 1.25 A (Pi 4B)
        extra=dict(pwm_channels=_BCM_PWM,
                   uarts={"uart0": (14, 15), "uart2": (0, 1), "uart3": (4, 5), "uart4": (8, 9), "uart5": (12, 13)},  # src: [BCM2711] ALT4
                   gpio_drive_ma=(2, 4, 6, 8, 10, 12, 14, 16)),  # src: [DOCS-PADS] drive strength list
        datasheet="https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf",
        notes="Downstream USB limited to ~1.1 A aggregate ([PI4-DS] §5.3).",
    ),
    _board(
        "rpi3bp", "Raspberry Pi 3 Model B+", "BCM2837",
        psu_ma=2500, usb_ma=1200, typical=500,  # src: [DOCS-PSU] table: 2.5A / 1.2A / 500mA
        idle=350,     # src: [GEERLING] pidramble: Pi 3 B+ idle 350 mA
        maximum=980,  # src: [GEERLING] pidramble: Pi 3 B+ 400 % CPU load 980 mA
        extra=dict(pwm_channels=_BCM_PWM, uarts={"uart0": (14, 15)},
                   gpio_drive_ma=(2, 4, 6, 8, 10, 12, 14, 16)),
        datasheet="https://datasheets.raspberrypi.com/rpi3/raspberry-pi-3-b-plus-product-brief.pdf",
        notes="BCM2837B0; GPIO block identical to BCM2835.",
    ),
    _board(
        "rpizero2w", "Raspberry Pi Zero 2 W", "BCM2837",
        psu_ma=2000, usb_ma=0, typical=350,  # src: [DOCS-PSU] table: 2A / limited by PSU / 350mA
        idle=100,     # src: [GEERLING] Zero 2 W review: 100 mA idle
        maximum=500,  # src: [GEERLING] "up to 500 mA full-tilt"
        extra=dict(pwm_channels=_BCM_PWM, uarts={"uart0": (14, 15)},
                   gpio_drive_ma=(2, 4, 6, 8, 10, 12, 14, 16)),
        datasheet="https://datasheets.raspberrypi.com/rpizero2/raspberry-pi-zero-2-w-product-brief.pdf",
        notes="RP3A0 SiP (BCM2710A1 die). Product brief lists 5 V 2.5 A input; docs table recommends 2 A. "
              "Header is unpopulated on the non-H version.",
    ),
]


def _psu(key: str, name: str, volts: float, amps_ma: float, url: str, notes: str, connector: str) -> PartDef:
    return PartDef(
        key=key, name=name, category="power",
        pins=(Pin("V+", "1", PinType.POWER_OUT, voltage=volts,
                  aliases=("VBUS", "+", "VOUT", "OUT+", "5V" if abs(volts - 5.0) < 0.3 else f"{volts:g}V")),
              Pin("GND", "2", PinType.GND, aliases=("-", "0V", "OUT-"))),
        params={"v_out": volts, "i_max_ma": amps_ma, "connector": connector},
        features=("psu",), ref_prefix="PS", datasheet=url, notes=notes,
        footprint="Connector_BarrelJack:BarrelJack_Horizontal",
    )


DEFS += [
    _psu("psu_usbc_5v3a", "Raspberry Pi 15W USB-C Power Supply (5.1 V 3 A)", 5.1, 3000,  # src: [RPI-PSU]
         "https://www.raspberrypi.com/products/type-c-power-supply/",
         "Official Pi 4 supply. Plugged into the Pi's USB-C it powers the Pi (no wiring needed).", "usb-c"),
    _psu("psu_usbc_5v5a", "Raspberry Pi 27W USB-C Power Supply (5.1 V 5 A)", 5.1, 5000,  # src: [RPI-PSU]
         "https://www.raspberrypi.com/products/27w-power-supply/",
         "Official Pi 5 supply (also 9 V 3 A, 12 V 2.25 A, 15 V 1.8 A PD profiles). Pi 5 allows 1.6 A USB "
         "only with a 5 A supply.", "usb-c"),
    _psu("psu_microusb_5v2a5", "Raspberry Pi micro USB Power Supply (5.1 V 2.5 A)", 5.1, 2500,  # src: [RPI-PSU]
         "https://www.raspberrypi.com/products/micro-usb-power-supply/",
         "Official supply for Pi 3B+ / Zero 2 W.", "micro-usb"),
    _psu("psu_dc_5v5a", "5 V 5 A DC adapter (5.5x2.1 mm barrel)", 5.0, 5000,
         "generic wall adapter",  # src: unverified (generic 5 V 5 A rating printed on typical adapters)
         "Feeds a 5 V rail shared by the Pi (5V header pin / back-powering) and motors; use short, thick "
         "wires and a polyfuse. Tie its GND to the Pi GND.", "barrel"),
    _psu("psu_dc_12v2a", "12 V 2 A DC adapter (5.5x2.1 mm barrel)", 12.0, 2000,
         "generic wall adapter",  # src: unverified (generic rating printed on typical adapters)
         "Separate supply for motors/LED strips; tie its GND to the Pi GND.", "barrel"),
]
