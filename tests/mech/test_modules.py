"""Module mechanical models (displays, sensors, buttons, motors…): data sanity + CAD validity."""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError, ValidationError

REQUIRED = """ssd1306_096_i2c sh1106_13_i2c lcd1602_i2c ili9341_28_spi pi_camera_v3 hcsr04 pir_hcsr501
tact_6x6 pushbutton_12mm pushbutton_16mm_panel led_5mm led_3mm buzzer_12mm rotary_encoder_ky040
pot_rv09 bme280_breakout mcp3008_dip16 hx711_module load_cell_5kg_bar sg90_servo mg996r_servo
stepper_28byj48 uln2003_board nema17 tb6612_breakout l298n_module relay_1ch_5v fan_30mm fan_40mm
lm2596_module mp1584_module dc_jack_panel_55x21 usb_c_breakout rocker_switch_kcd1 ir_breakbeam_3mm
tcrt5000_module ws2812_ring_12 perfboard_50x70 ds18b20_to92 dht22""".split()


def test_required_keys_present():
    from piforge.mech.modules import MODULES

    missing = [k for k in REQUIRED if k not in MODULES]
    assert not missing, missing


def test_module_data_sane():
    from piforge.mech.modules import MODULES, PANEL_MOUNTS

    for key, m in MODULES.items():
        assert m.key == key
        assert m.name and m.source
        assert m.panel_mount in PANEL_MOUNTS, key
        assert m.front_height > 0, key
        assert m.components or m.pcb, key
        for name, kind, center, size in m.components:
            assert kind in ("box", "cyl"), (key, name)
            assert len(center) == 3 and len(size) == (3 if kind == "box" else 2), (key, name)
            assert all(s > 0 for s in size), (key, name)
        if m.window is not None:
            shape, center, size = m.window
            assert shape in ("rect", "circle") and len(center) == 2
            assert len(size) == (2 if shape == "rect" else 1)
        if m.pcb is not None:  # holes inside the PCB, clear of its edge
            pl, pw, _ = m.pcb
            for x, y in m.holes:
                assert abs(x) + m.hole_d / 2 < pl / 2, (key, x, y)
                assert abs(y) + m.hole_d / 2 < pw / 2, (key, x, y)
        lo, hi = m.bounds()
        assert all(h > l for l, h in zip(lo, hi))


def test_elec_keys_exist():
    from piforge.elec.library import get_def
    from piforge.mech.modules import MODULES

    for m in MODULES.values():
        if m.elec_key is not None:
            get_def(m.elec_key)  # raises if unknown


def test_get_module_lookup():
    from piforge.mech.modules import ModuleModel, get_module

    m = get_module("SSD1306_096_I2C")
    assert m.key == "ssd1306_096_i2c"
    assert get_module(m) is m
    assert isinstance(get_module("oled"), ModuleModel)  # alias
    with pytest.raises(NotFoundError):
        get_module("ssd1307_nope")


def test_module_known_dims():
    from piforge.mech.modules import get_module

    oled = get_module("ssd1306_096_i2c")
    assert oled.pcb[0] == pytest.approx(27.3, abs=1) and oled.pcb[1] == pytest.approx(27.8, abs=1)

    sg90 = get_module("sg90_servo")
    body = sg90.component("body")
    assert body[3][0] == pytest.approx(22.8, abs=0.5) and body[3][1] == pytest.approx(12.2, abs=0.5)
    tabs = sg90.component("tabs")
    assert tabs[3][0] == pytest.approx(32.2, abs=0.3)

    byj = get_module("stepper_28byj48")
    assert byj.component("body")[3][0] == pytest.approx(28.0)
    xs = sorted(x for x, _ in byj.holes)
    assert xs[-1] - xs[0] == pytest.approx(35.0)

    nema = get_module("nema17")
    assert nema.component("body")[3][0] == pytest.approx(42.3)
    xs = sorted({x for x, _ in nema.holes})
    assert xs[-1] - xs[0] == pytest.approx(31.0)


def test_panel_cutout_requires_window():
    from piforge.mech.modules import get_module

    with pytest.raises(ValidationError):
        get_module("perfboard_50x70").panel_cutout(2.0)


@pytest.mark.slow
def test_every_module_valid():
    from piforge.mech.export import to_trimesh
    from piforge.mech.modules import MODULES

    for key, m in MODULES.items():
        shape = m.shape()
        assert shape.is_valid, key
        bb = shape.bounding_box()
        lo, hi = m.bounds()
        assert bb.min.Z == pytest.approx(lo[2], abs=0.05), key
        assert bb.max.Z == pytest.approx(hi[2], abs=0.05), key
        if m.window is not None:
            cut = m.panel_cutout(2.0)
            assert cut.is_valid and cut.volume > 0, key
            assert to_trimesh(cut).is_watertight, key
        spec = m.part()
        assert spec.kind == "reference"


@pytest.mark.slow
def test_panel_cutout_position():
    from piforge.mech.modules import get_module

    oled = get_module("ssd1306_096_i2c")
    cut = oled.panel_cutout(2.0, clearance=0.3)
    bb = cut.bounding_box()
    w, h = oled.window[2]
    assert bb.max.X - bb.min.X == pytest.approx(w + 0.6, abs=1e-3)
    assert bb.max.Y - bb.min.Y == pytest.approx(h + 0.6, abs=1e-3)
    # behind-panel module: the cutter starts at the front face and spans the panel thickness
    assert bb.min.Z < oled.front_height < bb.max.Z
    assert bb.max.Z == pytest.approx(oled.front_height + 2.0, abs=0.05)

    btn = get_module("pushbutton_16mm_panel")  # through-panel: z = 0 is the outer panel face
    bb = btn.panel_cutout(3.0).bounding_box()
    assert bb.max.Z == pytest.approx(0.0, abs=0.05) and bb.min.Z == pytest.approx(-3.0, abs=0.05)
