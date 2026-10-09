"""Circuit model: pin/part definitions, part instances, nets and the :class:`Circuit` container.

A :class:`PartDef` is an immutable library entry (pins with electrical limits, supply, I2C
addresses, simulation/mechanical cross-links). :meth:`Circuit.add` instantiates it as a
:class:`Part` with a reference designator (``U1``, ``R1``, ``D1``…); ``part["GPIO17"]`` returns a
:class:`PinRef`; :meth:`Circuit.connect` joins pins into :class:`Net` objects.

Pins of one part that are internally connected (the eight GND pins of a Pi, two GND pins of a
module, or explicit ``PartDef.ties``) always end up in the same net: a net is an electrical node.
Each ``connect`` call is remembered in ``Net.groups`` so wiring tables know which header pin a wire
was plugged into.
"""

from __future__ import annotations

import copy
import difflib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from piforge.core.errors import NotFoundError, PiForgeError
from piforge.core.report import jsonable
from piforge.elec.units import parse_quantity

log = logging.getLogger(__name__)


class CircuitError(PiForgeError):
    """Invalid use of the circuit API (bad parameter, foreign pin, conflicting net names…)."""


class PinNotFoundError(NotFoundError):
    """A pin name/number does not exist on a part (or is ambiguous); lists close matches."""

    def __init__(self, where: str, name: object, candidates=(), *, ambiguous: list[str] | None = None):
        super().__init__(f"pin on {where}", name, candidates)
        if ambiguous:
            self.suggestions = list(ambiguous)
            self.args = (f"Ambiguous pin name {name!r} on {where}: matches {', '.join(ambiguous)}. "
                         "Use the pin name or the physical pin number.",)


class PinType(str, Enum):
    """Electrical role of a pin (KiCad-like)."""

    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    GND = "gnd"
    INPUT = "input"
    OUTPUT = "output"
    BIDIR = "bidir"
    OPEN_DRAIN = "open_drain"
    PASSIVE = "passive"
    ANALOG = "analog"
    NC = "nc"


@dataclass(frozen=True)
class Pin:
    """One pin. Voltages in V, currents in mA. ``*_ratio`` thresholds are fractions of the logic VDD."""

    name: str
    number: str
    type: PinType
    voltage: float | None = None       # POWER_OUT nominal V; logic pins: fixed logic-domain VDD
    v_max: float | None = None         # absolute max input voltage
    vih: float | None = None
    vil: float | None = None
    voh: float | None = None
    vol: float | None = None
    i_max_ma: float | None = None
    functions: tuple[str, ...] = ()    # e.g. ("GPIO2", "BCM2", "I2C1_SDA")
    aliases: tuple[str, ...] = ()
    required: bool = False             # must be connected (ERC.UNCONNECTED)
    vih_ratio: float | None = None     # VIH as a fraction of the logic VDD (CMOS: 0.7)
    vil_ratio: float | None = None


@dataclass(frozen=True)
class Supply:
    """Operating supply range (V) and current draw (mA) of a part, taken at ``pin``."""

    v_min: float
    v_max: float
    i_typ_ma: float
    i_max_ma: float
    pin: str = "VCC"


@dataclass(frozen=True)
class PartDef:
    """Immutable library entry. See ``piforge.elec.library`` for the catalogue."""

    key: str
    name: str
    category: str
    pins: tuple[Pin, ...]
    supply: Supply | None = None
    i2c_addresses: tuple[int, ...] = ()        # possible addresses; params["i2c_address"] picks one
    params: dict = field(default_factory=dict)  # defaults; e.g. LED {"color": "red", "vf": 2.0, "if_max_ma": 20}
    sim: dict = field(default_factory=dict)     # {"twin": "button", "spice": "led_red", "pins": {role: pin}}
    mech: str | None = None                     # piforge.mech.modules key
    datasheet: str = ""
    notes: str = ""
    logic_from: str | None = None               # supply pin whose net voltage sets the IO logic level
    footprint: str = ""                         # KiCad footprint id
    features: tuple[str, ...] = ()              # e.g. "inductive", "motor", "driver", "flyback", "psu"
    ref_prefix: str = ""                        # reference designator prefix (default from category)
    variants: dict = field(default_factory=dict)  # {"color": {"red": {"vf": 2.0, ...}, ...}}
    ties: tuple[tuple[str, ...], ...] = ()      # groups of differently named, internally connected pins

    def __hash__(self) -> int:
        return hash((self.key, self.name, self.category))


