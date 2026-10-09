"""Part library: required keys, internal consistency, cross-subsystem naming contracts."""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError, PiForgeError
from piforge.elec.library import get_def, list_defs, register
from piforge.elec.model import Circuit, PartDef, Pin, PinType, Supply

REQUIRED_KEYS = """
rpi5 rpi4b rpi3bp rpizero2w psu_usbc_5v3a psu_usbc_5v5a resistor capacitor led rgb_led_cc
diode_1n4148 diode_1n4007 schottky_1n5819 npn_2n2222 nmos_2n7000 nmos_ao3400 nmos_irlz44n nmos_irf540n
pushbutton switch_spst limit_switch bme280_breakout ssd1306_096_i2c lcd1602_i2c mcp3008 ads1115_breakout
hx711_module load_cell hcsr04 pir_hcsr501 ir_breakbeam_rx ir_breakbeam_tx tcrt5000_module sg90_servo
mg996r_servo stepper_28byj48 uln2003_board tb6612_breakout l298n_module dc_motor relay_1ch_5v
buzzer_active buzzer_passive ws2812b_strip rotary_encoder_ky040 ds18b20 dht22 pcf8574_module
bss138_level_shifter lm2596_module fan_5v pi_camera_v3
""".split()

# Binding contract with the digital twin (Task 6 DEVICE_TYPES)
TWIN_TYPES = set("""
button switch limit_switch ir_breakbeam pir hcsr04 led rgb_led buzzer relay servo stepper_28byj48
dc_motor bme280 ssd1306 lcd1602_pcf8574 mcp3008 hx711 rotary_encoder neopixel camera ds18b20 dht22
shift_register_74hc595
""".split())

# Binding contract with the mechanical module library (Task 3)
MECH_KEYS = set("""
ssd1306_096_i2c lcd1602_i2c ili9341_28_spi pi_camera_v3 hcsr04 pir_hcsr501 tact_6x6 pushbutton_12mm
led_5mm buzzer_12mm rotary_encoder_ky040 bme280_breakout mcp3008_dip16 hx711_module load_cell_5kg_bar
sg90_servo mg996r_servo stepper_28byj48 uln2003_board tb6612_breakout l298n_module relay_1ch_5v fan_30mm
lm2596_module ir_breakbeam_3mm tcrt5000_module ws2812_ring_12 ds18b20_to92 dht22
""".split())

# SPICE model keys published by Task 5 (piforge.spice.models.MODELS)
SPICE_MODELS = set("""
led_red led_green led_blue led_white led_ir d1n4148 d1n4007 d1n5819 zener_3v3 q2n2222 q2n3904
nmos_2n7000 nmos_bss138 nmos_ao3400 nmos_irlz44n nmos_irf540n sw_ideal gpio_out relay_coil_5v
dc_motor_small psu_cable
""".split())


def test_required_keys_present():
    have = {d.key for d in list_defs()}
    missing = sorted(set(REQUIRED_KEYS) - have)
    assert not missing, missing
    assert len(have) >= 40


@pytest.mark.parametrize("key", sorted({d.key for d in list_defs()}))
def test_definition_consistent(key):
    d = get_def(key)
    assert d.key == key and d.name and d.category
    numbers = [p.number for p in d.pins]
    assert len(numbers) == len(set(numbers)), "pin numbers unique"
    names = [p.name for p in d.pins]
    dupes = {n for n in names if names.count(n) > 1}
    for n in dupes:  # duplicated names are only allowed for tied power/ground pins
        assert all(p.type in (PinType.GND, PinType.POWER_OUT, PinType.POWER_IN) for p in d.pins if p.name == n)
    pin_names = set(names)
    if d.supply is not None:
        assert d.supply.pin in pin_names
        assert 0 < d.supply.v_min <= d.supply.v_max
        assert 0 <= d.supply.i_typ_ma <= d.supply.i_max_ma
    if d.logic_from is not None:
        assert d.logic_from in pin_names
    if d.i2c_addresses:
        assert d.params.get("i2c_address", d.i2c_addresses[0]) in d.i2c_addresses
        assert any("I2C_SDA" in p.functions for p in d.pins)
    twin = d.sim.get("twin")
    assert twin is None or twin in TWIN_TYPES, twin
    spice = d.sim.get("spice")
    assert spice is None or spice in SPICE_MODELS, spice
    assert d.mech is None or d.mech in MECH_KEYS, d.mech
    for role, spec in d.sim.get("pins", {}).items():
        for name in (spec if isinstance(spec, tuple) else (spec,)):
            assert name.split(":")[0] in pin_names, (role, name)
    if d.category not in ("passive",):
        assert d.datasheet or d.notes, "every non-generic part names its source"


