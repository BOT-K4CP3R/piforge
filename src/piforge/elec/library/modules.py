"""Sensor, display, I/O and camera modules (breakout boards).

Sources:
- [BME280] Bosch BST-BME280-DS002: VDD 1.71-3.6 V, VDDIO 1.2-3.6 V, abs max 4.25 V supply and
  VDDIO + 0.3 V on interface pins, 3.6 µA @ 1 Hz, 714 µA peak (pressure measurement),
  Table 32: VIL 20 % / VIH 80 % VDDIO, address 0x76 (SDO=GND) / 0x77 (SDO=VDDIO).
- [SSD1306] Solomon Systech SSD1306 rev 1.1: VDD 1.65-3.3 V, VIH 0.8 VDD, VIL 0.2 VDD, address 0x3C/0x3D.
- [PCF8574] TI PCF8574 (SCPS068): VCC 2.5-6 V, VIH 0.7 VCC, VIL 0.3 VCC, ICC 40/100 µA, address 0100 A2A1A0.
- [ADS1115] TI ADS111x (SBAS444E): VDD 2.0-5.5 V, IVDD 150/200 µA (300 µA over temp), VIH 0.7 VDD
  up to 5.5 V, ADDR -> 0x48/0x49/0x4A/0x4B.
- [HX711] Avia HX711: 2.6-5.5 V, analog 1400 µA + digital 100 µA ("< 1.5 mA").
- [HCSR04] Elecfreaks HC-SR04: "Working Voltage DC 5 V, Working Current 15mA", pins Vcc Trig Echo GND.
- [HCSR501] Elecfreaks HC-SR501 product description: DC 4.5-20 V, quiescent < 50 µA, output high 3.3 V.
- [ADA2167] Adafruit IR break-beam 3 mm: 3.3-5.5 V, emitter 10 mA @ 3.3 V / 20 mA @ 5 V,
  open-collector receiver output (sinks up to 100 mA), needs a pull-up.
- [DS18B20] Maxim DS18B20: VDD 3.0-5.5 V, IDD 1/1.5 mA, VIH 2.2 V (local power), VIL 0.8 V,
  any pin -0.5..+6.0 V, ~5 kΩ (4.7 kΩ) pull-up on DQ; TO-92 pins GND, DQ, VDD.
- [DHT22] Aosong AM2302: 3.3-6 V, measuring 1-1.5 mA, stand-by 40-50 µA; pins VDD, DATA, NULL, GND.
- [WS2812B] Worldsemi WS2812B: VDD 3.5-5.3 V, VIH 0.7 VDD, VIL 0.3 VDD.
  [NEOPIXEL] Adafruit NeoPixel Überguide: "up to 60 milliamps at maximum brightness white", 20 mA rule of thumb.
- [DOCS-PSU] Raspberry Pi docs: "The Camera Module requires 250mA".
Values marked "unverified" are typical-module assumptions (no maker datasheet for the breakout).
"""

from __future__ import annotations

from piforge.elec.model import PartDef, Pin, PinType, Supply

PI, GN, OD, IN, OUT, AN, P = (PinType.POWER_IN, PinType.GND, PinType.OPEN_DRAIN, PinType.INPUT, PinType.OUTPUT,
                              PinType.ANALOG, PinType.PASSIVE)
HDR = "Connector_PinHeader_2.54mm:PinHeader_1x{n:02d}_P2.54mm_Vertical"


def _i2c_pins(sda_n: str, scl_n: str, *, vih: float, vil: float, v_max: float | None = None,
              order: str = "scl_first") -> tuple[Pin, Pin]:
    sda = Pin("SDA", sda_n, OD, required=True, vih_ratio=vih, vil_ratio=vil, v_max=v_max, functions=("I2C_SDA",))
    scl = Pin("SCL", scl_n, IN, required=True, vih_ratio=vih, vil_ratio=vil, v_max=v_max, functions=("I2C_SCL",))
    return (scl, sda) if order == "scl_first" else (sda, scl)


