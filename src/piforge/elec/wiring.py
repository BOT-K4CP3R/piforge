"""Wiring table (one row per jumper wire, physical pin numbers, wire colours) and wiring diagram.

Every net becomes a star: from the Pi (or the first board) header pin to every other pin of the net;
nets without a board start at a power output or at the first pin connected. When several header pins
of one net exist (e.g. GND 6, 9, 14), each wire starts at the header pin it was connected with in
:meth:`Circuit.connect` (``Net.groups``). Colours: 5V red, 3V3 orange, GND black, other supply rails
brown, I2C SDA blue / SCL yellow, other signals from a fixed palette.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from piforge.elec.model import Circuit, Net, PinRef, PinType

log = logging.getLogger(__name__)

ROLE_COLORS = {"I2C_SDA": "blue", "I2C_SCL": "yellow", "SPI_MOSI": "green", "SPI_MISO": "purple",
               "SPI_SCLK": "white", "SPI_CS": "grey", "UART_TX": "green", "UART_RX": "white"}
# signal colours avoid the supply (red/orange/black/brown) and I2C (blue/yellow) colours
SIGNAL_PALETTE = ("green", "purple", "pink", "cyan", "grey", "white", "olive", "lime")


@dataclass
class WireRow:
    """One wire from ``a`` (board header pin / hub) to ``b``; ``*_phys`` are physical pin numbers."""

    net: str
    a_ref: str
    a_pin: str
    a_phys: str
    b_ref: str
    b_pin: str
    b_phys: str
    color: str
    signal: str


def _rail_volts(net: Net) -> float | None:
    if any(r.pin.type == PinType.GND for r in net.refs):
        return 0.0
    for r in net.refs:
        v = r.part.output_voltage(r.pin)
        if v is not None:
            return v
    return None


def _role(net: Net) -> str | None:
    for r in net.refs:
        for f in r.pin.functions:
            if f in ROLE_COLORS:
                return f
    return None


def _style(net: Net, index: int) -> tuple[str, str]:
    """(colour, signal description) for a net."""
    v = _rail_volts(net)
    if v is not None:
        if v == 0.0:
            return "black", "GND"
        if abs(v - 5.0) < 0.3:
            return "red", net.name
        if abs(v - 3.3) < 0.2:
            return "orange", net.name
        return "brown", net.name
    role = _role(net)
    gpio = next((r for r in net.refs if r.part.category == "board" and r.pin.name.startswith("GPIO")), None)
    if role:
        fam, line = role.split("_")  # e.g. ("I2C", "SDA"); a module TX lands on the Pi's RX
        if gpio is None:
            return ROLE_COLORS[role], f"{fam} {line}"
        want = {"TX": "RX", "RX": "TX"}.get(line, line) if fam == "UART" else line
        hit = next((f for f in gpio.pin.functions if f.startswith(fam) and f.endswith("_" + want)), None)
        return ROLE_COLORS[role], f"{hit.split('_')[0] if hit else fam} {line} ({gpio.pin.name})"
    color = SIGNAL_PALETTE[index % len(SIGNAL_PALETTE)]
    if gpio is not None:
        pwm = any("PWM_IN" in r.pin.functions for r in net.refs)
        return color, gpio.pin.name + (" (PWM)" if pwm else "")
    return color, net.name


def _hubs(net: Net, board) -> list[PinRef]:
    if board is not None:
        pins = [r for r in net.refs if r.part is board]
        if pins:
            pins.sort(key=lambda r: int(r.pin.number) if r.pin.number.isdigit() else 999)
            first = pins[0]
            tied = {first.pin.name}
            return [r for r in pins if r.pin.name in tied]
    outs = [r for r in net.refs if r.pin.type == PinType.POWER_OUT]
    return outs[:1] or net.refs[:1]


def _wire_source(net: Net, target: PinRef, hubs: list[PinRef]) -> PinRef | None:
    """Pin a wire to ``target`` starts from: the nearest hub along explicit connections (``Net.groups``).

    When the hub is reachable only through a part's internal tie (motor plugged into a driver board's
    ``M+`` which is tied to ``+``), the wire starts at that tied pin; the tied pin itself needs no wire
    (``None``).
    """
    adj: dict[PinRef, set[PinRef]] = {}
    for g in net.groups:
        for x in g:
            adj.setdefault(x, set()).update(y for y in g if y != x)
    seen, queue, comp = {target}, deque([target]), []
    while queue:
        x = queue.popleft()
        comp.append(x)
        if x in hubs and x != target:
            return x
        for y in sorted(adj.get(x, ()), key=lambda r: r.label):
            if y not in seen:
                seen.add(y)
                queue.append(y)
    inside = set(comp)
    bridges = [x for x in comp
               if any(PinRef(x.part, t) in net.refs and PinRef(x.part, t) not in inside for t in x.part.tied(x.pin))]
    if target in bridges:
        return None
    return bridges[0] if bridges else hubs[0]


def wiring_table(circuit: Circuit) -> list[WireRow]:
    """One row per wire: header pin -> part pin (star per net), with colour and signal name."""
    board = circuit.board
    rows: list[tuple[tuple, WireRow]] = []
    signal_index = 0
    for net_i, net in enumerate(circuit.nets):
        if len(net.refs) < 2:
            continue
        is_signal = _rail_volts(net) is None and _role(net) is None
        color, signal = _style(net, signal_index)
        signal_index += 1 if is_signal else 0
        hubs = _hubs(net, board)
        for r in net.refs:
            if r in hubs:
                continue
            h = _wire_source(net, r, hubs)
            if h is None:
                continue
            key = (0 if h.part is board else 1, int(h.pin.number) if h.part is board and h.pin.number.isdigit() else net_i,
                   net_i, r.part.ref, r.pin.number)
            rows.append((key, WireRow(net.name, h.part.ref, h.pin.name, h.pin.number, r.part.ref, r.pin.name,
                                      r.pin.number, color, signal)))
    rows.sort(key=lambda kv: kv[0])
    return [row for _, row in rows]


def wiring_markdown(rows: list[WireRow]) -> str:
    """Markdown table of the wiring rows."""
    out = ["| # | Net | From | Pin | Phys | To | Pin | Phys | Wire | Signal |",
           "|---:|---|---|---|---:|---|---|---:|---|---|"]
    for i, r in enumerate(rows, start=1):
        out.append(f"| {i} | {r.net} | {r.a_ref} | {r.a_pin} | {r.a_phys} | {r.b_ref} | {r.b_pin} | {r.b_phys} | "
                   f"{r.color} | {r.signal} |")
    return "\n".join(out) + "\n"


def wiring_diagram(circuit: Circuit, svg_path, png_path=None) -> None:
    """Draw the wiring diagram (Pi 2x20 header, parts as boxes, colour-coded wires) to SVG (+ PNG)."""
    from piforge.elec.wiring_draw import draw  # matplotlib is imported only here

    rows = wiring_table(circuit)
    fig = draw(circuit, rows)
    svg = Path(svg_path)
    svg.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(svg, format="svg")
    if png_path is not None:
        png = Path(png_path)
        png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(png, format="png", dpi=150)
    log.info("wiring diagram written to %s", svg)
