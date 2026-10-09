"""Signal-level view of a circuit on top of :mod:`piforge.elec.dcsolve` (shared by ERC and power).

Answers: which pins drive a net high and to what voltage (logic VDD from ``logic_from``/``logic_map``
or a fixed pin voltage), which pins receive (with VIH and absolute-max limits), how much current each
board GPIO sources/sinks when driven, how much current each LED carries in any scenario and whether
an LED has nothing limiting its current.

Scenarios: idle, every driver high, every board GPIO low, and every pair of board GPIOs that share a
DC path (one high, the other low - an LED or resistor between two GPIOs). :meth:`SignalAnalysis.variants`
adds the circuit with every potentiometer at its CCW and CW ends (worst-case wiper positions).

One :class:`SignalAnalysis` is cached on the circuit object, keyed on ``circuit.version`` plus a
fingerprint of every part's ``params`` (they are public mutable dicts), so editing
``r.params["value"]`` invalidates it; the cache lives on the circuit, so nothing keeps it alive.
"""

from __future__ import annotations

from dataclasses import dataclass

from piforge.elec.dcsolve import Element, Fixed, Network, Solution
from piforge.elec.model import Circuit, Net, Part, PinRef, PinType

TOL_V = 0.05            # V, modelling tolerance before declaring a level violation
# V above logic VDD when a part gives no explicit absolute maximum. src: unverified rule of thumb - CMOS
# datasheets give VDD + 0.3..0.6 V (BME280 VDDIO + 0.3 V, MCP3008 VDD + 0.6 V); the low end is used.
GENERIC_VMAX_MARGIN = 0.3
RECEIVER_TYPES = (PinType.INPUT, PinType.BIDIR, PinType.OPEN_DRAIN, PinType.ANALOG)


@dataclass(frozen=True)
class Driver:
    """A pin that can pull its net high: ``v_max`` = nominal high (VDD), ``v_min`` = guaranteed VOH."""

    ref: PinRef
    net: Net
    v_max: float
    v_min: float


def _fingerprint(circuit: Circuit) -> tuple:
    return (circuit.version, repr([(p.ref, p.params) for p in circuit.parts]))


def analysis(circuit: Circuit) -> "SignalAnalysis":
    """Cached :class:`SignalAnalysis` for the current circuit state (version + part params)."""
    key = _fingerprint(circuit)
    hit = getattr(circuit, "_analysis", None)
    if hit is not None and hit[0] == key:
        return hit[1]
    a = SignalAnalysis(circuit)
    circuit._analysis = (key, a)
    return a


def is_board_gpio(ref: PinRef) -> bool:
    return ref.part.category == "board" and ref.pin.name.startswith("GPIO")


