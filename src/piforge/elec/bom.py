"""Bill of materials: identical parts grouped into lines, exported as CSV or Markdown."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from piforge.elec.model import Circuit, Part
from piforge.elec.units import format_si


@dataclass
class BomLine:
    """One BOM row: identical parts (same key and value) with their reference designators."""

    refs: tuple[str, ...]
    key: str
    name: str
    value: str
    qty: int
    notes: str = ""


def ref_sort_key(ref: str) -> tuple:
    """Natural order for reference designators: R2 < R10."""
    m = re.fullmatch(r"([A-Za-z_]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def part_value(part: Part) -> str:
    """Human value string for BOM/KiCad: ``330 Ω``, ``100 nF 16 V``, ``red``, ``5.1 V 3 A``…"""
    p = part.params
    if part.key in ("resistor", "potentiometer"):
        return format_si(float(p["value"]), "Ω")
    if part.key == "capacitor":
        return f"{format_si(float(p['value']), 'F')} {p.get('voltage_rating', 16):g} V"
    if part.key == "led":
        return str(p.get("color", ""))
    if part.key == "rgb_led_cc":
        return "RGB common cathode"
    if "psu" in part.features:
        return f"{p.get('v_out', 0):g} V {float(p.get('i_max_ma', 0)) / 1000:g} A"
    if "regulator" in part.features and p.get("vout") is not None:
        return f"{float(p['vout']):g} V out"
    if p.get("supply_per_unit") and "count" in p:
        return f"{int(p['count'])} LEDs"
    return ""


def bom(circuit: Circuit) -> list[BomLine]:
    """Group parts by (key, value); lines keep first-appearance order, refs are naturally sorted."""
    groups: dict[tuple[str, str], list[Part]] = {}
    for part in circuit.parts:
        groups.setdefault((part.key, part_value(part)), []).append(part)
    lines = []
    for (key, value), parts in groups.items():
        notes = []
        addrs = sorted({int(p.params["i2c_address"]) for p in parts if "i2c_address" in p.params})
        if addrs:
            notes.append("I2C " + ", ".join(f"0x{a:02X}" for a in addrs))
        lines.append(BomLine(refs=tuple(sorted((p.ref for p in parts), key=ref_sort_key)), key=key,
                             name=parts[0].name, value=value, qty=len(parts), notes="; ".join(notes)))
    return lines


def bom_csv(lines: list[BomLine]) -> str:
    """CSV with columns Qty, Refs (space separated), Key, Name, Value, Notes."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Qty", "Refs", "Key", "Name", "Value", "Notes"])
    for x in lines:
        w.writerow([x.qty, " ".join(x.refs), x.key, x.name, x.value, x.notes])
    return buf.getvalue()


def _md(text: str) -> str:
    return str(text).replace("|", "\\|")


def bom_markdown(lines: list[BomLine]) -> str:
    """Markdown table of the BOM."""
    out = ["| Qty | Refs | Part | Value | Key | Notes |", "|---:|---|---|---|---|---|"]
    for x in lines:
        out.append(f"| {x.qty} | {' '.join(x.refs)} | {_md(x.name)} | {_md(x.value)} | `{x.key}` | {_md(x.notes)} |")
    return "\n".join(out) + "\n"