class _Required:
    def __repr__(self) -> str:
        return "REQUIRED"

    def __copy__(self) -> "_Required":
        return self

    def __deepcopy__(self, memo: dict) -> "_Required":  # keep the sentinel identity through deepcopy
        return self


REQUIRED: Any = _Required()
"""Sentinel for a numeric parameter with no default (e.g. a resistor's ``value``)."""

_PREFIX_BY_CATEGORY = {
    "board": "U", "power": "PS", "passive": "R", "led": "D", "diode": "D", "transistor": "Q",
    "switch": "SW", "sensor": "U", "display": "U", "adc": "U", "ic": "U", "module": "U", "motor": "M",
    "driver": "U", "relay": "K", "audio": "BZ", "light": "U", "connector": "J", "camera": "U",
    "regulator": "U", "interface": "U",
}
# Behavioural keys any part may override (documented in piforge.elec.library), besides its own defaults.
GENERIC_PARAMS = frozenset({
    "pullups", "pullup_ohms", "input_loads", "load_ohms", "load_pins", "loads", "coils", "load_outputs",
    "load_supply", "passthrough", "logic_map", "extra_supplies", "motor_pins", "isolated_pins", "ground_ref",
    "label", "notes", "mpn",
})
_PULL_VALUES = {"up": "up", "pullup": "up", "pull_up": "up", "pud_up": "up", "high": "up",
                "down": "down", "pulldown": "down", "pull_down": "down", "pud_down": "down", "low": "down",
                "none": "none", "off": "none", "no": "none", "float": "none", "pud_off": "none"}
_INTERFACE_NAMES = {"i2c": "i2c1", "i2c1": "i2c1", "i2c_arm": "i2c1", "spi": "spi0", "spi0": "spi0",
                    "spi1": "spi1", "uart": "uart0", "uart0": "uart0", "serial": "uart0",
                    "onewire": "onewire", "one_wire": "onewire", "1wire": "onewire", "w1": "onewire",
                    "pwm": "pwm", "pcm": "pcm", "i2s": "pcm", "id_eeprom": "id_eeprom", "camera": "camera"}
_NUM_RE = re.compile(r"(?:PIN|PHYS|PHYSICAL|HEADER)?(\d+)")


def _norm(text: str) -> str:
    return re.sub(r"[\s_]", "", str(text)).upper()


def is_power(pin: Pin) -> bool:
    """True for supply-type pins (power in/out, ground)."""
    return pin.type in (PinType.POWER_IN, PinType.POWER_OUT, PinType.GND)


@dataclass(frozen=True)
class PinRef:
    """A pin of a specific part instance; what :meth:`Circuit.connect` takes."""

    part: "Part"
    pin: Pin

    @property
    def label(self) -> str:
        """``U1.GPIO17``; duplicated names get the pin number: ``U1.GND#6``."""
        dup = sum(1 for p in self.part.definition.pins if p.name == self.pin.name) > 1
        return f"{self.part.ref}.{self.pin.name}" + (f"#{self.pin.number}" if dup else "")

    def __str__(self) -> str:
        return self.label

    def __repr__(self) -> str:
        return f"PinRef({self.label})"


