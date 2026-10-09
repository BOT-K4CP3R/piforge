"""Linearised DC operating-point solver for ERC: what voltage does a pin really see?

The circuit is reduced to resistive elements between nets (resistors, potentiometers, coil/motor/
buzzer loads, on-board pull-ups, configured GPIO pulls, module input loads) and ideal diodes
(LEDs, rectifiers, BJT base-emitter junctions: forward drop ``vf`` + 1 Ω on-resistance) plus controlled
switches: an N-MOSFET drain-source channel (``rds_on_ohm``) conducts while VGS >= ``vgs_th_min`` and an
NPN collector-emitter path (``vce_sat`` drop) conducts while its base-emitter junction carries current.
Rails (ground references, POWER_OUT pins, regulator outputs) and pull-up references are fixed-voltage
nodes. Only the GND pins of boards, power supplies and regulators are 0 V references
(:func:`is_ground_ref`): a module's own GND pin defines nothing until it is wired to one of those.
A scenario adds more fixed nodes (e.g. "ECHO drives 5 V", "GPIO17 drives 3.3 V"); :meth:`Network.solve`
does nodal analysis on every connected component that touches a fixed node and iterates diode and
channel on/off states. ``Network(c, pot_position=0.0 | 1.0)`` evaluates every pot at one end.
Nodes without a resistive path to any fixed node come back as ``None`` (floating).

Pure Python (Gaussian elimination with partial pivoting); circuits here have < 100 nodes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from piforge.elec.model import Circuit, Net, Part, PinRef, PinType

log = logging.getLogger(__name__)

R_ON = 1.0          # Ω, modelling constant (not a datum): ideal diode/LED on-resistance, keeps the matrix regular
R_MIN = 1e-3        # Ω, modelling constant: floor for user resistances (a 0 Ω resistor is a wire)
_MAX_ITER = 40


GROUND_REF_CATEGORIES = ("board", "power", "regulator")


def is_ground_ref(part: Part) -> bool:
    """True when ``part``'s GND pins are a 0 V reference (board, PSU, regulator - things that source power)."""
    return (part.category in GROUND_REF_CATEGORIES or bool({"psu", "regulator"} & set(part.features))
            or bool(part.params.get("ground_ref")))


@dataclass(frozen=True)
class Fixed:
    """A pseudo node held at ``volts`` (internal regulator output, pull reference)."""

    volts: float


@dataclass
class Element:
    """Two-terminal DC element from node ``a`` to node ``b``. ``vf > 0`` (or ``diode``) marks a diode a->b."""

    a: object
    b: object
    r: float
    vf: float = 0.0
    diode: bool = False
    tag: str = ""            # resistor | pot | load | pullup | pull | input | led | diode | junction | channel
    part: Part | None = None
    pin: str = ""            # for pullups/pulls/inputs: the pin the element hangs on
    ctrl: tuple | None = None  # channel control: ("vgs", gate, source, vth) or ("junction", element index)