class SignalAnalysis:
    """Electrical facts about a circuit (see module docstring)."""

    def __init__(self, circuit: Circuit, *, pot_position: float | None = None):
        self.c = circuit
        self.pot_position = pot_position
        self.net = Network(circuit, pot_position=pot_position)
        self.rails = self.net.rails
        self.base = self.net.solve()
        self.load_outputs: set[PinRef] = set()
        for part in circuit.parts:
            for name in part.params.get("load_outputs", ()):
                self.load_outputs.add(PinRef(part, part.pin(name)))
        self._drivers: list[Driver] | None = None
        self._gpio: dict[PinRef, tuple[float, float]] | None = None
        self._leds: dict[int, tuple[float, bool, str]] | None = None
        self._scenarios: list[tuple[str, Solution, dict]] | None = None
        self._variants: list[SignalAnalysis] | None = None

    @property
    def variant_desc(self) -> str:
        """``""`` for the circuit as drawn, else which pot extreme this analysis models."""
        if self.pot_position is None:
            return ""
        return " with the potentiometer(s) turned fully " + ("CCW" if self.pot_position == 0.0 else "CW")

    def variants(self) -> list["SignalAnalysis"]:
        """This analysis plus, when the circuit has potentiometers, all pots at the CCW and at the CW end."""
        if self._variants is None:
            self._variants = [self]
            if self.pot_position is None and any(p.key == "potentiometer" for p in self.c.parts):
                self._variants += [SignalAnalysis(self.c, pot_position=x) for x in (0.0, 1.0)]
        return self._variants

    # -- voltages ---------------------------------------------------------------------------
    def node_v(self, net: Net | None) -> float | None:
        """Rail voltage, else the idle (no driver) operating point, else ``None``."""
        if net is None:
            return None
        if net in self.rails:
            return self.rails[net]
        return self.base.volts(net)

    def pin_net(self, part: Part, name: str) -> Net | None:
        return self.net.net(part, name)

    def logic_vdd(self, ref: PinRef) -> float | None:
        """Logic supply of a pin: fixed pin voltage, ``logic_map`` / ``logic_from`` supply net voltage."""
        pin, part = ref.pin, ref.part
        if pin.type not in (PinType.POWER_IN, PinType.POWER_OUT, PinType.GND) and pin.voltage is not None:
            return pin.voltage
        sup = (part.params.get("logic_map") or {}).get(pin.name) or part.definition.logic_from
        if sup:
            return self.node_v(self.pin_net(part, sup))
        return None

    def v_max(self, ref: PinRef) -> float | None:
        if ref.pin.v_max is not None:
            return ref.pin.v_max
        if ref.part.category == "board":
            return None
        vdd = self.logic_vdd(ref)
        return None if vdd is None else vdd + GENERIC_VMAX_MARGIN

    def vih(self, ref: PinRef) -> float | None:
        if ref.pin.vih is not None:
            return ref.pin.vih
        vdd = self.logic_vdd(ref)
        if ref.pin.vih_ratio is not None and vdd is not None:
            return ref.pin.vih_ratio * vdd
        return None

    # -- roles ------------------------------------------------------------------------------
    def is_open_drain_bus(self, net: Net) -> bool:
        """Net whose high level comes from pull-ups: an open-drain module pin or any I2C line (SDA and SCL)."""
        return any(r.part.category != "board" and (r.pin.type == PinType.OPEN_DRAIN
                                                   or {"I2C_SDA", "I2C_SCL"} & set(r.pin.functions))
                   for r in net.refs)

    def push_pull_outputs(self, net: Net) -> list[PinRef]:
        """Module OUTPUT pins on ``net`` (H-bridge/driver load outputs excluded)."""
        return [r for r in net.refs if r.pin.type == PinType.OUTPUT and r not in self.load_outputs]

    def drivers(self) -> list[Driver]:
        """Every pin that can actively drive its (non-rail) net high, with its high-level range."""
        if self._drivers is not None:
            return self._drivers
        out: list[Driver] = []
        for net in self.c.nets:
            if net in self.rails:
                continue
            od_bus = self.is_open_drain_bus(net)
            for r in net.refs:
                if r in self.load_outputs:
                    continue
                if r.pin.type == PinType.OUTPUT:
                    vdd = self.logic_vdd(r)
                    if vdd is None:
                        continue
                    vmin = r.pin.voh if r.pin.voh is not None and r.pin.voh <= vdd else vdd
                    out.append(Driver(r, net, vdd, vmin))
                elif is_board_gpio(r) and not od_bus:
                    vdd = r.pin.voltage or 3.3
                    out.append(Driver(r, net, vdd, r.pin.voh if r.pin.voh is not None else vdd))
        self._drivers = out
        return out

    def receivers(self, net: Net, *, exclude: PinRef | None = None) -> list[PinRef]:
        return [r for r in net.refs if r.pin.type in RECEIVER_TYPES and r != exclude]

    def affected(self, sol: Solution) -> list[Net]:
        """Nets whose voltage in ``sol`` is defined and differs from the idle operating point."""
        out = []
        for net in self.c.nets:
            if net in self.rails:
                continue
            v = sol.volts(net)
            if v is None:
                continue
            b = self.base.volts(net)
            if b is None or abs(v - b) > 1e-6:
                out.append(net)
        return out

    # -- GPIO pairs -----------------------------------------------------------------------------
    def gpio_nets(self) -> list[tuple[Net, float]]:
        """Non-rail nets with a board GPIO, with the GPIO's high level."""
        out = []
        for net in self.c.nets:
            if net in self.rails:
                continue
            g = next((r for r in net.refs if is_board_gpio(r)), None)
            if g is not None:
                out.append((net, g.pin.voltage or 3.3))
        return out

    def gpio_pairs(self) -> list[tuple[Net, float, Net]]:
        """(high net, its level, low net) for GPIO nets joined by a DC path that avoids rails."""
        gnets = self.gpio_nets()
        if len(gnets) < 2:
            return []
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            while parent.get(x, x) != x:
                x = parent[x]
            return x

        for e in self.net.elements:
            if any(isinstance(x, Fixed) or x in self.rails for x in (e.a, e.b)):
                continue
            ra, rb = find(id(e.a)), find(id(e.b))
            if ra != rb:
                parent[ra] = rb
        out = []
        for a, va in gnets:
            for b, _ in gnets:
                if a is not b and find(id(a)) == find(id(b)):
                    out.append((a, va, b))
        return out

    # -- currents ---------------------------------------------------------------------------
    def gpio_currents(self) -> dict[PinRef, tuple[float, float]]:
        """Per connected board GPIO: (mA sourced when driven high, mA sunk when driven low), worst case over
        the GPIO alone and paired with every other GPIO it shares a DC path with (one high, one low)."""
        if self._gpio is not None:
            return self._gpio
        res: dict[PinRef, tuple[float, float]] = {}
        els = self.net.elements
        pairs = self.gpio_pairs()
        for net in self.c.nets:
            if net in self.rails:
                continue
            for r in net.refs:
                if not is_board_gpio(r):
                    continue
                vdd = r.pin.voltage or 3.3
                hi = self.net.solve({net: vdd}).current_out(net, els) * 1000.0
                lo = -self.net.solve({net: 0.0}).current_out(net, els) * 1000.0
                for h, vh, low in pairs:
                    if h is net:
                        hi = max(hi, self.net.solve({h: vh, low: 0.0}).current_out(net, els) * 1000.0)
                    elif low is net:
                        lo = max(lo, -self.net.solve({h: vh, low: 0.0}).current_out(net, els) * 1000.0)
                for part in self.c.parts:  # parts powered straight from this GPIO
                    s = part.definition.supply
                    if s is not None and part is not r.part and self.pin_net(part, s.pin) is net:
                        hi += part.supply_ma()[1]
                res[r] = (max(hi, 0.0), max(lo, 0.0))
        self._gpio = res
        return res

    def scenarios(self) -> list[tuple[str, Solution, dict]]:
        """(description, solution, held-nets) for idle, every driver high, every GPIO low and every
        DC-connected GPIO pair at opposite levels."""
        if self._scenarios is not None:
            return self._scenarios
        out = [("idle", self.base, {})]
        for d in self.drivers():
            out.append((f"{d.ref.label} high", self.net.solve({d.net: d.v_max}), {d.net: d.v_max}))
        for net, _ in self.gpio_nets():
            out.append((f"{self._gpio_label(net)} low", self.net.solve({net: 0.0}), {net: 0.0}))
        for h, vh, low in self.gpio_pairs():
            held = {h: vh, low: 0.0}
            out.append((f"{self._gpio_label(h)} high and {self._gpio_label(low)} low", self.net.solve(held), held))
        self._scenarios = out
        return out

    @staticmethod
    def _gpio_label(net: Net) -> str:
        g = next((r for r in net.refs if is_board_gpio(r)), None)
        return g.label if g is not None else net.name

    def _hard(self, node, held: dict, skip: int, sol: Solution, seen: set[int]) -> bool:
        """Node held at a voltage directly or only through other conducting diodes (no resistance)."""
        if isinstance(node, Fixed) or node in self.rails or node in held:
            return True
        if id(node) in seen:
            return False
        seen.add(id(node))
        for i, e in self.net.attached(node):
            if i == skip or not (e.diode or e.tag == "channel") or not sol.on[i]:
                continue
            other = e.b if e.a is node else e.a
            if self._hard(other, held, skip, sol, seen):
                return True
        return False

    def led_currents(self) -> dict[int, tuple[float, bool, str]]:
        """Per LED element index: (max forward mA over scenarios, no-resistor flag, scenario)."""
        if self._leds is not None:
            return self._leds
        res: dict[int, tuple[float, bool, str]] = {}
        for i, e in enumerate(self.net.elements):
            if e.tag != "led":
                continue
            best, bare, where = 0.0, False, ""
            for desc, sol, held in self.scenarios():
                if not sol.on[i] or sol.current[i] <= 1e-9:
                    continue
                ma = sol.current[i] * 1000.0
                if self._hard(e.a, held, i, sol, set()) and self._hard(e.b, held, i, sol, set()):
                    bare = True
                if ma > best:
                    best, where = ma, desc
            res[i] = (best, bare, where)
        self._leds = res
        return res

    def element_nets(self, e: Element) -> list[Net]:
        return [x for x in (e.a, e.b) if isinstance(x, Net)]
