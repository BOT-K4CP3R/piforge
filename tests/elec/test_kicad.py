"""KiCad netlist export ("E" S-expression)."""

from __future__ import annotations

import re

from piforge.elec.kicad import kicad_netlist
from piforge.elec.model import Circuit


def _tokens(text: str):
    """Tiny S-expression tokenizer honouring double-quoted strings with backslash escapes."""
    i, out = 0, []
    while i < len(text):
        ch = text[i]
        if ch in "()":
            out.append(ch)
            i += 1
        elif ch == '"':
            j = i + 1
            buf = []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            out.append(("str", "".join(buf)))
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < len(text) and not text[j].isspace() and text[j] not in '()"':
                j += 1
            out.append(("atom", text[i:j]))
            i = j
    return out


def _parse(tokens):
    stack = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(t[1])
    assert len(stack) == 1
    return stack[0]


def _demo():
    c = Circuit('demo "quoted" net')
    pi = c.add("rpi4b")
    r = c.add("resistor", value=330)
    led = c.add("led")
    c.connect(pi["GPIO17"], r["1"])
    c.connect(r["2"], led["A"], name="LED_A")
    c.connect(led["K"], pi["GND"])
    bme = c.add("bme280_breakout")
    c.connect(pi["3V3"], bme["VIN"])
    c.connect(pi["GND"], bme["GND"])
    c.connect(pi["SDA1"], bme["SDA"])
    c.connect(pi["SCL1"], bme["SCL"])
    return c


def test_kicad_netlist_parses():
    c = _demo()
    text = kicad_netlist(c)
    depth = 0
    for t in _tokens(text):
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        assert depth >= 0
    assert depth == 0, "balanced parentheses"
    tree = _parse(_tokens(text))
    assert len(tree) == 1 and tree[0][0] == "export"
    root = tree[0]
    assert ["version", "E"] in root
    sections = {item[0]: item for item in root[1:] if isinstance(item, list)}
    assert {"design", "components", "nets"} <= set(sections)
    comps = {next(x[1] for x in comp if isinstance(x, list) and x[0] == "ref")
             for comp in sections["components"][1:]}
    assert comps == {p.ref for p in c.parts}
    nets = {}
    for net in sections["nets"][1:]:
        name = next(x[1] for x in net if isinstance(x, list) and x[0] == "name")
        nodes = [{y[0]: y[1] for y in x[1:]} for x in net if isinstance(x, list) and x[0] == "node"]
        nets[name] = nodes
    assert set(nets) == {n.name for n in c.nets}
    gnd = {(n["ref"], n["pin"]) for n in nets["GND"]}
    assert ("U1", "6") in gnd and ("D1", "1") in gnd and ("U2", "2") in gnd
    led_a = nets["LED_A"]
    assert {(n["ref"], n["pin"]) for n in led_a} == {("R1", "2"), ("D1", "2")}
    assert all(n.get("pintype") for n in led_a)
    assert re.search(r'\(value "330"\)', text) or re.search(r'\(value "330 ?Ω"\)', text)


def test_kicad_netlist_deterministic():
    assert kicad_netlist(_demo()) == kicad_netlist(_demo())