def test_twin_and_mech_links():
    assert get_def("pushbutton").sim["twin"] == "button"
    assert get_def("stepper_28byj48").sim["twin"] == "stepper_28byj48"
    assert "twin" not in get_def("uln2003_board").sim  # the motor carries the twin type
    assert get_def("dc_motor").sim["twin"] == "dc_motor"
    assert "twin" not in get_def("l298n_module").sim
    assert get_def("ws2812b_strip").sim["twin"] == "neopixel"
    assert get_def("lcd1602_i2c").sim["twin"] == "lcd1602_pcf8574"
    assert get_def("ssd1306_096_i2c").mech == "ssd1306_096_i2c"
    assert get_def("led").mech == "led_5mm" and get_def("led").sim["spice"] == "led_red"
    assert "twin" not in get_def("resistor").sim


def test_camera_has_no_header_pins():
    cam = get_def("pi_camera_v3")
    assert cam.pins == () and cam.sim["twin"] == "camera" and cam.mech == "pi_camera_v3"


def test_datasheet_numbers():
    # spot checks against the cited datasheets
    assert get_def("hcsr04").supply.i_typ_ma == pytest.approx(15.0)       # HC-SR04: 15 mA
    assert get_def("mg996r_servo").supply.i_max_ma == pytest.approx(2500)  # MG996R: 2.5 A stall
    assert get_def("bme280_breakout").supply.v_max == pytest.approx(3.6)   # BME280 VDD 1.71-3.6 V
    assert get_def("bme280_breakout").i2c_addresses == (0x76, 0x77)
    assert get_def("ads1115_breakout").i2c_addresses == (0x48, 0x49, 0x4A, 0x4B)
    assert get_def("mcp3008").supply.v_min == pytest.approx(2.7)
    assert get_def("ws2812b_strip").pins[1].vih_ratio == pytest.approx(0.7)
    assert get_def("nmos_irf540n").params["vgs_rated"] == pytest.approx(10.0)
    assert get_def("nmos_ao3400").params["vgs_rated"] == pytest.approx(2.5)
    assert get_def("relay_srd05vdc").params["load_ohms"] == pytest.approx(70.0)
    assert get_def("ds18b20").supply.v_min == pytest.approx(3.0)


def test_features():
    assert {"driver", "flyback"} <= set(get_def("relay_1ch_5v").features)
    assert {"inductive", "motor"} <= set(get_def("dc_motor").features)
    assert "inductive" in get_def("relay_srd05vdc").features
    assert "clamped_outputs" in get_def("uln2003_board").features


def test_list_defs_by_category():
    leds = list_defs("led")
    assert {d.key for d in leds} >= {"led", "rgb_led_cc"}
    assert all(d.category == "led" for d in leds)
    assert list_defs("no-such-category") == []
    keys = [d.key for d in list_defs()]
    assert keys == sorted(keys)


def test_get_def_unknown_suggests():
    with pytest.raises(NotFoundError) as ei:
        get_def("bme280")
    assert "bme280_breakout" in str(ei.value)
    assert get_def("pi4") is get_def("rpi4b")  # convenience alias


def test_register_custom_part():
    d = PartDef(key="test_widget_xyz", name="Widget", category="sensor",
                pins=(Pin("VCC", "1", PinType.POWER_IN), Pin("GND", "2", PinType.GND)),
                supply=Supply(3.0, 5.5, 1.0, 1.0), datasheet="n/a")
    register(d)
    assert get_def("test_widget_xyz") is d
    c = Circuit("x")
    assert c.add("test_widget_xyz").definition is d
    register(d)  # identical re-registration is harmless
    other = PartDef(key="test_widget_xyz", name="Other", category="sensor", pins=())
    with pytest.raises(PiForgeError):
        register(other)
    register(other, replace=True)
    assert get_def("test_widget_xyz") is other
    with pytest.raises(PiForgeError):  # bad definitions are rejected at registration
        register(PartDef(key="bad_widget", name="Bad", category="sensor",
                         pins=(Pin("A", "1", PinType.INPUT), Pin("B", "1", PinType.INPUT))))
