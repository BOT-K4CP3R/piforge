"""Engineering-notation parsing/formatting."""

from __future__ import annotations

import pytest

from piforge.elec.units import format_kicad, format_si, parse_quantity


@pytest.mark.parametrize("text, value", [
    (330, 330.0), ("330", 330.0), ("4k7", 4700.0), ("4.7k", 4700.0), ("10kΩ", 10_000.0), ("10 kohm", 10_000.0),
    ("1M", 1e6), ("470R", 470.0), ("4R7", 4.7), ("100n", 100e-9), ("100nF", 100e-9), ("2.2uF", 2.2e-6),
    ("2u2", 2.2e-6), ("10µF", 10e-6), ("1ms", 1e-3), ("-5V", -5.0), ("1e3", 1000.0),
])
def test_parse_quantity(text, value):
    assert parse_quantity(text) == pytest.approx(value)


@pytest.mark.parametrize("bad", ["lots", "", "4k7k", "k", True, float("nan")])
def test_parse_quantity_rejects(bad):
    with pytest.raises(ValueError):
        parse_quantity(bad)


def test_format():
    assert format_si(330, "Ω") == "330 Ω"
    assert format_si(4700, "Ω") == "4.7 kΩ"
    assert format_si(1e-7, "F") == "100 nF"
    assert format_si(999.9, "Ω") == "1 kΩ"
    assert format_kicad(4700) == "4.7k" and format_kicad(330) == "330" and format_kicad(2.2e-6) == "2.2u"
