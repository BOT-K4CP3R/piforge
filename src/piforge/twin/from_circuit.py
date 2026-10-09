"""Auto-wire a twin from an electronics netlist (``piforge.elec.model.Circuit``).

Every part whose definition has ``sim["twin"]`` (a :data:`~piforge.twin.devices.DEVICE_TYPES` name)
becomes a :class:`~piforge.twin.config.DeviceConfig` with id = part ref.

**Pin roles** come from ``PartDef.sim["pins"]`` — ``{role: pin}``, ``{role: (candidate, …)}`` or
``"PIN:kind"`` to follow a driver board's ``params["passthrough"][kind]`` output→input map (e.g. a
28BYJ-48 ``"A:in"`` through the ULN2003 to ``IN1``, a DC motor ``"M+:pwm"`` to the L298N ``ENA``).
Each candidate pin is traced to a Raspberry Pi GPIO through its net, series resistors and
passthrough maps (≤ 4 hops, never through GND/3V3/5V). Parts without ``sim["pins"]`` fall back to
name heuristics (device pin aliases such as ``TRIG`` → ``trigger``, ``INk``↔``OUTk`` driver channels).

**Buses**: roles ``sda``/``scl`` (or pins on SDA/SCL) → I2C (bus from the GPIOs, address from
``params["i2c_address"]``, else the definition's first ``i2c_addresses``); an MCP3008 whose clock is
on SCLK → SPI (chip-select from the CE GPIO, ``cs_pin`` for a plain GPIO), otherwise bit-banged pins.

**Shift registers** (parts with ``params["shift_register"]``, e.g. 74HC595/74HCT595): chips daisy-chained
QH' → SER form one ``shift_register_74hc595`` device (id = ref of the chip whose SER comes from the Pi,
``length`` = chain length, ``chain`` = refs in order). SER on MOSI + SRCLK on SCLK → SPI (chip-select from
RCLK on CE0/CE1, else RCLK as a ``latch`` pin); otherwise bit-banged ``data``/``clock``/``latch`` pins.
A stepper whose coil inputs trace (through e.g. a ULN2003A's ``passthrough``) to chain outputs instead of
GPIOs gets ``coil_source={"device": <chain id>, "bits": [...]}``.

**Params**: definition params, overridden by part params, plus wiring facts — a contact whose other
side is GND is ``active_low``; an LED driven on its cathode is ``active_high=False``.

**Pulls** (``TwinConfig.pulls``): physical resistors from a GPIO net straight to a rail and modules'
on-board ``params["pullups"]`` on GPIO nets. Internal pulls declared with
``Circuit.configure(pi, pulls=…)`` are the firmware's job and are *not* copied, so firmware that
forgets them fails in the twin as it would on hardware.

Duck-typed: works with any object graph following the Task 4 interface; ``piforge.elec`` is not
imported here.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any

from piforge.core.errors import PiForgeError
from piforge.core.report import jsonable
from piforge.twin.bus import SPI_CS_GPIO
from piforge.twin.config import BOARDS, DeviceConfig, TwinConfig
from piforge.twin.devices import DEVICE_TYPES
from piforge.twin.gpio import I2C_PINS

log = logging.getLogger(__name__)

_GPIO_RE = re.compile(r"^(?:GPIO|BCM)(\d{1,2})$", re.IGNORECASE)
_IN_RE = re.compile(r"^([AB]?)IN(\d+)$", re.IGNORECASE)
_OUT_RE = re.compile(r"^([AB]?)O(?:UT)?(\d+)$", re.IGNORECASE)
_SCLK = {11: (0, 10, 9), 21: (1, 20, 19)}               # SCLK → (SPI bus, MOSI, MISO)
_I2C_BY_PAIR = {(sda, scl): bus for bus, (sda, scl) in I2C_PINS.items()}
_CONTACTS = {"button", "switch", "limit_switch"}
_ANODE = {"a", "anode", "+", "p", "pos"}
_CATHODE = {"k", "c", "cathode", "-", "n", "neg"}
_CS_NAMES = ("cs", "csb", "ce", "ss", "nss", "csshdn", "shdn")
MAX_HOPS = 4


def _norm(name: Any) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum() or ch in "+-_")


def _pin_key(pin: Any) -> tuple[str, str]:
    return (str(getattr(pin, "name", "")), str(getattr(pin, "number", "")))


def _pin_type(pin: Any) -> str:
    t = getattr(pin, "type", "")
    return str(getattr(t, "value", t)).lower()


def _pin_bcm(pin: Any) -> int | None:
    for label in (getattr(pin, "name", ""), *getattr(pin, "functions", ()), *getattr(pin, "aliases", ())):
        m = _GPIO_RE.match(str(label))
        if m and 0 <= int(m.group(1)) <= 27:
            return int(m.group(1))
    return None


def _definition(part: Any) -> Any:
    return getattr(part, "definition", None) or getattr(part, "defn", None)


def _pins(part: Any) -> list[Any]:
    pins = part.pins() if callable(getattr(part, "pins", None)) else getattr(_definition(part), "pins", ())
    return list(pins)


def _params(part: Any) -> dict:
    return {**dict(getattr(_definition(part), "params", {}) or {}), **dict(getattr(part, "params", {}) or {})}


def _pin_named(part: Any, name: str) -> Any:
    for p in _pins(part):
        if str(p.name) == name:
            return p
    for p in _pins(part):
        if _norm(p.name) == _norm(name) or name in getattr(p, "aliases", ()):
            return p
    return None


def _find_pi(parts: list[Any]) -> Any:
    best, best_n = None, 0
    for part in parts:
        n = sum(1 for p in _pins(part) if _pin_bcm(p) is not None)
        if n > best_n:
            best, best_n = part, n
    return best if best_n >= 8 else None


class _Netlist:
    """Nets classified as Pi GPIO / rail, plus pass-through edges (resistors, driver maps)."""

    def __init__(self, circuit: Any, pi: Any) -> None:
        self.gpio: dict[int, int] = {}            # net id → BCM
        self.rail: dict[int, str] = {}            # net id → "gnd" | "3v3" | "5v"
        self.nets: dict[int, Any] = {}
        # net id → [(other net id, kind, channel, part)]; kind None = resistor (any direction)
        self.edges: dict[int, list[tuple[int, str | None, int | None, Any]]] = {}
        # (part, pin) → net from the nets themselves: no Part[...] lookups (Task 4's pi["GND"] hands
        # out the next unconnected GND pin, so name lookups are not side-effect free).
        self._net_of: dict[tuple[int, tuple[str, str]], Any] = {}
        for net in getattr(circuit, "nets", []):
            self.nets[id(net)] = net
            for ref in getattr(net, "refs", []):
                self._net_of[(id(ref.part), _pin_key(ref.pin))] = net
            if _norm(getattr(net, "name", "")) in ("gnd", "3v3", "5v"):
                self.rail[id(net)] = _norm(net.name)
        for pin in _pins(pi):
            net = self.net(pi, pin)
            if net is None:
                continue
            bcm = _pin_bcm(pin)
            if bcm is not None:
                self.gpio[id(net)] = bcm
            elif self._rail_of(pin):
                self.rail[id(net)] = self._rail_of(pin) or ""
        for part in getattr(circuit, "parts", []):
            if part is not pi:
                self._add_edges(part)

    @staticmethod
    def _rail_of(pin: Any) -> str | None:
        name, kind = _norm(getattr(pin, "name", "")), _pin_type(pin)
        if kind == "gnd" or name.startswith("gnd"):
            return "gnd"
        if name in ("3v3", "33v") or (kind == "power_out" and getattr(pin, "voltage", None) == 3.3):
            return "3v3"
        if name in ("5v", "5v0") or (kind == "power_out" and getattr(pin, "voltage", None) == 5.0):
            return "5v"
        return None

    def net(self, part: Any, pin: Any) -> Any:
        return self._net_of.get((id(part), _pin_key(pin)))

    def _edge(self, src: Any, dst: Any, kind: str | None, ch: int | None, part: Any) -> None:
        if src is not None and dst is not None:
            self.edges.setdefault(id(src), []).append((id(dst), kind, ch, part))

    def _add_edges(self, part: Any) -> None:
        d = _definition(part)
        pins = _pins(part)
        key = str(getattr(d, "key", "")).lower()
        cat = str(getattr(d, "category", "")).lower()
        if len(pins) == 2 and not (getattr(d, "sim", None) or {}).get("twin") and (
                key == "resistor" or "resistor" in key or cat == "resistor"):
            a, b = (self.net(part, p) for p in pins)
            self._edge(a, b, None, None, part)
            self._edge(b, a, None, None, part)
            return
        passthrough = _params(part).get("passthrough") or {}
        if isinstance(passthrough, dict) and passthrough:
            for kind, mapping in passthrough.items():
                for out_name, in_name in dict(mapping or {}).items():
                    p_out, p_in = _pin_named(part, str(out_name)), _pin_named(part, str(in_name))
                    if p_out is not None and p_in is not None:
                        m = re.search(r"(\d+)$", str(in_name))
                        self._edge(self.net(part, p_out), self.net(part, p_in), str(kind),
                                   int(m.group(1)) if m else None, part)
            return
        ins: dict[tuple[str, str], Any] = {}
        outs: dict[tuple[str, str], Any] = {}
        for p in pins:                                   # heuristic INk ↔ OUTk driver channels
            m_in, m_out = _IN_RE.match(str(p.name)), _OUT_RE.match(str(p.name))
            if m_in:
                ins[(m_in.group(1).upper(), m_in.group(2))] = p
            elif m_out:
                outs[(m_out.group(1).upper(), m_out.group(2))] = p
        for k, pin_in in ins.items():
            if k in outs:
                self._edge(self.net(part, outs[k]), self.net(part, pin_in), "in", int(k[1]), part)

    def trace(self, net: Any, kind: str = "in") -> tuple[int | None, int | None]:
        """(BCM, driver channel) reached from ``net`` through resistors and ``kind`` passthroughs."""
        if net is None:
            return None, None
        start = id(net)
        seen = {start}
        queue: deque[tuple[int, int, int | None]] = deque([(start, 0, None)])
        while queue:
            nid, hops, ch = queue.popleft()
            if nid in self.gpio:
                return self.gpio[nid], ch
            if hops >= MAX_HOPS or (nid in self.rail and nid != start):
                continue
            for nxt, e_kind, e_ch, _part in self.edges.get(nid, []):
                if nxt in seen or nxt in self.rail or (e_kind is not None and e_kind != kind):
                    continue
                seen.add(nxt)
                queue.append((nxt, hops + 1, e_ch if e_ch is not None else ch))
        return None, None

    def trace_to(self, net: Any, targets: dict[int, Any], kind: str = "in") -> Any:
        """First ``targets[net id]`` reached from ``net`` through resistors and ``kind`` passthroughs."""
        if net is None:
            return None
        start = id(net)
        seen = {start}
        queue: deque[tuple[int, int]] = deque([(start, 0)])
        while queue:
            nid, hops = queue.popleft()
            if nid in targets:
                return targets[nid]
            if hops >= MAX_HOPS or (nid in self.rail and nid != start):
                continue
            for nxt, e_kind, _ch, _part in self.edges.get(nid, []):
                if nxt in seen or nxt in self.rail or (e_kind is not None and e_kind != kind):
                    continue
                seen.add(nxt)
                queue.append((nxt, hops + 1))
        return None

    def pulls(self, parts: list[Any]) -> dict[int, str]:
        """GPIO pulls from modules' on-board ``pullups`` and resistors straight to a rail."""
        out: dict[int, str] = {}
        for part in parts:
            pull_map = _params(part).get("pullups") or {}
            if not isinstance(pull_map, dict) or _params(part).get("pullup_ohms", 0) is None:
                continue
            for pin_name, spec in pull_map.items():
                pin = _pin_named(part, str(pin_name))
                net = self.net(part, pin) if pin is not None else None
                if net is None or id(net) not in self.gpio:
                    continue
                target = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 else 3.3
                if isinstance(target, (int, float)):
                    out[self.gpio[id(net)]] = "up" if target > 0 else "down"
                else:
                    tpin = _pin_named(part, str(target))
                    tnet = self.net(part, tpin) if tpin is not None else None
                    out[self.gpio[id(net)]] = "down" if tnet is not None and self.rail.get(id(tnet)) == "gnd" else "up"
        for nid, bcm in self.gpio.items():
            for nxt, kind, _ch, _part in self.edges.get(nid, []):
                if kind is None and nxt in self.rail:
                    out[bcm] = "down" if self.rail[nxt] == "gnd" else "up"
        return out


