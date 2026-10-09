"""Bill of materials grouping and export."""

from __future__ import annotations

import csv
import io

from piforge.elec.bom import BomLine, bom, bom_csv, bom_markdown
from piforge.elec.model import Circuit


def _circuit():
    c = Circuit("bom")
    c.add("rpi4b")
    for _ in range(3):
        c.add("resistor", value=330)
    c.add("resistor", value=10_000)
    c.add("led", color="red")
    c.add("led", color="green")
    c.add("led", color="red")
    c.add("bme280_breakout")
    c.add("bme280_breakout", i2c_address=0x77)
    return c


def test_bom_groups():
    lines = bom(_circuit())
    assert all(isinstance(x, BomLine) for x in lines)
    r330 = [x for x in lines if x.key == "resistor" and x.qty == 3]
    assert len(r330) == 1 and r330[0].refs == ("R1", "R2", "R3")
    assert "330" in r330[0].value and "Ω" in r330[0].value
    r10k = next(x for x in lines if x.key == "resistor" and x.qty == 1)
    assert r10k.refs == ("R4",) and "10" in r10k.value and "k" in r10k.value
    leds = {x.value: x for x in lines if x.key == "led"}
    assert leds["red"].qty == 2 and leds["green"].qty == 1
    bme = next(x for x in lines if x.key == "bme280_breakout")
    assert bme.qty == 2  # the address is a solder jumper, same part
    assert sum(x.qty for x in lines) == 10  # Pi + 4 resistors + 3 LEDs + 2 BME280


def test_bom_csv_and_markdown():
    lines = bom(_circuit())
    text = bom_csv(lines)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows and {"Qty", "Refs", "Key", "Name", "Value", "Notes"} <= set(rows[0])
    assert any(r["Refs"] == "R1 R2 R3" and r["Qty"] == "3" for r in rows)
    md = bom_markdown(lines)
    assert md.splitlines()[0].startswith("|") and "| 3 |" in md and "R1 R2 R3" in md
