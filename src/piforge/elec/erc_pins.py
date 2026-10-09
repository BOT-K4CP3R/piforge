"""ERC rules about inputs and header pins: floating inputs, pulls in the wrong direction, the reserved
ID EEPROM pins and interface pin conflicts (registered in :data:`piforge.elec.erc.RULES`)."""

from __future__ import annotations

from piforge.core.report import Finding
from piforge.elec.ercbase import E, W, _f, _phys, _pin
from piforge.elec.model import Circuit, Net, PinRef, PinType
from piforge.elec.signals import SignalAnalysis, analysis, is_board_gpio


# ---------------------------------------------------------------------------------- inputs & pins
def floating_input(c: Circuit) -> list[Finding]:
    """Inputs that float when a switch is open or an open-drain output releases the line."""
    a = analysis(c)
    out = []
    groups = _floating_groups(a)
    for group in groups:
        refs = [r for n in group for r in n.refs]
        if any(r.pin.type in (PinType.POWER_OUT,) for r in refs) or any(a.push_pull_outputs(n) for n in group):
            continue
        sources = [r for r in refs if r.part.category == "switch"
                   or (r.pin.type == PinType.OPEN_DRAIN and r.part.category != "board")]
        if not sources:
            continue
        victims = [r for r in refs if is_board_gpio(r)] or \
                  [r for r in refs if r.pin.type in (PinType.INPUT, PinType.BIDIR) and r.part.category != "switch"]
        if not victims:
            continue
        v = victims[0]
        what = f"{sources[0].part.ref} ({sources[0].part.name})"
        cfg = (f"c.configure({v.part.ref}, pulls={{'{v.pin.name}': 'up'}})" if is_board_gpio(v) else "a 10 kΩ resistor")
        out.append(_f("ERC.FLOATING_INPUT", W, f"{_phys(v)} floats when {what} is open/released: no pull-up or "
                      "pull-down defines its level.", _pin(v), f"Enable a pull ({cfg}) or add a 10 kΩ pull-up "
                      "(4.7 kΩ for 1-Wire) to 3V3."))
    return out


def pull_direction(c: Circuit) -> list[Finding]:
    """A switch from an input to the level its pull already holds: pressing it changes nothing."""
    a = analysis(c)
    out = []
    for part in c.parts:
        if part.category != "switch":
            continue
        nets = [n for n in (a.pin_net(part, p.name) for p in part.pins()) if n is not None]
        part_rails = [n for n in nets if n in a.rails]
        volts = [a.rails[n] for n in part_rails]
        if volts and max(volts) - min(volts) >= 1.0:
            continue  # changeover: some position drives the input to each rail, the pull is irrelevant
        for rail in part_rails:
            for net in (n for n in nets if n not in a.rails):
                v = a.base.volts(net)
                victims = [r for r in net.refs if is_board_gpio(r)] or \
                          [r for r in net.refs if r.pin.type in (PinType.INPUT, PinType.BIDIR)]
                if v is None or not victims:
                    continue
                vr = a.rails[rail]
                if abs(v - vr) < 1.0:  # same logic level whether the switch is open or closed
                    level = "low" if vr < 1.0 else "high"
                    other = "up" if level == "low" else "down"
                    out.append(_f("ERC.PULL_DIRECTION", W, f"{_phys(victims[0])} is already pulled {level} "
                                  f"({v:.2f} V) and {part.ref} ({part.name}) connects it to {rail.name}: pressing it "
                                  "does not change the level.", _pin(victims[0]),
                                  f"Pull the input {other} instead (c.configure(pi, pulls={{'{victims[0].pin.name}': "
                                  f"'{other}'}})) or wire the switch to the other rail."))
    return out


def _floating_groups(a: SignalAnalysis) -> list[list[Net]]:
    """Connected groups (through resistive elements) of nets that have no defined idle voltage."""
    floating = [n for n in a.c.nets if n not in a.rails and a.base.volts(n) is None]
    ids = {id(n): n for n in floating}
    seen: set[int] = set()
    groups = []
    for n in floating:
        if id(n) in seen:
            continue
        group, stack = [], [n]
        seen.add(id(n))
        while stack:
            x = stack.pop()
            group.append(x)
            for _, e in a.net.attached(x):
                other = e.b if e.a is x else e.a
                if id(other) in ids and id(other) not in seen:
                    seen.add(id(other))
                    stack.append(other)
        groups.append(group)
    return groups


def reserved_pin(c: Circuit) -> list[Finding]:
    """GPIO0/GPIO1 (ID_SD/ID_SC) are reserved for the HAT ID EEPROM."""
    out = []
    for board in c.boards():
        if c.config(board)["interfaces"].get("id_eeprom"):
            continue
        for name in board.params.get("reserved", ()):
            ref = PinRef(board, board.pin(name))
            net = c.net_of(ref)
            if net is not None and any(r.part is not board for r in net.refs):
                out.append(_f("ERC.RESERVED_PIN", W, f"{_phys(ref)} is reserved for the HAT ID EEPROM (I2C0, probed at "
                              "boot); using it for other signals can break booting with HATs.", _pin(ref),
                              "Move the signal to another GPIO (or configure interfaces={'id_eeprom': True} if this "
                              "is the ID EEPROM)."))
    return out


def interface_pin_conflict(c: Circuit) -> list[Finding]:
    from piforge.elec.bootconfig import interface_claims  # lazy: bootconfig builds on the model only

    out = []
    for board in c.boards():
        claims, miswired = interface_claims(c, board)
        for bcm, (iface, allowed) in claims.items():
            ref = PinRef(board, board.pin(f"GPIO{bcm}"))
            net = c.net_of(ref)
            if net is None:
                continue
            intruders = [r for r in net.refs if r.part is not board and not allowed(r)]
            if intruders:
                out.append(_f("ERC.INTERFACE_PIN_CONFLICT", E, f"{_phys(ref)} is used by {iface} but also wired to "
                              f"{', '.join(r.label for r in intruders)}.", _pin(ref),
                              "Move the other signal to a free GPIO (see piforge.elec.pinmap.allocate_pins).",
                              interface=iface))
        for sev, ref, msg, hint in miswired:
            out.append(_f("ERC.INTERFACE_PIN_CONFLICT", sev, msg, _pin(ref), hint))
    return out
