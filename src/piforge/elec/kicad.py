"""KiCad netlist export (Eeschema ``(export (version "E") ...)`` S-expression).

The netlist is the hand-off to a custom PCB (spec §1: no layout in PiForge). Components carry the
library ``footprint`` (KiCad 8 standard library names), nets list every connected pin with its pin
type. Output is deterministic (natural ref order, UUID5 time stamps).
"""

from __future__ import annotations

import uuid

import piforge
from piforge.elec.bom import part_value, ref_sort_key
from piforge.elec.model import Circuit, Part, Pin, PinType
from piforge.elec.units import format_kicad

_PINTYPE = {
    PinType.POWER_IN: "power_in", PinType.POWER_OUT: "power_out", PinType.GND: "power_in",
    PinType.INPUT: "input", PinType.OUTPUT: "output", PinType.BIDIR: "bidirectional",
    PinType.OPEN_DRAIN: "open_collector", PinType.PASSIVE: "passive", PinType.ANALOG: "passive",
    PinType.NC: "no_connect",
}


def _q(text: object) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _pintype(part: Part, pin: Pin) -> str:
    if pin.type == PinType.GND and (part.category in ("board", "power") or "regulator" in part.features):
        return "power_out"  # ground supplied by the board/PSU
    return _PINTYPE[pin.type]


def _value(part: Part) -> str:
    if part.key in ("resistor", "capacitor", "potentiometer"):
        return format_kicad(float(part.params["value"]))
    return part_value(part) or part.name


def kicad_netlist(circuit: Circuit, *, date: str = "") -> str:
    """KiCad "E" netlist of the circuit (parts, library parts, nets)."""
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f"piforge:{circuit.name}")
    parts = sorted(circuit.parts, key=lambda p: ref_sort_key(p.ref))
    out = ['(export (version "E")',
           "  (design",
           f"    (source {_q(circuit.name)})",
           f"    (date {_q(date)})",
           f"    (tool {_q('PiForge ' + piforge.__version__)})",
           f'    (sheet (number "1") (name "/") (tstamps "/")',
           f"      (title_block (title {_q(circuit.name)}) (company \"\") (rev \"\") (date {_q(date)}) (source \"\"))))",
           "  (components"]
    for p in parts:
        d = p.definition
        out += [f"    (comp (ref {_q(p.ref)})",
                f"      (value {_q(_value(p))})",
                f"      (footprint {_q(d.footprint)})",
                f"      (datasheet {_q(d.datasheet)})",
                f"      (fields (field (name \"PiForge\") {_q(p.key)}))",
                f"      (libsource (lib \"piforge\") (part {_q(p.key)}) (description {_q(d.name)}))",
                '      (sheetpath (names "/") (tstamps "/"))',
                f"      (tstamps {_q(uuid.uuid5(ns, p.ref))}))"]
    out += ["  )", "  (libparts"]
    seen: set[str] = set()
    for p in parts:
        d = p.definition
        if d.key in seen:
            continue
        seen.add(d.key)
        out += [f"    (libpart (lib \"piforge\") (part {_q(d.key)})",
                f"      (description {_q(d.name)})",
                f"      (footprints (fp {_q(d.footprint or '*')}))",
                f"      (fields (field (name \"Reference\") {_q(d.ref_prefix or p.ref.rstrip('0123456789'))}) "
                f"(field (name \"Value\") {_q(d.name)}))",
                "      (pins"]
        out += [f"        (pin (num {_q(pin.number)}) (name {_q(pin.name)}) (type {_q(_pintype(p, pin))}))"
                for pin in d.pins]
        out += ["      ))"]
    out += ["  )", '  (libraries (library (logical "piforge") (uri "piforge.elec.library")))', "  (nets"]
    for code, net in enumerate(circuit.nets, start=1):
        out.append(f"    (net (code {_q(code)}) (name {_q(net.name)})")
        refs = sorted(net.refs, key=lambda r: (ref_sort_key(r.part.ref), r.pin.number.zfill(4)))
        for r in refs:
            out.append(f"      (node (ref {_q(r.part.ref)}) (pin {_q(r.pin.number)}) (pinfunction {_q(r.pin.name)}) "
                       f"(pintype {_q(_pintype(r.part, r.pin))}))")
        out[-1] += ")"
    out += ["  ))"]
    return "\n".join(out) + "\n"