DEFS: list[PartDef] = [
    PartDef(
        key="bme280_breakout", name="BME280 breakout (3.3 V, I2C)", category="sensor",
        pins=(Pin("VIN", "1", PI, required=True, aliases=("VCC", "VDD", "3V3")), Pin("GND", "2", GN, required=True))
        + _i2c_pins("4", "3", vih=0.8, vil=0.2),                              # src: [BME280] Table 32
        supply=Supply(1.71, 3.6, 0.0036, 0.714, pin="VIN"),                  # src: [BME280] VDD range, 3.6 µA, 714 µA peak
        i2c_addresses=(0x76, 0x77),                                           # src: [BME280] SDO strap
        params={"i2c_address": 0x76, "pullup_ohms": 10_000.0,
                # src: unverified (common GY-BME280 boards carry 10 kΩ pull-ups to VIN)
                "pullups": {"SDA": (10_000.0, "VIN"), "SCL": (10_000.0, "VIN")}},
        logic_from="VIN", sim={"twin": "bme280", "pins": {"sda": "SDA", "scl": "SCL"}},
        mech="bme280_breakout", footprint=HDR.format(n=4),
        datasheet="https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf",
        notes="3.3 V-only breakout (no regulator): abs max 4.25 V on VIN. Adafruit's version adds a regulator.",
    ),
    PartDef(
        key="ssd1306_096_i2c", name='SSD1306 0.96" 128x64 OLED (I2C)', category="display",
        pins=(Pin("GND", "1", GN, required=True), Pin("VCC", "2", PI, required=True, aliases=("VDD", "VIN")))
        # module logic runs from its on-board 3.3 V LDO -> fixed 3.3 V logic domain
        + tuple(Pin(p.name, p.number, p.type, voltage=3.3, required=True, vih_ratio=0.8, vil_ratio=0.2,  # src: [SSD1306]
                    functions=p.functions) for p in _i2c_pins("4", "3", vih=0.8, vil=0.2)),
        supply=Supply(3.3, 5.0, 10.0, 25.0, pin="VCC"),  # src: unverified (typical module: 3.3-5 V in, ~10-25 mA)
        i2c_addresses=(0x3C, 0x3D),                    # src: [SSD1306] SA0
        params={"i2c_address": 0x3C, "pullup_ohms": 4_700.0,
                "pullups": {"SDA": (4_700.0, 3.3), "SCL": (4_700.0, 3.3)}},  # src: unverified (4.7 kΩ to the 3.3 V LDO)
        sim={"twin": "ssd1306", "pins": {"sda": "SDA", "scl": "SCL"}}, mech="ssd1306_096_i2c",
        footprint=HDR.format(n=4), datasheet="https://cdn-shop.adafruit.com/datasheets/SSD1306.pdf",
        notes="Pin order GND-VCC-SCL-SDA on most boards; some swap GND/VCC - check the silkscreen.",
    ),
    PartDef(
        key="lcd1602_i2c", name="LCD 16x2 (HD44780) with PCF8574 I2C backpack", category="display",
        pins=(Pin("GND", "1", GN, required=True), Pin("VCC", "2", PI, required=True, aliases=("VDD", "5V")))
        + _i2c_pins("3", "4", vih=0.7, vil=0.3, order="sda_first"),            # src: [PCF8574] VIH 0.7 VCC
        supply=Supply(4.5, 5.5, 25.0, 60.0, pin="VCC"),  # src: unverified (5 V module, backlight ~20-40 mA)
        i2c_addresses=tuple(range(0x20, 0x28)) + tuple(range(0x38, 0x40)),  # src: [PCF8574] (+PCF8574A 0x38-0x3F)
        params={"i2c_address": 0x27, "pullup_ohms": 4_700.0,
                "pullups": {"SDA": (4_700.0, "VCC"), "SCL": (4_700.0, "VCC")}},  # src: unverified (FC-113 backpack)
        logic_from="VCC", sim={"twin": "lcd1602_pcf8574", "pins": {"sda": "SDA", "scl": "SCL"}},
        mech="lcd1602_i2c", footprint=HDR.format(n=4),
        datasheet="https://www.ti.com/lit/ds/symlink/pcf8574.pdf",
        notes="At 5 V the backpack pull-ups lift SDA/SCL above 3.3 V: remove them (pullup_ohms=None) or use "
              "a bss138_level_shifter. Default address 0x27 (PCF8574T) or 0x3F (PCF8574AT).",
    ),
    PartDef(
        key="ads1115_breakout", name="ADS1115 16-bit 4-channel ADC breakout (I2C)", category="adc",
        pins=(Pin("VDD", "1", PI, required=True, aliases=("VCC", "VIN")), Pin("GND", "2", GN, required=True))
        + _i2c_pins("4", "3", vih=0.7, vil=0.3, v_max=5.5)                    # src: [ADS1115] VIH 0.7 VDD .. 5.5 V
        + (Pin("ADDR", "5", IN, v_max=5.5), Pin("ALRT", "6", OD, aliases=("ALERT", "RDY")))
        + tuple(Pin(f"A{i}", str(7 + i), AN, aliases=(f"AIN{i}",)) for i in range(4)),
        supply=Supply(2.0, 5.5, 0.15, 0.3, pin="VDD"),   # src: [ADS1115] 2.0-5.5 V, 150 µA typ, 300 µA max
        i2c_addresses=(0x48, 0x49, 0x4A, 0x4B),          # src: [ADS1115] Table 7-2
        params={"i2c_address": 0x48, "pullup_ohms": 10_000.0,
                "pullups": {"SDA": (10_000.0, "VDD"), "SCL": (10_000.0, "VDD")}},  # src: unverified (Adafruit 1085: 10 kΩ)
        logic_from="VDD", footprint=HDR.format(n=10), datasheet="https://www.ti.com/lit/ds/symlink/ads1115.pdf",
        notes="ADDR to GND/VDD/SDA/SCL selects 0x48/0x49/0x4A/0x4B.",
    ),
    PartDef(
        key="pcf8574_module", name="PCF8574 8-bit I/O expander module (I2C)", category="interface",
        pins=(Pin("GND", "1", GN, required=True), Pin("VCC", "2", PI, required=True))
        + _i2c_pins("3", "4", vih=0.7, vil=0.3, order="sda_first")
        + (Pin("INT", "5", OD),)
        + tuple(Pin(f"P{i}", str(6 + i), PinType.BIDIR, vih_ratio=0.7, vil_ratio=0.3) for i in range(8)),
        supply=Supply(2.5, 6.0, 0.04, 0.1, pin="VCC"),  # src: [PCF8574] VCC 2.5-6 V, ICC 40/100 µA
        i2c_addresses=tuple(range(0x20, 0x28)),         # src: [PCF8574] 0100 A2A1A0
        params={"i2c_address": 0x20, "pullup_ohms": 10_000.0,
                "pullups": {"SDA": (10_000.0, "VCC"), "SCL": (10_000.0, "VCC")}},  # src: unverified (typical module)
        logic_from="VCC", footprint=HDR.format(n=13), datasheet="https://www.ti.com/lit/ds/symlink/pcf8574.pdf",
        notes="Quasi-bidirectional ports: strong sink, weak (~100 µA) pull-up.",
    ),
    PartDef(
        key="hx711_module", name="HX711 24-bit load-cell amplifier module", category="adc",
        pins=(Pin("GND", "1", GN, required=True), Pin("DT", "2", OUT, required=True, aliases=("DOUT", "DAT")),
              Pin("SCK", "3", IN, required=True, aliases=("PD_SCK", "CLK")),
              Pin("VCC", "4", PI, required=True, aliases=("VDD",)),
              Pin("E+", "5", P, aliases=("RED",)), Pin("E-", "6", P, aliases=("BLK",)),
              Pin("A-", "7", P, aliases=("WHT",)), Pin("A+", "8", P, aliases=("GRN",)),
              Pin("B-", "9", P), Pin("B+", "10", P)),
        # src: [HX711] < 1.5 mA + bridge excitation ~4.3 V / 1 kΩ (unverified typical 1 kΩ bar cell)
        supply=Supply(2.6, 5.5, 5.8, 6.5, pin="VCC"),
        logic_from="VCC", sim={"twin": "hx711", "pins": {"dout": "DT", "sck": "SCK"}}, mech="hx711_module",
        footprint=HDR.format(n=10), datasheet="https://cdn.sparkfun.com/datasheets/Sensors/ForceFlex/hx711_english.pdf",
        notes="DT swings to VCC: power the module from 3V3 when DT goes straight to a GPIO.",
    ),
    PartDef(
        key="load_cell", name="Bar load cell 5 kg (full bridge)", category="sensor",
        pins=(Pin("E+", "1", P, aliases=("RED", "EXC+")), Pin("E-", "2", P, aliases=("BLACK", "EXC-")),
              Pin("S-", "3", P, aliases=("A-", "WHITE", "SIG-")), Pin("S+", "4", P, aliases=("A+", "GREEN", "SIG+"))),
        params={"capacity_kg": 5.0, "bridge_ohms": 1000.0, "rated_output_mv_v": 1.0},  # src: unverified (typical bar cell)
        mech="load_cell_5kg_bar", ref_prefix="U", footprint=HDR.format(n=4),
        datasheet="typical 5 kg aluminium bar load cell (unverified generic data)",
        notes="Wire red E+, black E-, white A-, green A+ to the HX711.",
    ),
    PartDef(
        key="hcsr04", name="HC-SR04 ultrasonic distance sensor", category="sensor",
        pins=(Pin("VCC", "1", PI, required=True), Pin("TRIG", "2", IN, required=True, vih=2.0, aliases=("TRIGGER",)),
              Pin("ECHO", "3", OUT, required=True), Pin("GND", "4", GN, required=True)),
        # src: [HCSR04] 5 V, 15 mA; 4.5-5.5 V window and TTL VIH 2.0 V are unverified assumptions
        supply=Supply(4.5, 5.5, 15.0, 15.0, pin="VCC"),
        logic_from="VCC", sim={"twin": "hcsr04", "pins": {"trigger": "TRIG", "echo": "ECHO"}}, mech="hcsr04",
        footprint=HDR.format(n=4), datasheet="https://cdn.sparkfun.com/datasheets/Sensors/Proximity/HCSR04.pdf",
        notes="ECHO is a 5 V output: use a 1 kΩ/2 kΩ divider or a level shifter into a GPIO.",
    ),
    PartDef(
        key="pir_hcsr501", name="HC-SR501 PIR motion sensor", category="sensor",
        pins=(Pin("VCC", "1", PI, required=True), Pin("OUT", "2", OUT, required=True, voltage=3.3),  # src: [HCSR501] 3.3 V high
              Pin("GND", "3", GN, required=True)),
        supply=Supply(4.5, 20.0, 0.05, 0.065, pin="VCC"),  # src: [HCSR501] 4.5-20 V, < 50 µA (65 µA listed elsewhere)
        sim={"twin": "pir", "pins": {"pin": "OUT"}}, mech="pir_hcsr501", footprint=HDR.format(n=3),
        datasheet="https://www.sigmaelectronica.net/manuals/HC-SR501.pdf",
        notes="Output is 3.3 V (on-board regulator): safe for a GPIO even with VCC = 5 V.",
    ),
    PartDef(
        key="ir_breakbeam_rx", name="IR break-beam receiver 3 mm (Adafruit 2167)", category="sensor",
        pins=(Pin("VCC", "1", PI, required=True, aliases=("RED",)), Pin("GND", "2", GN, required=True, aliases=("BLACK",)),
              Pin("OUT", "3", OD, required=True, i_max_ma=100.0, aliases=("WHITE", "SIG"))),  # src: [ADA2167]
        supply=Supply(3.3, 5.5, 0.5, 1.0, pin="VCC"),  # src: [ADA2167] 3.3-5.5 V; receiver current unverified
        sim={"twin": "ir_breakbeam", "pins": {"pin": "OUT"}}, mech="ir_breakbeam_3mm", footprint=HDR.format(n=3),
        datasheet="https://www.adafruit.com/product/2167",
        notes="Open-collector output: enable the GPIO pull-up or add 10 kΩ to VCC.",
    ),
    PartDef(
        key="ir_breakbeam_tx", name="IR break-beam emitter 3 mm (Adafruit 2167)", category="sensor",
        pins=(Pin("VCC", "1", PI, required=True, aliases=("RED",)), Pin("GND", "2", GN, required=True, aliases=("BLACK",))),
        supply=Supply(3.3, 5.5, 10.0, 20.0, pin="VCC"),  # src: [ADA2167] 10 mA @ 3.3 V, 20 mA @ 5 V
        mech="ir_breakbeam_3mm", footprint=HDR.format(n=2), datasheet="https://www.adafruit.com/product/2167",
    ),
    PartDef(
        key="tcrt5000_module", name="TCRT5000 reflective IR sensor module (LM393)", category="sensor",
        pins=(Pin("VCC", "1", PI, required=True), Pin("GND", "2", GN, required=True),
              Pin("D0", "3", OD, aliases=("DO", "OUT")), Pin("A0", "4", AN, aliases=("AO",))),
        supply=Supply(3.3, 5.0, 15.0, 20.0, pin="VCC"),  # src: unverified (module listings: 3.3-5 V, 15 mA)
        params={"pullups": {"D0": (10_000.0, "VCC")}},   # src: unverified (LM393 output pull-up on module)
        sim={"twin": "ir_breakbeam", "pins": {"pin": "D0"}}, mech="tcrt5000_module", footprint=HDR.format(n=4),
        datasheet="https://www.vishay.com/docs/83760/tcrt5000.pdf (sensor); module schematic unverified",
        notes="D0 is pulled up to VCC: power from 3V3 when D0 goes to a GPIO.",
    ),
    PartDef(
        key="rotary_encoder_ky040", name="KY-040 rotary encoder with push switch", category="sensor",
        pins=(Pin("CLK", "1", OD, required=True, aliases=("A",)), Pin("DT", "2", OD, required=True, aliases=("B",)),
              Pin("SW", "3", OD, aliases=("BUTTON",)), Pin("+", "4", PI, required=True, aliases=("VCC",)),
              Pin("GND", "5", GN, required=True)),
        supply=Supply(3.0, 5.5, 0.0, 1.0, pin="+"),  # src: unverified (only the pull-ups draw current)
        # src: KY-040 schematics (unverified): 10 kΩ pull-ups on CLK and DT; SW pull-up often unpopulated
        params={"pullups": {"CLK": (10_000.0, "+"), "DT": (10_000.0, "+")}},
        sim={"twin": "rotary_encoder", "pins": {"a": "CLK", "b": "DT", "sw": "SW"}}, mech="rotary_encoder_ky040",
        footprint=HDR.format(n=5), datasheet="KY-040 module (no maker datasheet)",
        notes="Power '+' from 3V3: the pull-ups go to '+'. SW needs a GPIO pull-up.",
    ),
    PartDef(
        key="ds18b20", name="DS18B20 1-Wire temperature sensor (TO-92)", category="sensor",
        pins=(Pin("GND", "1", GN, required=True),
              Pin("DQ", "2", OD, required=True, vih=2.2, vil=0.8, v_max=6.0, functions=("ONEWIRE",), aliases=("DATA",)),
              Pin("VDD", "3", PI, aliases=("VCC",))),
        supply=Supply(3.0, 5.5, 1.0, 1.5, pin="VDD"),  # src: [DS18B20]
        sim={"twin": "ds18b20", "pins": {"pin": "DQ"}}, mech="ds18b20_to92", ref_prefix="U",
        footprint="Package_TO_SOT_THT:TO-92_Inline",
        datasheet="https://www.analog.com/media/en/technical-documentation/data-sheets/DS18B20.pdf",
        notes="Needs a 4.7 kΩ pull-up from DQ to VDD (default 1-Wire pin GPIO4).",
    ),
    PartDef(
        key="dht22", name="DHT22 / AM2302 humidity + temperature sensor", category="sensor",
        pins=(Pin("VCC", "1", PI, required=True, aliases=("VDD",)), Pin("DATA", "2", OD, required=True, aliases=("SDA", "OUT")),
              Pin("NC", "3", PinType.NC), Pin("GND", "4", GN, required=True)),
        supply=Supply(3.3, 6.0, 1.0, 1.5, pin="VCC"),  # src: [DHT22] 3.3-6 V, measuring 1-1.5 mA
        logic_from="VCC", sim={"twin": "dht22", "pins": {"pin": "DATA"}}, mech="dht22", footprint=HDR.format(n=4),
        datasheet="https://www.sparkfun.com/datasheets/Sensors/Temperature/DHT22.pdf",
        notes="Needs a pull-up on DATA (datasheet 5.1 kΩ, Adafruit 10 kΩ) to the sensor VCC; use 3V3 for a GPIO.",
    ),
]


