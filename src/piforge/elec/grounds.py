"""Ground domains: which parts share a 0 V reference.

Only the GND pins of boards, power supplies and regulators are references
(:func:`piforge.elec.dcsolve.is_ground_ref`). A *ground island* is a net holding at least one reference
GND pin; a reference part whose GND is not wired at all is an island of its own (a Pi always has its own
ground). A part with GND pins belongs to the island its GND net is on. Signal and supply nets are then
grouped through parts without GND pins (resistors, LEDs, transistors, motors, bare relays...), except
across pins listed in ``params["isolated_pins"]`` (relay contacts are galvanically separate). A group
touching parts of two islands carries a signal between grounds that are not connected: the receiver
sees an undefined voltage and the current has no return path -> ``ERC.NO_COMMON_GROUND``.

A net with GND pins but no reference (module grounds tied only to each other) is a *floating ground*
-> ``ERC.UNCONNECTED`` (error) from :func:`floating_grounds`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from piforge.elec.dcsolve import is_ground_ref
from piforge.elec.model import Circuit, Net, Part, PinRef, PinType


@dataclass
class GroundDomains:
    """``islands``: key -> description; ``domain``: part -> island key (None = no GND pin / unreferenced)."""

    islands: dict[object, str] = field(default_factory=dict)
    domain: dict[Part, object] = field(default_factory=dict)
    floating: list[Net] = field(default_factory=list)
    gnd_nets: set[int] = field(default_factory=set)


def _gnd_pins(part: Part) -> list:
    return [p for p in part.pins() if p.type == PinType.GND]


def ground_domains(c: Circuit) -> GroundDomains:
    """Classify GND nets into reference islands / floating grounds and give each part its island."""
    g = GroundDomains()
    for net in c.nets:
        gnd = [r for r in net.refs if r.pin.type == PinType.GND]
        if not gnd:
            continue
        g.gnd_nets.add(id(net))
        refs = [r for r in gnd if is_ground_ref(r.part)]
        if refs:
            parts = ", ".join(dict.fromkeys(r.part.ref for r in refs))
            g.islands[id(net)] = f"ground net {net.name} ({parts})"
        else:
            g.floating.append(net)
    for part in c.parts:
        pins = _gnd_pins(part)
        if not pins:
            continue
        key = None
        for p in pins:
            net = c.net_of(PinRef(part, p))
            if net is not None and id(net) in g.islands:
                key = id(net)
                break
        if key is None and is_ground_ref(part) and not any(c.is_connected(PinRef(part, p)) for p in pins):
            key = ("own", part.ref)
            g.islands[key] = f"{part.ref}'s own ground (its GND is not wired)"
        g.domain[part] = key
    return g


def floating_grounds(c: Circuit) -> list[Net]:
    """GND nets with module grounds only (no board/PSU/regulator ground on them)."""
    return ground_domains(c).floating


def ground_crossings(c: Circuit) -> list[tuple[list[Net], dict[object, list[PinRef]], dict[object, str]]]:
    """Groups of non-ground nets that connect parts of two or more ground islands:
    ``(nets, {island: [pins]}, island descriptions)``."""
    g = ground_domains(c)
    if len(g.islands) < 2:
        return []
    nets = [n for n in c.nets if id(n) not in g.gnd_nets]
    index = {id(n): i for i, n in enumerate(nets)}
    parent = list(range(len(nets)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for part in c.parts:
        if _gnd_pins(part):
            continue  # parts with a ground carry their island; they do not bridge nets
        isolated = set(part.params.get("isolated_pins") or ())
        for group in ([p for p in part.pins() if p.name not in isolated],
                      [p for p in part.pins() if p.name in isolated]):
            ids = [index[id(n)] for n in (c.net_of(PinRef(part, p)) for p in group)
                   if n is not None and id(n) in index]
            for a, b in zip(ids, ids[1:]):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    clusters: dict[int, list[Net]] = {}
    for n in nets:
        clusters.setdefault(find(index[id(n)]), []).append(n)
    out = []
    for group in clusters.values():
        by_island: dict[object, list[PinRef]] = {}
        for n in group:
            for r in n.refs:
                key = g.domain.get(r.part)
                if key is None or r.pin.name in set(r.part.params.get("isolated_pins") or ()):
                    continue
                by_island.setdefault(key, []).append(r)
        if len(by_island) > 1:
            out.append((group, by_island, g.islands))
    return out