def twin_config_from_circuit(circuit: Any) -> TwinConfig:
    """Build a :class:`TwinConfig` from a circuit's parts, nets and ``PartDef.sim`` tags."""
    parts = list(getattr(circuit, "parts", []))
    pi = _find_pi(parts)
    if pi is None:
        raise PiForgeError("the circuit has no Raspberry Pi board part (no part with GPIO pins); "
                           "add one, e.g. circuit.add('rpi4b')")
    key = str(getattr(_definition(pi), "key", "")).lower()
    nl = _Netlist(circuit, pi)
    devices: list[DeviceConfig] = []
    spi_used: dict[int, set[int]] = {}
    chains, sr_bits = _shift_chains(parts, nl)
    for part in parts:
        if part is pi:
            continue
        twin_type = (getattr(_definition(part), "sim", None) or {}).get("twin")
        if not twin_type:
            continue
        cls = DEVICE_TYPES.get(str(twin_type))
        if cls is None:
            log.warning("part %s: unknown twin device type %r — skipped", getattr(part, "ref", "?"), twin_type)
            continue
        chain = chains.get(id(part))
        if chain is not None and chain[0] is not part:
            continue                                      # downstream chip: part of the head's device
        dev = _device_config(part, cls, nl, spi_used, sr_bits=sr_bits)
        if dev is not None:
            if chain is not None:
                dev.params["length"] = len(chain)
                dev.params["chain"] = [str(getattr(p, "ref", "?")) for p in chain]
                dev.params.pop("shift_register", None)
            devices.append(dev)
    return TwinConfig(board=key if key in BOARDS else "rpi4b", devices=devices, pulls=nl.pulls(parts))


