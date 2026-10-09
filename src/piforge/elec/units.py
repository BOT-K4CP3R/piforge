"""Engineering-notation parsing and formatting for component values (SI units).

``parse_quantity("4k7") == 4700.0``, ``parse_quantity("100nF") == 1e-7``,
``format_si(4700, "Ω") == "4.7 kΩ"``. Resistor codes with ``R``/``k``/``M`` as the decimal mark
(``4R7``, ``2k2``, ``1M5``) are accepted, as are ``u``/``µ`` for micro.
"""

from __future__ import annotations

import math
import re

_PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "": 1.0, "r": 1.0, "R": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}
_UNIT_WORDS = ("ohms", "ohm", "Ω", "Ω", "F", "f", "H", "h", "V", "v", "A", "a", "Hz", "hz", "W", "w", "s")

# "4k7", "2R2", "1M5": the multiplier letter sits where the decimal point would be
_CODE = re.compile(r"^(\d+)([pnuµμmrRkKMG])(\d+)$")
_PLAIN = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([pnuµμmrRkKMG]?)$")


def parse_quantity(value: str | float | int) -> float:
    """Parse ``330``, ``"4.7k"``, ``"4k7"``, ``"10 kΩ"``, ``"100nF"``, ``"2u2"`` into a float (SI base unit).

    Raises ``ValueError`` when the text is not a number with an optional SI prefix and unit.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a quantity: {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"not a finite quantity: {value!r}")
        return float(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    for unit in sorted(_UNIT_WORDS, key=len, reverse=True):
        if text.endswith(unit) and len(text) > len(unit):
            stripped = text[: -len(unit)]
            # keep a bare multiplier such as "M" in "1M" (mega) but drop "F" from "100nF"
            if stripped and (stripped[-1].isdigit() or stripped[-1] in _PREFIX or stripped[-1] == "."):
                text = stripped
                break
    m = _CODE.match(text)
    if m:
        whole, prefix, frac = m.groups()
        return float(f"{whole}.{frac}") * _PREFIX[prefix]
    m = _PLAIN.match(text)
    if m:
        number, prefix = m.groups()
        return float(number) * _PREFIX[prefix]
    raise ValueError(f"cannot parse {value!r} as a quantity (examples: 330, 4k7, 4.7k, 100nF, 2.2u)")


_FMT_PREFIX = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]


def format_si(value: float, unit: str = "", digits: int = 3) -> str:
    """Format ``value`` with an SI prefix: ``format_si(4700, "Ω") -> "4.7 kΩ"``."""
    if value == 0 or not math.isfinite(value):
        return f"{value:g} {unit}".strip()
    mag = abs(value)
    for scale, prefix in _FMT_PREFIX:
        if mag >= scale * 0.9995:
            num = value / scale
            text = f"{num:.{digits}g}"
            return f"{text} {prefix}{unit}".strip()
    scale, prefix = _FMT_PREFIX[-1]
    return f"{value / scale:.{digits}g} {prefix}{unit}".strip()


def format_kicad(value: float) -> str:
    """KiCad-style compact value without unit: 330 -> ``330``, 4700 -> ``4.7k``, 1e-7 -> ``100n``."""
    text = format_si(value, "", digits=3).replace(" ", "")
    return text.replace("µ", "u")
