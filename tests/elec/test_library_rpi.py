"""Golden data for the Raspberry Pi boards: 40-pin header, alt functions, electrical limits.

Sources (see src/piforge/elec/library/rpi.py for the per-number citations):
- Raspberry Pi documentation, "GPIO and the 40-pin header" + "GPIO pads control" + "Power supply"
  (github.com/raspberrypi/documentation, asciidoc/computers/raspberry-pi/*.adoc).
- Raspberry Pi 4 Model B datasheet rel. 1.1 (2024), Table 3 (DC characteristics) and Table 5 (alt functions).
- BCM2835 ARM Peripherals Table 6-31; RP1 Peripherals Table 4; CM5 datasheet Tables 7-8.
"""

from __future__ import annotations

import pytest

from piforge.elec.library import get_def, list_defs
from piforge.elec.model import PinType

MODELS = ["rpi5", "rpi4b", "rpi3bp", "rpizero2w"]

# BCM -> physical header pin (official pinout diagram)
GPIO_POS = {
    0: 27, 1: 28, 2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23, 12: 32, 13: 33,
    14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16, 24: 18, 25: 22,
    26: 37, 27: 13,
}


def _header(key):
    d = get_def(key)
    return d, {int(p.number): p for p in d.pins if p.number.isdigit()}


@pytest.mark.parametrize("key", MODELS)
def test_header_table(key):
    d, by_num = _header(key)
    assert d.category == "board"
    assert sorted(by_num) == list(range(1, 41)), "exactly the 40 header pins"
    kinds = lambda t: sorted(n for n, p in by_num.items() if p.type == t)  # noqa: E731
    assert kinds(PinType.GND) == [6, 9, 14, 20, 25, 30, 34, 39]
    assert sorted(n for n, p in by_num.items() if p.name == "5V") == [2, 4]
    assert sorted(n for n, p in by_num.items() if p.name == "3V3") == [1, 17]
    assert all(p.type == PinType.POWER_OUT for p in by_num.values() if p.name in ("5V", "3V3"))
    for bcm, phys in GPIO_POS.items():
        p = by_num[phys]
        assert p.name == f"GPIO{bcm}", (key, bcm, phys)
        assert f"BCM{bcm}" in p.functions and p.type == PinType.BIDIR
    assert sum(1 for p in by_num.values() if p.name.startswith("GPIO")) == 28
    # alternate functions (all four boards)
    assert "I2C1_SDA" in by_num[3].functions and "I2C1_SCL" in by_num[5].functions
    assert "PWM0" in by_num[12].functions
    assert "SPI0_MOSI" in by_num[19].functions and "SPI0_MISO" in by_num[21].functions
    assert "SPI0_SCLK" in by_num[23].functions and "SPI0_CE0" in by_num[24].functions
    assert "SPI0_CE1" in by_num[26].functions
    assert "UART0_TX" in by_num[8].functions and "UART0_RX" in by_num[10].functions
    assert "PCM_CLK" in by_num[12].functions and "PCM_DOUT" in by_num[40].functions
    assert "I2C0_SDA" in by_num[27].functions and "ID_SD" in by_num[27].aliases


@pytest.mark.parametrize("key", MODELS)
def test_gpio_electrical_data(key):
    d, by_num = _header(key)
    for p in by_num.values():
        if not p.name.startswith("GPIO"):
            continue
        assert p.voltage == pytest.approx(3.3)
        assert p.vil is not None and p.vih is not None and p.vil < p.vih < 3.3
        assert p.vol is not None and p.voh is not None and p.vol < 0.5 and 2.5 < p.voh <= 3.3
        assert 3.3 < p.v_max < 4.0, "not 5 V tolerant"
        assert p.i_max_ma == pytest.approx(16.0)  # docs: "each pin can individually draw up to 16mA"
    assert d.params["gpio_total_ma"] == pytest.approx(50.0)
    assert d.params["pullups"]["GPIO2"][0] == pytest.approx(1800.0)  # fixed 1.8 kΩ I2C pull-ups


def test_soc_specific_thresholds():
    # docs table for BCM2835/6/7 & RP3A0: VIL 0.9 V max, VIH 1.6 V min, VOH 3.0 V min @ 2 mA
    for key in ("rpi3bp", "rpizero2w"):
        p = get_def(key).pins[2]  # pin 3
        assert (p.vil, p.vih, p.voh) == (0.9, 1.6, 3.0)
    # Pi 4 datasheet Table 3: VIL 0.8, VIH 2.0, VOH = VDD_IO - 0.4
    p4 = _header("rpi4b")[1][3]
    assert (p4.vil, p4.vih) == (0.8, 2.0) and p4.voh == pytest.approx(2.9)
    # CM5 datasheet (RP1, same GPIO as the Pi 5 header): abs max VGPIO_VREF + 0.5 = 3.8 V
    p5 = _header("rpi5")[1][3]
    assert (p5.vil, p5.vih) == (0.8, 2.0) and p5.v_max == pytest.approx(3.8)


def test_alt_function_differences():
    _, p4 = _header("rpi4b")
    _, p5 = _header("rpi5")
    _, p3 = _header("rpi3bp")
    assert "UART3_TX" in p4[7].functions          # BCM2711 extra UARTs (GPIO4 ALT4 TXD3)
    assert "UART3_TX" not in p3[7].functions      # not on BCM2837
    assert "PWM0_2" in p5[12].functions           # RP1: GPIO18 = PWM0 channel 2
    assert "PWM0_0" in p4[12].functions and "PWM0_0" in p4[32].functions  # BCM: 12 and 18 share ch0
    assert "SPI1_CE0" in p4[12].functions and "SPI1_CE0" in p5[12].functions


def test_power_data_per_model():
    # Raspberry Pi docs "Typical power requirements" table
    exp = {"rpi5": (5000, 800), "rpi4b": (3000, 600), "rpi3bp": (2500, 500), "rpizero2w": (2000, 350)}
    for key, (psu, typ) in exp.items():
        p = get_def(key).params
        assert p["psu_recommended_ma"] == psu and p["typical_ma"] == typ
        assert p["max_ma"] > p["typical_ma"] > p["idle_ma"] > 0
        assert p["rail_3v3_budget_ma"] > 0
    assert get_def("rpi5").params["usb_max_ma"] == 1600
    assert get_def("rpi5").params["usb_max_ma_3a"] == 600


def test_psus():
    a, b = get_def("psu_usbc_5v3a"), get_def("psu_usbc_5v5a")
    assert a.params["i_max_ma"] == 3000 and b.params["i_max_ma"] == 5000
    assert a.params["v_out"] == pytest.approx(5.1) and b.params["v_out"] == pytest.approx(5.1)
    assert all(p.category == "power" for p in (a, b))


def test_boards_listed():
    keys = {d.key for d in list_defs("board")}
    assert set(MODELS) <= keys