def _ws2812(key: str, name: str, count: int, mech: str | None) -> PartDef:
    return PartDef(
        key=key, name=name, category="light",
        pins=(Pin("5V", "1", PI, required=True, aliases=("VCC", "VDD", "+5V")),
              Pin("DIN", "2", IN, required=True, vih_ratio=0.7, vil_ratio=0.3, aliases=("DI", "IN")),  # src: [WS2812B]
              Pin("GND", "3", GN, required=True), Pin("DOUT", "4", OUT, aliases=("DO",))),
        # per pixel: src [WS2812B] VDD 3.5-5.3 V; [NEOPIXEL] 20 mA rule of thumb / 60 mA full white
        supply=Supply(3.5, 5.3, 20.0, 60.0, pin="5V"),
        params={"count": count, "supply_per_unit": True},
        logic_from="5V", sim={"twin": "neopixel", "pins": {"pin": "DIN"}}, mech=mech,
        footprint=HDR.format(n=3), datasheet="https://cdn-shop.adafruit.com/datasheets/WS2812B.pdf",
        notes="DIN needs >= 0.7 x VDD: at 5 V a 3.3 V GPIO is marginal (use a 74AHCT125 or power the first pixel "
              "lower). Add 300-500 Ω in series with DIN and 1000 µF across the supply.",
    )


DEFS += [
    _ws2812("ws2812b_strip", "WS2812B addressable LED strip", 8, None),
    _ws2812("ws2812b_ring_12", "WS2812B 12-LED ring", 12, "ws2812_ring_12"),
    PartDef(
        key="pi_camera_v3", name="Raspberry Pi Camera Module 3 (CSI)", category="camera", pins=(),
        params={"interface": "csi", "current_ma": 250.0},  # src: [DOCS-PSU] "The Camera Module requires 250mA"
        features=("csi",), sim={"twin": "camera"}, mech="pi_camera_v3",
        datasheet="https://datasheets.raspberrypi.com/camera/camera-module-3-product-brief.pdf",
        notes="Connects with the FPC ribbon to the CSI/CAM port: no header pins. Its current is budgeted on the "
              "Pi's 5 V input.",
    ),
]
