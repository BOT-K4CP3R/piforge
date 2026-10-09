"""Wiring table (physical pins, colours) and wiring diagram (SVG + PNG)."""

from __future__ import annotations

from piforge.elec.model import Circuit
from piforge.elec.wiring import WireRow, wiring_diagram, wiring_markdown, wiring_table


def _demo():
    c = Circuit("demo")
    pi = c.add("rpi4b")
    r = c.add("resistor", value=330)
    led = c.add("led")
    c.connect(pi["GPIO17"], r["1"])
    c.connect(r["2"], led["A"])
    c.connect(led["K"], pi["GND"])
    bme = c.add("bme280_breakout")
    c.connect(pi["3V3"], bme["VIN"])
    c.connect(pi["GND"], bme["GND"])
    c.connect(pi["SDA1"], bme["SDA"])
    c.connect(pi["SCL1"], bme["SCL"])
    us = c.add("hcsr04")
    r1, r2 = c.add("resistor", value=1000), c.add("resistor", value=2000)
    c.connect(pi["5V"], us["VCC"])
    c.connect(pi["GND"], us["GND"])
    c.connect(pi["GPIO23"], us["TRIG"])
    c.connect(us["ECHO"], r1["1"])
    c.connect(r1["2"], r2["1"], pi["GPIO24"])
    c.connect(r2["2"], pi["GND"])
    btn = c.add("pushbutton")
    c.connect(pi["GPIO27"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    servo = c.add("sg90_servo")
    c.connect(pi["5V"], servo["V+"])
    c.connect(pi["GND"], servo["GND"])
    c.connect(pi["GPIO18"], servo["SIG"])
    return c, pi


def test_wiring_table_physical_pins():
    c, pi = _demo()
    rows = wiring_table(c)
    assert rows and all(isinstance(r, WireRow) for r in rows)
    gpio17 = [r for r in rows if r.a_ref == "U1" and r.a_pin == "GPIO17"]
    assert len(gpio17) == 1 and gpio17[0].a_phys == "11" and gpio17[0].b_ref == "R1"
    assert gpio17[0].b_phys == "1"
    by_net = {}
    for r in rows:
        by_net.setdefault(r.net, []).append(r)
    assert all(r.color == "red" for r in by_net["5V"])
    assert all(r.color == "orange" for r in by_net["3V3"])
    assert all(r.color == "black" for r in by_net["GND"])
    # every GND wire starts at a real GND header pin; LED cathode goes to the first one (pin 6)
    assert {r.a_phys for r in by_net["GND"]} <= {"6", "9", "14", "20", "25", "30", "34", "39"}
    led_k = next(r for r in by_net["GND"] if r.b_ref == "D1")
    assert led_k.a_phys == "6"
    # I2C lines are labelled and coloured by convention
    sda = next(r for r in rows if r.a_pin == "GPIO2")
    assert sda.a_phys == "3" and "SDA" in sda.signal and sda.color == "blue"
    # nets without the Pi are stars from a module pin (R1.2 -> D1.A)
    r1_led = next(r for r in rows if {r.a_ref, r.b_ref} == {"R1", "D1"})
    assert r1_led.a_ref == "R1" and r1_led.b_pin == "A"
    md = wiring_markdown(rows)
    assert md.splitlines()[0].startswith("|") and "GPIO17" in md and "| 11 |" in md


def test_wiring_diagram_files(spaced_tmp):
    c, _ = _demo()
    svg = spaced_tmp / "wiring.svg"
    png = spaced_tmp / "wiring.png"
    wiring_diagram(c, svg, png)
    assert svg.exists() and svg.read_text(encoding="utf-8").lstrip().startswith(("<?xml", "<svg"))
    assert png.exists() and png.stat().st_size > 10_000
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_wiring_diagram_svg_only(tmp_path):
    c, _ = _demo()
    wiring_diagram(c, tmp_path / "only.svg")
    assert (tmp_path / "only.svg").stat().st_size > 5_000


def test_wiring_diagram_with_loose_parts_and_driver_chain(tmp_path):
    c = Circuit("chain")
    pi = c.add("rpi5")
    c.add("pi_camera_v3")       # no header pins
    c.add("psu_usbc_5v5a")      # plugged into USB-C, not wired
    drv = c.add("uln2003_board")
    m = c.add("stepper_28byj48")
    c.connect(pi["5V"], drv["+"])
    c.connect(pi["GND"], drv["-"])
    for i, (g, o) in enumerate(zip((5, 6, 13, 19), "ABCD"), start=1):
        c.connect(pi[f"GPIO{g}"], drv[f"IN{i}"])
        c.connect(drv[o], m[o])
    c.connect(drv["M+"], m["COM"])
    rows = wiring_table(c)
    assert any(r.a_ref == drv.ref and r.b_ref == m.ref for r in rows)  # second column wires
    # the motor's COM is plugged into the board's M+ (tied to '+' on 5 V), not wired to the Pi
    com = [r for r in rows if r.b_ref == m.ref and r.b_pin == "COM"]
    assert len(com) == 1 and (com[0].a_ref, com[0].a_pin) == (drv.ref, "M+")
    assert not any(r.b_ref == drv.ref and r.b_pin == "M+" for r in rows)  # tied pin: no extra wire
    wiring_diagram(c, tmp_path / "chain.svg", tmp_path / "chain.png")
    assert (tmp_path / "chain.png").stat().st_size > 10_000


def test_wiring_diagram_long_title_is_not_clipped(tmp_path):
    """A 32-char project name must be shortened to fit: no ink in the right edge of the title band."""
    import numpy as np
    from PIL import Image

    c, _ = _demo()
    c.name = "very-long-project-name-0123456789"[:32]
    assert len(c.name) == 32
    png = tmp_path / "long.png"
    wiring_diagram(c, png.with_suffix(".svg"), png)
    px = np.asarray(Image.open(png).convert("L"))
    band = px[:62, :]  # the title sits alone in the top strip
    ink_cols = np.where((band < 200).any(axis=0))[0]
    assert ink_cols.size, "title not drawn"
    assert ink_cols.max() < px.shape[1] - 4, "title touches the right edge (clipped)"