def _shift_chains(parts: list[Any], nl: _Netlist) -> tuple[dict[int, list[Any]], dict[int, tuple[str, int]]]:
    """Shift-register chains: ``{id(part): [head, …]}`` and ``{output net id: (head ref, bit)}``."""
    srs = [p for p in parts if isinstance(_params(p).get("shift_register"), dict)]
    if not srs:
        return {}, {}

    def net_of(part: Any, role: str) -> Any:
        name = _params(part)["shift_register"].get(role)
        pin = _pin_named(part, str(name)) if name else None
        return nl.net(part, pin) if pin is not None else None

    feeds: dict[int, Any] = {}                            # id(data_out net) → part
    for p in srs:
        n = net_of(p, "data_out")
        if n is not None:
            feeds[id(n)] = p
    nxt: dict[int, Any] = {}
    has_prev: set[int] = set()
    for p in srs:
        n = net_of(p, "data_in")
        prev = feeds.get(id(n)) if n is not None else None
        if prev is not None and prev is not p:
            nxt[id(prev)] = p
            has_prev.add(id(p))
    chains: dict[int, list[Any]] = {}
    bits: dict[int, tuple[str, int]] = {}
    for head in srs:
        if id(head) in has_prev:
            continue
        chain = [head]
        while id(chain[-1]) in nxt and nxt[id(chain[-1])] not in chain:
            chain.append(nxt[id(chain[-1])])
        ref = str(getattr(head, "ref", "SR"))
        for i, p in enumerate(chain):
            chains[id(p)] = chain
            for q, name in enumerate(_params(p)["shift_register"].get("outputs", ())):
                pin = _pin_named(p, str(name))
                n = nl.net(p, pin) if pin is not None else None
                if n is not None and id(n) not in nl.rail:
                    bits[id(n)] = (ref, 8 * i + q)
    return chains, bits