class Part:
    """A part instance in a circuit. ``part[name]`` resolves a pin (name, alias, number, function)."""

    def __init__(self, circuit: "Circuit", ref: str, definition: PartDef, params: dict):
        self.circuit = circuit
        self.ref = ref
        self.definition = definition
        self.params = params
        ties: dict[str, set[str]] = {}
        for group in definition.ties:
            for n in group:
                ties.setdefault(n, set()).update(group)
        self._ties = ties

    # -- identity ---------------------------------------------------------------------------
    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def category(self) -> str:
        return self.definition.category

    @property
    def features(self) -> tuple[str, ...]:
        return self.definition.features

    def __repr__(self) -> str:
        return f"Part({self.ref}: {self.key})"

    def pins(self) -> tuple[Pin, ...]:
        """All pins of the part in definition (physical) order."""
        return self.definition.pins

    # -- pin resolution ---------------------------------------------------------------------
    def __getitem__(self, name: str | int) -> PinRef:
        return PinRef(self, self.pin(name, allocate=True))

    def pin(self, name: str | int, *, allocate: bool = False) -> Pin:
        """Resolve a pin by name, alias, number (``11``, ``"pin11"``) or alternate function.

        Several pins with the same name (``GND``, ``5V``, ``3V3``): with ``allocate`` the first one
        not yet connected in the circuit (physical order), otherwise the first one.
        """
        pins = self.definition.pins
        where = f"{self.ref} ({self.key})"
        if isinstance(name, bool) or not isinstance(name, (str, int)):
            raise PinNotFoundError(where, name, self._candidates())
        if isinstance(name, int):
            hits = [p for p in pins if p.number == str(name)]
            if not hits:
                raise PinNotFoundError(where, name, self._candidates())
            return hits[0]
        key = _norm(name)
        if not key:
            raise PinNotFoundError(where, name, self._candidates())
        hits = [p for p in pins if _norm(p.name) == key]
        if not hits:
            m = _NUM_RE.fullmatch(key)
            if m:
                hits = [p for p in pins if p.number == str(int(m.group(1)))]
            if not hits:
                hits = [p for p in pins if _norm(p.number) == key]
        if not hits:
            hits = [p for p in pins if key in {_norm(a) for a in p.aliases}]
        if not hits:
            hits = [p for p in pins if key in {_norm(f) for f in p.functions}]
            if len(hits) > 1:
                desc = [f"{p.name} (pin {p.number})" for p in hits]
                raise PinNotFoundError(where, name, ambiguous=desc)
        if not hits:
            raise PinNotFoundError(where, name, self._candidates())
        if len(hits) > 1:
            if len({p.name for p in hits}) > 1:
                raise PinNotFoundError(where, name, ambiguous=[f"{p.name} (pin {p.number})" for p in hits])
            if allocate:
                for p in hits:
                    if not self.circuit.is_connected(PinRef(self, p)):
                        return p
        return hits[0]

    def _candidates(self) -> list[str]:
        out: list[str] = []
        for p in self.definition.pins:
            out.append(p.name)
            out.extend(p.aliases)
            out.extend(f for f in p.functions if f.startswith(("GPIO", "BCM")))
        return sorted(set(out))

    # -- electrical helpers -----------------------------------------------------------------
    def tied(self, pin: Pin) -> tuple[Pin, ...]:
        """Pins internally connected to ``pin`` (same-named power/ground pins and ``PartDef.ties``)."""
        out = []
        names = self._ties.get(pin.name, set())
        for p in self.definition.pins:
            if p is pin:
                continue
            same = p.name == pin.name and is_power(p) and is_power(pin)
            if same or p.name in names:
                out.append(p)
        return tuple(out)

    def output_voltage(self, pin: Pin) -> float | None:
        """Nominal voltage of a POWER_OUT pin (adjustable outputs read ``params["vout"]``)."""
        if pin.type != PinType.POWER_OUT:
            return None
        if self.params.get("vout_pin") == pin.name and self.params.get("vout") is not None:
            return float(self.params["vout"])
        return pin.voltage

    def supply_ma(self) -> tuple[float, float]:
        """(typical, max) supply current in mA, scaled by ``params["count"]`` for per-unit parts."""
        s = self.definition.supply
        if s is None:
            return (0.0, 0.0)
        n = float(self.params.get("count", 1)) if self.params.get("supply_per_unit") else 1.0
        return (s.i_typ_ma * n, s.i_max_ma * n)


class Net:
    """An electrical node: the pins in ``refs`` are connected. ``groups`` records each connect call."""

    def __init__(self, name: str, serial: int):
        self.name = name
        self.refs: list[PinRef] = []
        self.groups: list[tuple[PinRef, ...]] = []
        self.user_named = False
        self.serial = serial

    def __contains__(self, ref: object) -> bool:
        return ref in self.refs

    def __iter__(self) -> Iterator[PinRef]:
        return iter(self.refs)

    def __len__(self) -> int:
        return len(self.refs)

    def __repr__(self) -> str:
        return f"Net({self.name}: {', '.join(r.label for r in self.refs)})"

    @property
    def parts(self) -> list["Part"]:
        seen: list[Part] = []
        for r in self.refs:
            if r.part not in seen:
                seen.append(r.part)
        return seen


