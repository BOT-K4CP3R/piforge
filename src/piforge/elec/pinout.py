"""Markdown pinout of the circuit's Raspberry Pi (wired header pins and a 2x20 header map)."""

from __future__ import annotations

from typing import Any


def pinout_markdown(circuit: Any, rows: list | None = None) -> str:
    """Markdown pinout of the circuit's Raspberry Pi: the wired header pins (net, parts, pull) and a
    2×20 header map. ``rows`` = :func:`piforge.elec.wiring_table` output (computed when omitted)."""
    title = f"# Pinout — {circuit.name}"
    board = circuit.board
    if board is None:
        return f"{title}\n\nNo Raspberry Pi in the circuit.\n"
    if rows is None:
        from piforge.elec.wiring import wiring_table

        rows = wiring_table(circuit)
    wired: dict[str, list[tuple[str, str]]] = {}  # physical pin → [(net, "REF.PIN")]
    for r in rows:
        if r.a_ref == board.ref:
            wired.setdefault(r.a_phys, []).append((r.net, f"{r.b_ref}.{r.b_pin}"))
        elif r.b_ref == board.ref:
            wired.setdefault(r.b_phys, []).append((r.net, f"{r.a_ref}.{r.a_pin}"))
    cfg = circuit.config(board)
    pulls = {"up": "pull-up", "down": "pull-down", "none": "no pull"}
    names = {q.ref: q.name for q in circuit.parts}
    pins = {pin.number: pin for pin in board.pins()}
    out = [title, "", f"{board.ref}: {board.name} — {len(wired)} of {len(pins)} header pins wired.", "",
           "| Phys | Pin | BCM | Net | Wired to | Pull (configured) |", "|---:|---|---:|---|---|---|"]
    for number, pin in pins.items():
        if number in wired:
            bcm = next((f[3:] for f in pin.functions if f.startswith("BCM")), "")
            to = ", ".join(f"{t} ({names.get(t.split('.')[0], '')})" for _, t in wired[number])
            out.append(f"| {number} | {pin.name} | {bcm} | {wired[number][0][0]} | {to} | "
                       f"{pulls.get(cfg['pulls'].get(pin.name, ''), '')} |")
    enabled = sorted(k for k, v in cfg["interfaces"].items() if v)
    if enabled:
        out += ["", f"Interfaces enabled: {', '.join(enabled)} (see config.txt)."]
    out += ["", "## 40-pin header (top view, pin 1 = 3V3)", "",
            "| Wired to | Pin | # | # | Pin | Wired to |", "|---|---|---:|---:|---|---|"]

    def side(n: int) -> tuple[str, str]:
        pin = pins.get(str(n))
        return (pin.name if pin else "", ", ".join(t for _, t in wired.get(str(n), [])))

    for odd in range(1, len(pins), 2):
        (ln, lw), (rn, rw) = side(odd), side(odd + 1)
        out.append(f"| {lw} | {ln} | {odd} | {odd + 1} | {rn} | {rw} |")
    return "\n".join(out) + "\n"