@dataclass
class Solution:
    """Node voltages (``None`` = floating) and element currents a->b (A) for one scenario."""

    v: dict = field(default_factory=dict)
    current: list = field(default_factory=list)
    on: list = field(default_factory=list)

    def volts(self, node) -> float | None:
        return self.v.get(node)

    def current_out(self, net: object, elements: list[Element]) -> float:
        """Current (A) flowing out of ``net`` into the network (positive = the node sources current)."""
        total = 0.0
        for i, e in enumerate(elements):
            if not self.on[i]:
                continue
            if e.a is net:
                total += self.current[i]
            elif e.b is net:
                total -= self.current[i]
        return total


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-18:
            raise ZeroDivisionError("singular DC network")
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
            b[col], b[piv] = b[piv], b[col]
        inv = 1.0 / a[col][col]
        for r in range(col + 1, n):
            f = a[r][col] * inv
            if f:
                row_r, row_c = a[r], a[col]
                for k in range(col, n):
                    row_r[k] -= f * row_c[k]
                b[r] -= f * b[col]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = b[r] - sum(a[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / a[r][r]
    return x


class Network:
    """DC model of a :class:`Circuit` (built once per circuit version)."""

    def __init__(self, circuit: Circuit, *, pot_position: float | None = None):
        self.circuit = circuit
        self.pot_position = pot_position  # override every potentiometer's wiper (0 = CCW end, 1 = CW end)
        self.rails: dict[Net, float] = {}
        self.rail_sources: dict[Net, list[tuple[PinRef, float | None]]] = {}
        self.elements: list[Element] = []
        self._cache: dict[tuple, Solution] = {}
        self._find_rails()
        self._build_elements()

    # -- construction -----------------------------------------------------------------------
    def net(self, part: Part, pin_name: str) -> Net | None:
        """Net of ``part.pin_name`` (or of an internally tied pin), ``None`` when unconnected."""
        try:
            pin = part.pin(pin_name)
        except LookupError:
            return None
        n = self.circuit.net_of(PinRef(part, pin))
        if n is None:
            for t in part.tied(pin):
                n = self.circuit.net_of(PinRef(part, t))
                if n is not None:
                    break
        return n

    def _find_rails(self) -> None:
        for _ in range(5):  # regulators may depend on other rails
            changed = False
            for net in self.circuit.nets:
                sources: list[tuple[PinRef, float | None]] = []
                gnd = False
                for r in net.refs:
                    if r.pin.type == PinType.GND:
                        if is_ground_ref(r.part):
                            gnd = True
                            sources.append((r, 0.0))
                    elif r.pin.type == PinType.POWER_OUT:
                        v = r.part.output_voltage(r.pin)
                        need = r.part.params.get("requires_input")
                        if need and r.part.params.get("vout_pin") == r.pin.name:
                            inp = self.net(r.part, need)
                            vin = self.rails.get(inp) if inp is not None else None
                            if vin is None or v is None or vin < v + float(r.part.params.get("dropout_v", 0.0)):
                                continue  # regulator without (enough) input is not a source
                        if v is not None:
                            sources.append((r, v))
                if sources:
                    volts = 0.0 if gnd else sources[0][1]
                    if self.rails.get(net) != volts:
                        self.rails[net] = volts
                        changed = True
                    self.rail_sources[net] = sources
            if not changed:
                break

    def _add(self, a, b, r: float, **kw) -> int | None:
        if a is None or b is None or a is b or (isinstance(a, Fixed) and isinstance(b, Fixed)):
            return None
        self.elements.append(Element(a, b, max(float(r), R_MIN), **kw))
        return len(self.elements) - 1

    def _ref_node(self, part: Part, target) -> object | None:
        """Pull-up target: a fixed voltage, a POWER_OUT pin of the part (its internal rail) or a pin's net."""
        if target is None:
            return None
        if isinstance(target, (int, float)):
            return Fixed(float(target))
        try:
            pin = part.pin(str(target))
        except LookupError:
            return None
        if pin.type == PinType.POWER_OUT and part.output_voltage(pin) is not None:
            n = self.circuit.net_of(PinRef(part, pin))
            return n if n is not None else Fixed(float(part.output_voltage(pin)))
        return self.net(part, pin.name)

    def _build_elements(self) -> None:
        c = self.circuit
        for part in c.parts:
            p = part.params
            n = lambda name: self.net(part, name)  # noqa: E731
            if part.key == "resistor":
                self._add(n("1"), n("2"), p["value"], tag="resistor", part=part)
            elif part.key == "potentiometer":
                pos = p.get("position", 0.5) if self.pot_position is None else self.pot_position
                total, pos = float(p.get("value", 10_000.0)), min(max(float(pos), 0.0), 1.0)
                self._add(n("CCW"), n("W"), total * pos, tag="pot", part=part)
                self._add(n("W"), n("CW"), total * (1 - pos), tag="pot", part=part)
            if part.category == "led":
                if part.key == "rgb_led_cc":
                    for col, key in (("R", "vf_r"), ("G", "vf_g"), ("B", "vf_b")):
                        self._add(n(col), n("K"), R_ON, vf=float(p.get(key, 2.0)), diode=True, tag="led",
                                  part=part, pin=col)
                else:
                    self._add(n("A"), n("K"), R_ON, vf=float(p.get("vf", 2.0)), diode=True, tag="led", part=part, pin="A")
            elif part.category == "diode":
                self._add(n("A"), n("K"), R_ON, vf=float(p.get("vf", 0.7)), diode=True, tag="diode", part=part, pin="A")
            elif part.category == "transistor" and p.get("kind") == "npn":
                j = self._add(n("B"), n("E"), R_ON, vf=float(p.get("vbe_on", 0.7)), diode=True, tag="junction",
                              part=part, pin="B")
                if j is not None:
                    self._add(n("C"), n("E"), R_ON, vf=float(p.get("vce_sat", 0.2)), diode=True, tag="channel",
                              part=part, pin="C", ctrl=("junction", j))
            elif part.category == "transistor" and p.get("kind") == "nmos":
                g, s = n("G"), n("S")
                if g is not None:
                    self._add(n("D"), s, float(p.get("rds_on_ohm", R_ON)), tag="channel", part=part, pin="D",
                              ctrl=("vgs", g, s, float(p.get("vgs_th_min", p.get("vgs_th_max", 2.0)))))
            if p.get("load_ohms") and p.get("load_pins"):
                a, b = p["load_pins"]
                self._add(n(a), n(b), p["load_ohms"], tag="load", part=part)
            for a, b, ohms in p.get("loads", ()):
                self._add(n(a), n(b), ohms, tag="load", part=part)
            for pin_name, (ohms, target) in (p.get("pullups") or {}).items():
                if "pullup_ohms" in p and target not in (0, 0.0):
                    ohms = p["pullup_ohms"]
                if ohms is None:
                    continue
                self._add(n(pin_name), self._ref_node(part, target), ohms, tag="pullup", part=part, pin=pin_name)
            for pin_name, (ohms, vf, gnd) in (p.get("input_loads") or {}).items():
                self._add(n(pin_name), n(gnd), ohms, vf=vf, diode=True, tag="input", part=part, pin=pin_name)
            pulls = c.config(part)["pulls"]
            if pulls:
                ohms = float(p.get("pull_ohms", 50_000.0))
                vdd = float(p.get("gpio_vdd", 3.3))
                for pin_name, mode in pulls.items():
                    if mode in ("up", "down"):
                        self._add(n(pin_name), Fixed(vdd if mode == "up" else 0.0), ohms, tag="pull",
                                  part=part, pin=pin_name)

    # -- solving ----------------------------------------------------------------------------
    def solve(self, fixed: dict | None = None) -> Solution:
        """Operating point with the rails plus ``fixed`` ({net: volts}) held; cached per scenario."""
        extra = {k: float(v) for k, v in (fixed or {}).items() if k not in self.rails}
        key = tuple(sorted(((id(k), v) for k, v in extra.items())))
        if key in self._cache:
            return self._cache[key]
        held: dict = dict(self.rails)
        held.update(extra)
        els = self.elements
        # Diodes start OFF and switch ON when forward biased (or when their anode is known and the
        # cathode still floats, so LED chains can come up); ON diodes with reverse current switch OFF.
        # Starting OFF keeps nodes that are only reachable through a zero-current diode floating.
        # Controlled channels start OFF and follow their gate/base each iteration.
        on = [not e.diode and e.ctrl is None for e in els]
        sol = Solution()
        for _ in range(_MAX_ITER):
            sol = self._solve_once(held, on)
            flip = False
            for i, e in enumerate(els):
                if e.ctrl is not None and not self._ctrl_ok(e.ctrl, sol, on):
                    if on[i]:
                        on[i] = False
                        flip = True
                    continue
                if not e.diode:
                    if not on[i]:
                        on[i] = True
                        flip = True
                    continue
                va = e.a.volts if isinstance(e.a, Fixed) else sol.v.get(e.a)
                vb = e.b.volts if isinstance(e.b, Fixed) else sol.v.get(e.b)
                if on[i]:
                    if sol.current[i] < -1e-12:
                        on[i] = False
                        flip = True
                elif va is not None and (vb is None or va - vb > e.vf + 1e-9):
                    on[i] = True
                    flip = True
            if not flip:
                break
        self._cache[key] = sol
        return sol

    @staticmethod
    def _ctrl_ok(ctrl: tuple, sol: Solution, on: list[bool]) -> bool:
        """Is a controlled channel's gate/base condition met in ``sol``?"""
        if ctrl[0] == "junction":
            j = ctrl[1]
            return on[j] and sol.current[j] > 1e-9
        _, gate, source, vth = ctrl
        vg = gate.volts if isinstance(gate, Fixed) else sol.v.get(gate)
        vs = 0.0 if source is None else (source.volts if isinstance(source, Fixed) else sol.v.get(source))
        return vg is not None and vs is not None and vg - vs >= vth

    def _solve_once(self, held: dict, on: list[bool]) -> Solution:
        els = self.elements
        nodes: list = []
        index: dict = {}

        def node_id(x) -> int:
            if id(x) not in index:
                index[id(x)] = len(nodes)
                nodes.append(x)
            return index[id(x)]

        adj: dict[int, set[int]] = {}
        for i, e in enumerate(els):
            if not on[i]:
                continue
            a, b = node_id(e.a), node_id(e.b)
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

        def fixed_v(x) -> float | None:
            if isinstance(x, Fixed):
                return x.volts
            return held.get(x)

        v: dict = {}
        for x in list(held):
            v[x] = held[x]
        seen: set[int] = set()
        for start in range(len(nodes)):
            if start in seen:
                continue
            comp, stack = [], [start]
            seen.add(start)
            while stack:
                k = stack.pop()
                comp.append(k)
                for m in adj.get(k, ()):
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
            fixed_nodes = [k for k in comp if fixed_v(nodes[k]) is not None]
            if not fixed_nodes:
                continue  # floating component
            unknown = [k for k in comp if fixed_v(nodes[k]) is None]
            for k in fixed_nodes:
                if not isinstance(nodes[k], Fixed):
                    v[nodes[k]] = fixed_v(nodes[k])
            if not unknown:
                continue
            pos = {k: i for i, k in enumerate(unknown)}
            n = len(unknown)
            g = [[0.0] * n for _ in range(n)]
            rhs = [0.0] * n
            comp_set = set(comp)
            for i, e in enumerate(els):
                if not on[i]:
                    continue
                a, b = index[id(e.a)], index[id(e.b)]
                if a not in comp_set:
                    continue
                cond = 1.0 / e.r
                src = cond * e.vf if e.diode else 0.0  # Norton equivalent of the forward drop
                for here, there, sign in ((a, b, 1.0), (b, a, -1.0)):
                    if here not in pos:
                        continue
                    r = pos[here]
                    g[r][r] += cond
                    rhs[r] += sign * src
                    if there in pos:
                        g[r][pos[there]] -= cond
                    else:
                        rhs[r] += cond * fixed_v(nodes[there])
            try:
                x = _solve_linear(g, rhs)
            except ZeroDivisionError:  # pragma: no cover - conductances are all > 0
                log.warning("singular DC component; treating as floating")
                continue
            for k, val in zip(unknown, x):
                v[nodes[k]] = val
        current = []
        for i, e in enumerate(els):
            va, vb = v.get(e.a) if not isinstance(e.a, Fixed) else e.a.volts, \
                v.get(e.b) if not isinstance(e.b, Fixed) else e.b.volts
            if on[i] and va is not None and vb is not None:
                current.append((va - vb - (e.vf if e.diode else 0.0)) / e.r)
            else:
                current.append(0.0)
        return Solution(v={k: val for k, val in v.items() if not isinstance(k, Fixed)}, current=current,
                        on=list(on))

    # -- helpers ----------------------------------------------------------------------------
    def attached(self, net: object) -> list[tuple[int, Element]]:
        """Elements touching ``net`` (index, element)."""
        return [(i, e) for i, e in enumerate(self.elements) if e.a is net or e.b is net]

    def pullups_on(self, net: Net, sol: Solution | None = None) -> list[tuple[Element, float]]:
        """Resistive elements from ``net`` straight to a held node above 0.5 V: (element, reference volts)."""
        sol = sol or self.solve()
        out = []
        for _, e in self.attached(net):
            if e.diode:
                continue
            other = e.b if e.a is net else e.a
            ref = other.volts if isinstance(other, Fixed) else (self.rails.get(other))
            if ref is not None and ref > 0.5:
                out.append((e, ref))
        return out