def _coil_source(part: Any, cls: Any, nl: _Netlist, sr_bits: dict[int, tuple[str, int]]) -> dict | None:
    """``{"device", "bits"}`` when every coil role of a stepper traces to one shift-register chain."""
    sim_pins = (getattr(_definition(part), "sim", None) or {}).get("pins") or {}
    found: list[tuple[str, int]] = []
    for role in cls.pin_roles:
        spec = sim_pins.get(role)
        hit = None
        for cand in (spec if isinstance(spec, (list, tuple)) else (spec,)) if spec else ():
            name, _, kind = str(cand).partition(":")
            pin = _pin_named(part, name)
            hit = nl.trace_to(nl.net(part, pin) if pin is not None else None, sr_bits, kind or "in")
            if hit is not None:
                break
        if hit is None:
            return None
        found.append(hit)
    if len({dev for dev, _ in found}) != 1:
        return None
    return {"device": found[0][0], "bits": [b for _, b in found]}


def _resolve_roles(part: Any, cls: Any, nl: _Netlist) -> tuple[dict[str, int], dict[str, Any]]:
    """role → BCM and role → the part pin that carried it."""
    roles = set(cls.pin_roles) | set(cls.optional_pins) | {"sda", "scl"}
    found: dict[str, int] = {}
    chosen: dict[str, Any] = {}
    sim_pins = (getattr(_definition(part), "sim", None) or {}).get("pins")
    if isinstance(sim_pins, dict) and sim_pins:
        for role, spec in sim_pins.items():
            role = str(role)
            if role not in roles:
                continue
            for cand in (spec if isinstance(spec, (list, tuple)) else (spec,)):
                name, _, kind = str(cand).partition(":")
                pin = _pin_named(part, name)
                bcm, _ = nl.trace(nl.net(part, pin) if pin is not None else None, kind or "in")
                if bcm is not None:
                    found[role], chosen[role] = bcm, pin
                    break
        return found, chosen
    # heuristic fallback: pin names / aliases, driver channel numbers, single-role devices
    for pin in _pins(part):
        net = nl.net(part, pin)
        if net is None or id(net) in nl.rail:
            continue
        bcm, ch = nl.trace(net)
        if bcm is None:
            continue
        role = cls.normalize_role(pin.name)
        if role not in roles:
            role = next((cls.normalize_role(a) for a in getattr(pin, "aliases", ())
                         if cls.normalize_role(a) in roles), role)
        if role not in roles and ch is not None and f"in{ch}" in roles:
            role = f"in{ch}"
        if role not in roles and _norm(pin.name) in ("sda", "sdi") and bcm in {s for s, _ in _I2C_BY_PAIR}:
            role = "sda"
        if role not in roles and _norm(pin.name) in ("scl", "sck") and bcm in {c for _, c in _I2C_BY_PAIR}:
            role = "scl"
        if role not in roles and len(cls.pin_roles) == 1:
            role = cls.pin_roles[0]
        if role in roles and role not in found:
            found[role], chosen[role] = bcm, pin
    return found, chosen