class Circuit:
    """A set of parts and the nets connecting their pins."""

    def __init__(self, name: str):
        self.name = name
        self.parts: list[Part] = []
        self.nets: list[Net] = []
        self._net_of: dict[PinRef, Net] = {}
        self._config: dict[str, dict] = {}
        self._serial = 0
        self._auto = 0
        self.version = 0  # bumped on every mutation (analysis caches key on it)
        self._analysis: tuple | None = None  # (fingerprint, SignalAnalysis) - see piforge.elec.signals

    # -- parts ------------------------------------------------------------------------------
    def add(self, defn: str | PartDef, ref: str | None = None, **params) -> Part:
        """Instantiate a library part (key or :class:`PartDef`); returns the new :class:`Part`."""
        if isinstance(defn, str):
            from piforge.elec.library import get_def  # lazy: library imports this module
            defn = get_def(defn)
        if not isinstance(defn, PartDef):
            raise CircuitError(f"Circuit.add expects a part key or PartDef, got {type(defn).__name__}")
        if ref is None:
            ref = self._next_ref(defn)
        elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", ref):
            raise CircuitError(f"Invalid reference {ref!r}: use letters, digits and '_' (e.g. 'R1')")
        if any(p.ref == ref for p in self.parts):
            raise CircuitError(f"Reference {ref!r} is already used in circuit {self.name!r}")
        part = Part(self, ref, defn, self._resolve_params(defn, ref, params))
        self.parts.append(part)
        self.version += 1
        return part

    def _next_ref(self, defn: PartDef) -> str:
        prefix = defn.ref_prefix or _PREFIX_BY_CATEGORY.get(defn.category, "U")
        used = {p.ref for p in self.parts}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    @staticmethod
    def _resolve_params(defn: PartDef, ref: str, given: dict) -> dict:
        params = copy.deepcopy(defn.params)
        allowed = set(defn.params) | set(defn.variants) | GENERIC_PARAMS
        for table in defn.variants.values():
            for row in table.values():
                allowed.update(row)
        if defn.i2c_addresses:
            allowed.add("i2c_address")
        unknown = sorted(k for k in given if k not in allowed)
        if unknown:
            close = sorted({m for k in unknown for m in difflib.get_close_matches(k, sorted(allowed), n=3, cutoff=0.6)})
            hint = f" Did you mean {', '.join(close)}?" if close else ""
            raise CircuitError(f"{ref} ({defn.key}): unknown parameter(s) {', '.join(unknown)}.{hint} "
                               f"Known: {', '.join(sorted(allowed))}")
        given = copy.deepcopy(given)
        for k, v in given.items():
            default = defn.params.get(k)
            numeric = default is REQUIRED or (isinstance(default, (int, float)) and not isinstance(default, bool))
            if numeric and isinstance(v, str):
                try:
                    v = parse_quantity(v)
                except ValueError as exc:
                    raise CircuitError(f"{ref} ({defn.key}): parameter {k}={v!r}: {exc}") from None
            params[k] = v
        missing = [k for k, v in params.items() if v is REQUIRED]
        if missing:
            raise CircuitError(f"{ref} ({defn.key}) needs parameter(s) {', '.join(missing)}, "
                               f"e.g. c.add({defn.key!r}, {missing[0]}=...)")
        for pname, table in defn.variants.items():
            choice = params.get(pname)
            if choice not in table:
                raise CircuitError(f"{ref} ({defn.key}): {pname}={choice!r} is not one of {sorted(table)}")
            for k, v in table[choice].items():
                if k not in given:
                    params[k] = copy.deepcopy(v)
        if defn.i2c_addresses:
            addr = params.get("i2c_address", defn.i2c_addresses[0])
            if addr not in defn.i2c_addresses:
                allowed = ", ".join(f"0x{a:02X}" for a in defn.i2c_addresses)
                raise CircuitError(f"{ref} ({defn.key}): i2c_address {addr!r} not possible; choose {allowed}")
            params["i2c_address"] = addr
        return params

    def part(self, ref: str) -> Part:
        """Part by reference designator (``NotFoundError`` with suggestions)."""
        for p in self.parts:
            if p.ref == ref:
                return p
        raise NotFoundError("part", ref, [p.ref for p in self.parts])

    def boards(self) -> list[Part]:
        return [p for p in self.parts if p.category == "board"]

    @property
    def board(self) -> Part | None:
        """The first Raspberry Pi (or other board) in the circuit."""
        b = self.boards()
        return b[0] if b else None

    # -- nets -------------------------------------------------------------------------------
    def is_connected(self, ref: PinRef) -> bool:
        return ref in self._net_of

    def net_of(self, ref: PinRef) -> Net | None:
        """The net containing ``ref`` (``None`` when unconnected)."""
        return self._net_of.get(ref)

    def net(self, name: str) -> Net:
        for n in self.nets:
            if n.name == name:
                return n
        raise NotFoundError("net", name, [n.name for n in self.nets])

    def connect(self, *refs: PinRef, name: str | None = None) -> Net:
        """Connect pins; merges existing nets. Naming: user > power ("GND", "5V", "3V3") > auto "N$n"."""
        if not refs:
            raise CircuitError("connect() needs at least one pin, e.g. c.connect(pi['GPIO17'], r1['1'])")
        for r in refs:
            if not isinstance(r, PinRef):
                raise CircuitError(f"connect() takes pins like part['GPIO17'], got {r!r}")
            if r.part.circuit is not self or r.part not in self.parts:
                raise CircuitError(f"{r.label} belongs to another circuit ({r.part.circuit.name!r})")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise CircuitError(f"Net name must be a non-empty string, got {name!r}")
        uniq: list[PinRef] = []
        for r in refs:
            if r not in uniq:
                uniq.append(r)
        merge: list[Net] = []

        def _take(n: Net | None) -> None:
            if n is not None and n not in merge:
                merge.append(n)

        for r in uniq:
            _take(self._net_of.get(r))
            for t in r.part.tied(r.pin):
                _take(self._net_of.get(PinRef(r.part, t)))
        if name is not None:
            # A name that already exists ("GND", "5V", a user name) means "connect to that net".
            _take(next((n for n in self.nets if n.name == name), None))
        if name is None and not any(n.user_named for n in merge):
            # Power nets are unique per name: a later GND/5V/3V3 net joins an existing net of that name,
            # whatever the wiring order was.
            union = [r for n in merge for r in n.refs] + uniq
            probe = Net("", 0)
            probe.refs = union
            power = self._power_name(probe) if union else None
            if power:  # only user-named nets: two auto-named supplies of equal voltage must stay separate
                _take(next((n for n in self.nets if n.name == power and n.user_named), None))
        user_names = {n.name for n in merge if n.user_named} | ({name} if name else set())
        if len(user_names) > 1:
            raise CircuitError(f"connect() would merge nets named {sorted(user_names)}; "
                               "pins already belong to differently named nets")
        if merge:
            merge.sort(key=lambda n: n.serial)
            target, others = merge[0], merge[1:]
        else:
            self._serial += 1
            target, others = Net("", self._serial), []
            self.nets.append(target)
        for o in others:
            for r in o.refs:
                if r not in target.refs:
                    target.refs.append(r)
            target.groups.extend(o.groups)
            target.user_named = target.user_named or o.user_named
            if o.user_named:
                target.name = o.name
            self.nets.remove(o)
        for r in uniq:
            if r not in target.refs:
                target.refs.append(r)
        target.groups.append(tuple(uniq))
        for r in target.refs:
            self._net_of[r] = target
        self._name_net(target, name)
        self.version += 1
        return target

    def _name_net(self, net: Net, user: str | None) -> None:
        if user:
            net.name, net.user_named = user, True
            return
        if net.user_named:
            return
        power = self._power_name(net)
        if power:
            net.name = self._unique(power, net)
        elif not net.name.startswith("N$"):
            self._auto += 1
            net.name = self._unique(f"N${self._auto}", net)

    def _power_name(self, net: Net) -> str | None:
        if any(r.pin.type == PinType.GND for r in net.refs):
            return "GND"
        for r in net.refs:
            v = r.part.output_voltage(r.pin)
            if v is not None:
                if abs(v - 3.3) < 0.2:
                    return "3V3"
                if abs(v - 5.0) < 0.3:
                    return "5V"
                return f"{v:g}V"
        return None

    def _unique(self, base: str, net: Net) -> str:
        taken = {n.name for n in self.nets if n is not net}
        if base not in taken:
            return base
        i = 2
        while f"{base}_{i}" in taken:
            i += 1
        return f"{base}_{i}"

    # -- configuration ----------------------------------------------------------------------
    def configure(self, part: Part, *, pulls: dict | None = None, interfaces: dict | None = None) -> None:
        """Record GPIO pulls (``{"GPIO27": "up"}``) and enabled interfaces (``{"i2c": True}``)."""
        if not isinstance(part, Part) or part not in self.parts:
            raise CircuitError(f"configure() needs a part of circuit {self.name!r}, got {part!r}")
        cfg = self._config.setdefault(part.ref, {"pulls": {}, "interfaces": {}})
        for key, value in (pulls or {}).items():
            pin = part.pin(key)
            if pin.type not in (PinType.BIDIR, PinType.INPUT, PinType.OPEN_DRAIN):
                raise CircuitError(f"{part.ref}.{pin.name} is a {pin.type.value} pin; pulls apply to GPIOs")
            norm = _PULL_VALUES.get(str(value).strip().lower()) if value is not None else "none"
            if norm is None:
                raise CircuitError(f"Pull {value!r} for {part.ref}.{pin.name}: use 'up', 'down' or 'none'")
            cfg["pulls"][pin.name] = norm
        for key, value in (interfaces or {}).items():
            canon = _INTERFACE_NAMES.get(str(key).strip().lower())
            if canon is None:
                raise CircuitError(f"Unknown interface {key!r}; use one of {sorted(set(_INTERFACE_NAMES.values()))}")
            if canon in ("pwm", "onewire") and value not in (True, False, None):
                vals = value if isinstance(value, (list, tuple)) else [value]
                if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 27 for v in vals):
                    raise CircuitError(f"interfaces[{key!r}] takes BCM GPIO numbers 0-27, got {value!r}")
                value = [int(v) for v in vals]
            cfg["interfaces"][canon] = value
        self.version += 1

    def config(self, part: Part | str) -> dict:
        """``{"pulls": {...}, "interfaces": {...}}`` recorded by :meth:`configure` (a copy)."""
        ref = part.ref if isinstance(part, Part) else str(part)
        return copy.deepcopy(self._config.get(ref, {"pulls": {}, "interfaces": {}}))

    # -- queries ----------------------------------------------------------------------------
    def bcm_of(self, ref: PinRef, *, kind: str = "in", depth: int = 4) -> int | None:
        """BCM number of the board GPIO driving ``ref`` (directly, through series resistors, or through a
        driver's ``params["passthrough"][kind]`` output->input map), else ``None``."""
        start = self.net_of(ref)
        if start is None:
            return None
        seen = {id(start)}
        frontier = [start]
        for _ in range(depth + 1):
            nxt: list[Net] = []
            for net in frontier:
                for r in net.refs:
                    if r.part.category == "board":
                        for f in r.pin.functions:
                            if f.startswith("BCM"):
                                return int(f[3:])
                if any(is_power(r.pin) for r in net.refs):
                    continue  # never trace out of a rail
                for r in net.refs:
                    cand: list[PinRef] = []
                    if r.part.key == "resistor":
                        cand = [PinRef(r.part, p) for p in r.part.pins() if p is not r.pin]
                    mapping = (r.part.params.get("passthrough") or {}).get(kind) or {}
                    if r.pin.name in mapping:
                        cand.append(PinRef(r.part, r.part.pin(mapping[r.pin.name])))
                    for c in cand:
                        n = self.net_of(c)
                        if n is not None and id(n) not in seen:
                            seen.add(id(n))
                            nxt.append(n)
            frontier = nxt
        return None

    def to_dict(self) -> dict:
        """JSON-friendly description of parts, nets and configuration."""
        return {
            "name": self.name,
            "parts": [{"ref": p.ref, "key": p.key, "name": p.name, "category": p.category,
                       "params": jsonable(p.params)} for p in self.parts],
            "nets": [{"name": n.name, "pins": [r.label for r in n.refs],
                      "numbers": [f"{r.part.ref}:{r.pin.number}" for r in n.refs]} for n in self.nets],
            "config": jsonable(self._config),
        }