def _device_config(part: Any, cls: Any, nl: _Netlist, spi_used: dict[int, set[int]], *,
                   sr_bits: dict[int, tuple[str, int]] | None = None) -> DeviceConfig | None:
    d = _definition(part)
    ref = str(getattr(part, "ref", cls.type))
    params = _params(part)
    params.pop("passthrough", None)
    found, chosen = _resolve_roles(part, cls, nl)
    if cls.type == "stepper_28byj48" and sr_bits and any(r not in found for r in cls.pin_roles):
        src = _coil_source(part, cls, nl, sr_bits)
        if src is not None:
            params["coil_source"] = src
            return DeviceConfig(ref, cls.type, params=jsonable(params))
    if cls.type == "shift_register_74hc595":
        found = {("clk" if r == "clock" else "mosi" if r == "data" else "cs" if r == "latch" else r): b
                 for r, b in found.items()}
    bus: dict | None = None
    if "i2c" in cls.bus_kinds and "sda" in found and "scl" in found:
        bus_no = _I2C_BY_PAIR.get((found["sda"], found["scl"]))
        if bus_no is None:
            log.warning("part %s: SDA/SCL on GPIO%d/%d is not a hardware I2C bus — skipped",
                        ref, found["sda"], found["scl"])
            return None
        addr = params.get("i2c_address", params.get("address"))
        if addr is None:
            addrs = tuple(getattr(d, "i2c_addresses", ()) or ())
            addr = addrs[0] if addrs else cls.default_address
        bus = {"kind": "i2c", "bus": bus_no, "address": int(addr, 0) if isinstance(addr, str) else int(addr)}
    elif "spi" in cls.bus_kinds and found.get("clk") in _SCLK:
        bus_no, mosi, miso = _SCLK[found["clk"]]
        if found.get("mosi", mosi) == mosi and found.get("miso", miso) == miso:
            used = spi_used.setdefault(bus_no, set())
            cs_bcm = found.get("cs")
            cs = next((c for (b, c), g in SPI_CS_GPIO.items() if b == bus_no and g == cs_bcm and c not in used), None)
            bus = {"kind": "spi", "bus": bus_no}
            if cs is None:
                cs = next(i for i in range(8) if i not in used)
                if cs_bcm is not None:
                    bus["cs_pin"] = cs_bcm
            used.add(cs)
            bus["cs"] = cs
            bus = {k: bus[k] for k in ("kind", "bus", "cs", "cs_pin") if k in bus}
    sr_latch: dict[str, int] = {}
    if cls.type == "shift_register_74hc595":            # back to the device's own role names
        if bus is not None and "cs_pin" in bus:          # RCLK on a plain GPIO: latch on its rising edge
            sr_latch["latch"] = bus.pop("cs_pin")
        found = {{"clk": "clock", "mosi": "data", "cs": "latch"}.get(r, r): b for r, b in found.items()}
    pins = {r: b for r, b in found.items() if r in set(cls.pin_roles) | set(cls.optional_pins)}
    if bus is not None:
        pins = {r: b for r, b in pins.items() if r in cls.optional_pins}      # e.g. SSD1306 dc/reset
        pins.update(sr_latch)
    else:
        missing = [r for r in cls.pin_roles if r not in pins]
        if missing or (cls.bus_kinds and not cls.pin_roles):
            what = f"pin role(s) {missing}" if missing else "a bus (I2C SDA/SCL or SPI SCLK)"
            log.warning("part %s (%s): could not trace %s to the Pi — skipped", ref, cls.type, what)
            return None
    _wiring_params(part, cls, nl, chosen, params)
    return DeviceConfig(ref, cls.type, pins=pins, bus=bus, params=jsonable(params))


def _wiring_params(part: Any, cls: Any, nl: _Netlist, chosen: dict[str, Any], params: dict) -> None:
    sig = chosen.get("pin")
    if cls.type in _CONTACTS and sig is not None:
        rails = {nl.rail.get(id(nl.net(part, p))) for p in _pins(part) if p is not sig}
        if "gnd" in rails:
            params["active_low"] = True
        elif rails & {"3v3", "5v"}:
            params["active_low"] = False
        else:
            params.setdefault("active_low", True)
    elif cls.type == "led" and sig is not None:
        side = _norm(sig.name)
        if side in _ANODE:
            params["active_high"] = True
        elif side in _CATHODE:
            params["active_high"] = False
    elif cls.type == "rgb_led" and "common_anode" not in params:
        key = str(getattr(_definition(part), "key", "")).lower()
        params["common_anode"] = key.endswith("_ca") or "anode" in key
    elif cls.type == "neopixel" and "count" not in params:
        for alias in ("n", "leds", "pixels", "num_leds", "length"):
            if alias in params:
                params["count"] = params[alias]
                break
