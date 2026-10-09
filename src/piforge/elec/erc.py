"""Electrical rule check: catches wiring mistakes before anything is soldered or powered.

``run_erc(circuit)`` returns a :class:`~piforge.core.report.Report` titled ``"ERC"``. Each rule in
:data:`RULES` (key = finding code) is a function ``circuit -> list[Finding]``; voltages and currents
come from the DC network solver (:mod:`piforge.elec.dcsolve`) through :mod:`piforge.elec.signals`, so
dividers, pull-ups, series resistors and LED drops are taken into account.

Codes added in fix round 1 (beyond the original brief):

- ``ERC.NO_COMMON_GROUND`` (ERROR): a signal or supply connects parts whose grounds are separate islands
  (e.g. a servo on an external 12 V -> LM2596 supply driven from a GPIO without tying the supply GND to
  the Pi GND). See :mod:`piforge.elec.grounds`.
- ``ERC.PULL_DIRECTION`` (WARNING): a switch connects an input to the level its pull already holds
  (button to GND with a pull-down, button to 3V3 with a pull-up), so pressing it changes nothing.
- ``ERC.UNCONNECTED`` also reports (ERROR) ground nets that join module GND pins only to each other.
- ``ERC.LEVEL_MISMATCH`` has two tiers: ERROR at/above the absolute maximum, WARNING between the
  receiver's VDD + 0.3 V and the absolute maximum (Pi 5: 3.6-3.8 V). Potentiometers are evaluated at
  both ends of their travel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from piforge.core.errors import NotFoundError
from piforge.core.report import Finding, Report, Severity
from piforge.elec.erc_pins import floating_input, interface_pin_conflict, pull_direction, reserved_pin
from piforge.elec.ercbase import E, I, W, _f, _ma, _phys, _pin
from piforge.elec.grounds import floating_grounds, ground_crossings
from piforge.elec.model import Circuit, Net, Part, PinRef, PinType
from piforge.elec.signals import GENERIC_VMAX_MARGIN, TOL_V, SignalAnalysis, analysis, is_board_gpio

log = logging.getLogger(__name__)

I2C_SINK_MA = 3.0  # src: NXP UM10204 I2C spec: 3 mA max sink (Standard/Fast mode)
_V_EPS = 0.005     # V, rounding guard for the over-voltage comparisons (modelling constant)


# ---------------------------------------------------------------------------------- wiring rules
def unconnected(c: Circuit) -> list[Finding]:
    """Required pins left open; parts with no connection at all."""
    out = []
    for part in c.parts:
        if part.category == "board":
            continue
        connected = [p for p in part.pins() if c.is_connected(PinRef(part, p))]
        if part.pins() and not connected and not {"psu", "csi"} & set(part.features):
            out.append(_f("ERC.UNCONNECTED", W, f"{part.ref} ({part.name}) is not connected to anything.",
                          f"part:{part.ref}", "Wire it up or remove it from the circuit."))
            continue
        for p in part.pins():
            if not p.required or c.is_connected(PinRef(part, p)):
                continue
            if any(c.is_connected(PinRef(part, t)) for t in part.tied(p)):
                continue
            power = p.type in (PinType.POWER_IN, PinType.GND)
            out.append(_f("ERC.UNCONNECTED", E if power else W,
                          f"{part.ref}.{p.name} ({part.name}, pin {p.number}) must be connected.",
                          f"pin:{part.ref}.{p.name}",
                          "Connect the supply/ground pin." if power else "Connect this signal."))
    for net in floating_grounds(c):
        refs = ", ".join(r.label for r in net.refs if r.pin.type == PinType.GND)
        out.append(_f("ERC.UNCONNECTED", E, f"Ground net {net.name} ({refs}) is not connected to the ground of the "
                      "Pi, a power supply or a regulator: these modules have no 0 V reference or return path.",
                      f"net:{net.name}", "Connect the net to a Pi GND pin (or the supply's GND)."))
    return out


def no_common_ground(c: Circuit) -> list[Finding]:
    """Signals/supplies crossing between ground islands that are not tied together."""
    out = []
    for nets, by_island, names in ground_crossings(c):
        sides = []
        for key, refs in by_island.items():
            labels = ", ".join(dict.fromkeys(_phys(r) for r in refs))
            sides.append(f"{labels} on {names[key]}")
        subj = next((n for n in nets if sum(any(r in n.refs for r in refs) for refs in by_island.values()) > 1),
                    next(n for n in nets if any(r in n.refs for refs in by_island.values() for r in refs)))
        out.append(_f("ERC.NO_COMMON_GROUND", E, f"Net(s) {', '.join(n.name for n in nets)} connect parts with "
                      f"separate grounds: {'; '.join(sides)}. Without a common ground the signal level is "
                      "undefined and the current has no return path.", f"net:{subj.name}",
                      "Tie the grounds together: connect the external supply's GND to a Pi GND pin.",
                      islands=list(names[k] for k in by_island)))
    return out


def supply_voltage(c: Circuit) -> list[Finding]:
    """Each part's supply pin sits on a rail inside its operating range."""
    a = analysis(c)
    out = []
    for part in c.parts:
        s = part.definition.supply
        if s is None or part.category == "board":
            continue
        checks = [(s.pin, s.v_min, s.v_max)] + [(k, lo, hi) for k, (lo, hi) in part.params.get("extra_supplies", {}).items()]
        for pin_name, vmin, vmax in checks:
            net = a.pin_net(part, pin_name)
            if net is None:
                continue
            if any(r in a.load_outputs for r in net.refs):
                continue  # motor/coil fed by a driver output: the driver owns the supply
            subj = f"pin:{part.ref}.{pin_name}"
            gpio = [r for r in net.refs if is_board_gpio(r)]
            v = a.node_v(net)
            if net not in a.rails and gpio:
                v = gpio[0].pin.voltage or 3.3
                out.append(_f("ERC.SUPPLY_VOLTAGE", W, f"{part.ref} ({part.name}) is powered from {_phys(gpio[0])}; "
                              f"a GPIO is not a supply (16 mA max).", subj, "Power it from 3V3/5V or a regulator."))
            if v is None:
                out.append(_f("ERC.SUPPLY_VOLTAGE", W, f"{part.ref}.{pin_name} ({part.name}) is not connected to a "
                              f"power rail (net {net.name}).", subj, "Connect it to 3V3, 5V or a regulator output."))
                continue
            if v > vmax + TOL_V:
                out.append(_f("ERC.SUPPLY_VOLTAGE", E, f"{part.ref} ({part.name}) gets {v:.2f} V on {pin_name} but its "
                              f"supply range is {vmin:g}-{vmax:g} V.", subj,
                              "Use the 3V3 rail or a regulator matching the part.", volts=v, v_min=vmin, v_max=vmax))
            elif v < vmin - TOL_V:
                out.append(_f("ERC.SUPPLY_VOLTAGE", W, f"{part.ref} ({part.name}) gets only {v:.2f} V on {pin_name}; it "
                              f"needs {vmin:g}-{vmax:g} V and may not work reliably.", subj,
                              "Power it from a rail inside its range (e.g. 5V).", volts=v, v_min=vmin, v_max=vmax))
    return out


def rail_short(c: Circuit) -> list[Finding]:
    """Different rails (or a rail and GND) joined in one net."""
    a = analysis(c)
    out = []
    for net, sources in a.net.rail_sources.items():
        gnd = [r for r, v in sources if r.pin.type == PinType.GND]
        pwr = [(r, v) for r, v in sources if r.pin.type == PinType.POWER_OUT]
        if gnd and pwr:
            out.append(_f("ERC.RAIL_SHORT", E, f"Net {net.name} joins ground ({gnd[0].label}) with the "
                          f"{pwr[0][1]:g} V supply {pwr[0][0].label}: a dead short.", f"net:{net.name}",
                          "Separate the supply from GND.", volts=pwr[0][1]))
            continue
        volts = sorted({round(v, 2) for _, v in pwr if v is not None})
        if len(volts) > 1 and volts[-1] - volts[0] > 0.3:
            out.append(_f("ERC.RAIL_SHORT", E, f"Net {net.name} connects supplies of different voltages "
                          f"({', '.join(f'{v:g} V' for v in volts)}).", f"net:{net.name}",
                          "Each rail needs its own net.", volts=volts))
        elif len({r.part for r, _ in pwr}) > 1:
            out.append(_f("ERC.RAIL_SHORT", I, f"Net {net.name} has two supplies in parallel "
                          f"({', '.join(r.label for r, _ in pwr)}).", f"net:{net.name}",
                          "Back-powering bypasses the Pi's input protection; use one supply per rail."))
    return out


def output_conflict(c: Circuit) -> list[Finding]:
    """Push-pull outputs fighting each other or a rail; GPIOs hard-wired to rails or to each other."""
    a = analysis(c)
    out = []
    for net in c.nets:
        outs = [r for r in net.refs if r.pin.type == PinType.OUTPUT]
        parts = {r.part for r in outs}
        if net in a.rails and outs:
            out.append(_f("ERC.OUTPUT_CONFLICT", E, f"Output {outs[0].label} is wired straight to the "
                          f"{a.rails[net]:g} V net {net.name}: it will be destroyed when it drives the other level.",
                          _pin(outs[0]), "Remove the connection to the rail."))
        elif len(parts) > 1:
            out.append(_f("ERC.OUTPUT_CONFLICT", E, f"Net {net.name} is driven by several outputs "
                          f"({', '.join(r.label for r in outs)}).", f"net:{net.name}",
                          "Give each output its own GPIO, or combine them with diodes/open-drain logic."))
        gpios = [r for r in net.refs if is_board_gpio(r)]
        if net in a.rails and gpios:
            out.append(_f("ERC.OUTPUT_CONFLICT", W, f"{_phys(gpios[0])} is wired directly to {net.name}; setting it "
                          "as an output at the other level shorts the pin.", _pin(gpios[0]),
                          "Use a series resistor (1 kΩ) or remove the connection."))
        elif len({r.pin.name for r in gpios}) > 1:
            out.append(_f("ERC.OUTPUT_CONFLICT", W, f"GPIOs {', '.join(r.pin.name for r in gpios)} are tied together; "
                          "two outputs at different levels would short.", f"net:{net.name}",
                          "Use one GPIO per signal."))
    return out


# ---------------------------------------------------------------------------------- levels
def over_limits(a: SignalAnalysis, rx: PinRef) -> tuple[float, float] | None:
    """(recommended max, absolute max) input voltage of a receiver: VDD + 0.3 V and the abs max rating."""
    vmax = a.v_max(rx)
    if vmax is None:
        return None
    vdd = a.logic_vdd(rx)
    warn = vdd + GENERIC_VMAX_MARGIN if vdd is not None else vmax
    return min(warn, vmax), vmax


def _level_scan(a: SignalAnalysis, *, with_low: bool = True):
    """Worst over-voltage and worst under-drive per receiver across idle + every driver scenario."""
    high: dict[PinRef, tuple[float, str]] = {}
    low: dict[PinRef, tuple[float, float, str]] = {}
    tag = a.variant_desc

    def check(net: Net, v: float, src: str, *, low_ok: bool, exclude: PinRef | None = None) -> None:
        for rx in a.receivers(net, exclude=exclude):
            lim = over_limits(a, rx)
            if lim is not None and v > lim[0] + _V_EPS and v > high.get(rx, (-1e9, ""))[0]:
                high[rx] = (v, src + tag)
            vih = a.vih(rx)
            if low_ok and vih is not None and v > 0.5 and v < vih - 0.01 and v < low.get(rx, (1e9, 0, ""))[0]:
                low[rx] = (v, vih, src)

    for net, v in a.rails.items():
        check(net, v, f"the {net.name} rail", low_ok=False)
    for net in c_nets(a):
        v = a.base.volts(net)
        if v is not None:
            check(net, v, "the pull-up/bias network", low_ok=with_low and a.is_open_drain_bus(net))
    for d in a.drivers():
        sol = a.net.solve({d.net: d.v_max})
        for net in a.affected(sol):
            src = f"{d.ref.label} driving {d.v_max:g} V" + (" through resistors" if net is not d.net else "")
            check(net, sol.volts(net), src, low_ok=False, exclude=d.ref)
        if not with_low:
            continue
        sol_lo = a.net.solve({d.net: d.v_min})
        for net in a.affected(sol_lo):
            check_low_only(a, net, sol_lo.volts(net), f"{d.ref.label} (VOH {d.v_min:g} V)", d.ref, low)
    return high, low


def c_nets(a: SignalAnalysis) -> list[Net]:
    return [n for n in a.c.nets if n not in a.rails]


def check_low_only(a: SignalAnalysis, net: Net, v: float, src: str, exclude: PinRef, low: dict) -> None:
    for rx in a.receivers(net, exclude=exclude):
        if rx.part is exclude.part:
            continue
        vih = a.vih(rx)
        if vih is not None and v > 0.5 and v < vih - 0.01 and v < low.get(rx, (1e9, 0, ""))[0]:
            low[rx] = (v, vih, src)


def level_mismatch(c: Circuit) -> list[Finding]:
    """A pin sees more than its absolute maximum (ERROR, e.g. a 5 V output into a 3.3 V GPIO) or more than
    VDD + 0.3 V but less than the absolute maximum (WARNING); potentiometers are checked at both ends."""
    a = analysis(c)
    high: dict[PinRef, tuple[float, str]] = {}
    for va in a.variants():
        for rx, (v, src) in _level_scan(va, with_low=False)[0].items():
            if v > high.get(rx, (-1e9, ""))[0]:
                high[rx] = (v, src)
    out = []
    for rx, (v, src) in high.items():
        warn, vmax = over_limits(a, rx)
        hint = ("Add a level shifter (bss138_level_shifter), a divider (1 kΩ/2 kΩ from 5 V) or power the "
                "module from 3V3.")
        if v >= vmax - _V_EPS:
            out.append(_f("ERC.LEVEL_MISMATCH", E, f"{_phys(rx)} sees {v:.2f} V from {src}, at or above its "
                          f"{vmax:.2f} V absolute maximum.", _pin(rx), hint,
                          volts=round(v, 4), v_max=vmax, v_warn=warn, source=src))
        else:
            out.append(_f("ERC.LEVEL_MISMATCH", W, f"{_phys(rx)} sees {v:.2f} V from {src}: above VDD + 0.3 V "
                          f"({warn:.2f} V) and only {vmax - v:.2f} V below its {vmax:.2f} V absolute maximum.",
                          _pin(rx), hint, volts=round(v, 4), v_max=vmax, v_warn=warn, source=src))
    return out


def level_low_drive(c: Circuit) -> list[Finding]:
    """A driver's guaranteed high level is below the receiver's VIH (e.g. 3.3 V into a 5 V CMOS input)."""
    a = analysis(c)
    _, low = _level_scan(a)
    out = []
    for rx, (v, vih, src) in low.items():
        i2c = {"I2C_SDA", "I2C_SCL"} & set(rx.pin.functions)
        hint = ("Use an I2C-safe bidirectional level shifter (bss138_level_shifter) or power the device from 3V3."
                if i2c else "Use a level shifter (e.g. 74AHCT125 for LED strips) or power the receiver from 3V3.")
        out.append(_f("ERC.LEVEL_LOW_DRIVE", W, f"{_phys(rx)} needs VIH >= {vih:.2f} V but {src} only reaches "
                      f"{v:.2f} V: the high level is not guaranteed.", _pin(rx), hint,
                      volts=round(v, 4), vih=round(vih, 4), source=src))
    return out


# ---------------------------------------------------------------------------------- I2C
def _i2c_buses(c: Circuit) -> dict[Net, list[Part]]:
    buses: dict[Net, list[Part]] = {}
    for part in c.parts:
        for p in part.pins():
            if "I2C_SDA" in p.functions:
                net = c.net_of(PinRef(part, p))
                if net is not None:
                    buses.setdefault(net, []).append(part)
    return buses


def i2c_address_conflict(c: Circuit) -> list[Finding]:
    """Two devices answering on the same address on one bus."""
    out = []
    for net, parts in _i2c_buses(c).items():
        by_addr: dict[int, list[Part]] = {}
        for p in parts:
            if p.definition.i2c_addresses:
                by_addr.setdefault(int(p.params["i2c_address"]), []).append(p)
        for addr, ps in by_addr.items():
            if len(ps) > 1:
                alt = [f"0x{x:02X}" for x in ps[0].definition.i2c_addresses if x != addr]
                out.append(_f("ERC.I2C_ADDRESS_CONFLICT", E, f"{', '.join(p.ref for p in ps)} all use I2C address "
                              f"0x{addr:02X} on bus {net.name}.", f"net:{net.name}",
                              f"Strap one to another address ({', '.join(alt) or 'none available'}: "
                              f"c.add(..., i2c_address=...)) or use a second bus / TCA9548A mux.",
                              address=addr, parts=[p.ref for p in ps]))
    return out


def i2c_pullups(c: Circuit) -> list[Finding]:
    """Missing, too strong, or only-on-board pull-ups on each I2C bus (SDA line checked)."""
    a = analysis(c)
    out = []
    for net, parts in _i2c_buses(c).items():
        ups = a.net.pullups_on(net)
        refs = ", ".join(p.ref for p in parts)
        if not ups:
            out.append(_f("ERC.I2C_PULLUPS", W, f"I2C bus {net.name} ({refs}) has no pull-up resistors; the bus will "
                          "not work.", f"net:{net.name}",
                          "Add 4.7 kΩ from SDA and SCL to 3V3 (or use GPIO2/GPIO3 which have 1.8 kΩ on the Pi)."))
            continue
        sink = sum(v / e.r for e, v in ups) * 1000.0
        req = 1.0 / sum(1.0 / e.r for e, _ in ups)
        if sink > I2C_SINK_MA + 1e-6:
            out.append(_f("ERC.I2C_PULLUPS", W, f"I2C bus {net.name} pull-ups total {req:.0f} Ω: a device must sink "
                          f"{sink:.1f} mA to pull it low (I2C limit {I2C_SINK_MA:g} mA).", f"net:{net.name}",
                          "Remove some module pull-ups (pullup_ohms=None).", ohms=req, sink_ma=sink))
        elif all(e.part is not None and e.part.category == "board" for e, _ in ups):
            out.append(_f("ERC.I2C_PULLUPS", I, f"I2C bus {net.name} ({refs}) relies on the Pi's 1.8 kΩ on-board "
                          "pull-ups only; fine for short wires.", f"net:{net.name}",
                          "Fine as is; for long cables (> ~30 cm) lower the clock (dtparam=i2c_arm_baudrate=50000).",
                          ohms=req))
    return out


# ---------------------------------------------------------------------------------- currents
def gpio_loads(circuit: Circuit) -> dict[str, float]:
    """Worst-case current (mA, source or sink) of every connected board GPIO, keyed by pin name."""
    a = analysis(circuit)
    return {r.pin.name: max(hi, lo) for r, (hi, lo) in a.gpio_currents().items()}


def _bare_led_nets(a: SignalAnalysis) -> set[int]:
    nets = set()
    for i, (_, bare, _) in a.led_currents().items():
        if bare:
            e = a.net.elements[i]
            nets.update(id(x) for x in (e.a, e.b))
    return nets


def gpio_overcurrent(c: Circuit) -> list[Finding]:
    """A GPIO sourcing/sinking more than its 16 mA safe limit."""
    a = analysis(c)
    bare = _bare_led_nets(a)
    out = []
    for r, (hi, lo) in a.gpio_currents().items():
        net = c.net_of(r)
        if id(net) in bare:
            continue  # reported as ERC.LED_NO_RESISTOR
        lim = r.pin.i_max_ma or float(r.part.params.get("gpio_pin_max_ma", 16.0))
        worst = max(hi, lo)
        if worst > lim:
            how = "sources" if hi >= lo else "sinks"
            out.append(_f("ERC.GPIO_OVERCURRENT", E, f"{_phys(r)} {how} {_ma(worst)} when driven, above the "
                          f"{lim:g} mA per-pin limit.", _pin(r),
                          "Increase the series resistor or switch the load with a transistor/MOSFET.",
                          ma=round(worst, 3), limit_ma=lim))
    return out


def gpio_total_current(c: Circuit) -> list[Finding]:
    """All GPIOs together above the ~50 mA the 3V3 GPIO supply was designed for."""
    a = analysis(c)
    bare = _bare_led_nets(a)
    total = sum(max(hi, lo) for r, (hi, lo) in a.gpio_currents().items() if id(c.net_of(r)) not in bare)
    board = c.board
    lim = float(board.params.get("gpio_total_ma", 50.0)) if board else 50.0
    if total > lim:
        return [_f("ERC.GPIO_TOTAL_CURRENT", W, f"GPIO loads total {total:.1f} mA, above the {lim:g} mA the "
                   "Raspberry Pi GPIOs can supply together.", f"part:{board.ref if board else ''}",
                   "Drive the loads through transistors or a driver IC (e.g. ULN2003).", ma=round(total, 2), limit_ma=lim)]
    return []


def _led_items(a: SignalAnalysis):
    """(element, worst mA, no-resistor flag, scenario) per LED, worst case over the pot-extreme variants."""
    merged: dict[int, tuple[float, bool, str]] = {}
    for va in a.variants():
        for i, (ma, bare, where) in va.led_currents().items():
            best, was_bare, w = merged.get(i, (0.0, False, ""))
            if ma > best:
                best, w = ma, where + va.variant_desc
            merged[i] = (best, was_bare or bare, w)
    for i, (ma, bare, where) in merged.items():
        yield a.net.elements[i], ma, bare, where


def led_no_resistor(c: Circuit) -> list[Finding]:
    """An LED connected between two hard voltages with nothing limiting its current."""
    a = analysis(c)
    out = []
    for e, ma, bare, where in _led_items(a):
        if bare:
            what = e.part.ref + (f" ({e.pin})" if e.part.key == "rgb_led_cc" else "")
            out.append(_f("ERC.LED_NO_RESISTOR", E, f"LED {what} has no series resistor (conducts when {where}); "
                          "the current is limited only by the source.", f"part:{e.part.ref}",
                          "Add a series resistor: R = (V - Vf) / I, e.g. 330 Ω from 3.3 V for ~4 mA."))
    return out


def led_overcurrent(c: Circuit) -> list[Finding]:
    """LED forward current above its rating."""
    a = analysis(c)
    out = []
    for e, ma, bare, where in _led_items(a):
        lim = float(e.part.params.get("if_max_ma", 20.0))
        if not bare and ma > lim + 0.05:
            out.append(_f("ERC.LED_OVERCURRENT", E, f"LED {e.part.ref} carries {_ma(ma)} when {where}, above its "
                          f"{lim:g} mA rating.", f"part:{e.part.ref}", "Increase the series resistor.",
                          ma=round(ma, 3), limit_ma=lim))
    return out


# ---------------------------------------------------------------------------------- loads & drive
def _switch_pin(r: PinRef) -> bool:
    if r.part.category == "transistor":
        return r.pin.name in ("C", "D")
    return is_board_gpio(r) or (r.pin.type in (PinType.OUTPUT, PinType.OPEN_DRAIN)
                                and "clamped_outputs" not in r.part.features)


def inductive_no_flyback(c: Circuit) -> list[Finding]:
    """Switched coils (relay, motor, solenoid, magnetic buzzer) need a flyback diode."""
    a = analysis(c)
    out = []
    for part in c.parts:
        if "inductive" not in part.features or "flyback" in part.features:
            continue
        coils = part.params.get("coils") or ((tuple(part.params["load_pins"]),) if part.params.get("load_pins") else ())
        missing, reversed_, unclamped = [], [], []
        for pa, pb in coils:
            na, nb = a.pin_net(part, pa), a.pin_net(part, pb)
            if na is None or nb is None:
                continue
            others = [r for r in na.refs + nb.refs if r.part is not part]
            clampers = [r for r in others if "clamped_outputs" in r.part.features]
            if clampers:
                # Drivers with internal clamp diodes; a bare chip (ULN2003A) only clamps when its
                # ``clamp_pin`` (COM) is tied to the coil's supply side.
                bad = []
                for r in clampers:
                    cp = r.part.params.get("clamp_pin")
                    supply_side = nb if r in na.refs else na
                    if not cp or a.pin_net(r.part, cp) is supply_side:
                        break                                   # clamped
                    bad.append((r, supply_side, cp))
                else:
                    r, supply_side, cp = bad[0]
                    external = any(a.pin_net(d, "K") is supply_side and a.pin_net(d, "A") is not supply_side
                                   and {id(a.pin_net(d, "K")), id(a.pin_net(d, "A"))} == {id(na), id(nb)}
                                   for d in c.parts if d.category == "diode")
                    if not external:
                        unclamped.append((f"{pa}-{pb}", r.part.ref, cp))
                continue
            low = next((n for n in (na, nb) if any(_switch_pin(r) for r in n.refs if r.part is not part)), None)
            if low is None:
                continue  # not switched (always on, or switched by a mechanical contact elsewhere)
            high = nb if low is na else na
            diodes = []
            for d in c.parts:
                if d.category != "diode":
                    continue
                nk, nan = a.pin_net(d, "K"), a.pin_net(d, "A")
                if {id(nk), id(nan)} == {id(na), id(nb)}:
                    diodes.append((d, nk is high))
            if not diodes:
                missing.append(f"{pa}-{pb}")
            elif not any(ok for _, ok in diodes):
                reversed_.append((f"{pa}-{pb}", diodes[0][0].ref))
        for coil, dref in reversed_:
            out.append(_f("ERC.INDUCTIVE_NO_FLYBACK", E, f"Diode {dref} across {part.ref} ({coil}) is reversed: it "
                          "conducts the supply straight into the switch when it turns on.", f"part:{part.ref}",
                          "Turn the diode around: cathode (band) to the supply side, anode to the switched side."))
        by_drv: dict[tuple[str, str], list[str]] = {}
        for coil, dref, cp in unclamped:
            by_drv.setdefault((dref, cp), []).append(coil)
        for (dref, cp), coil_list in by_drv.items():
            coil = ", ".join(coil_list)
            out.append(_f("ERC.INDUCTIVE_NO_FLYBACK", W, f"{part.ref} ({part.name}) coil {coil} is switched by "
                          f"{dref}, but {dref}.{cp} is not tied to the coil supply: its internal clamp diodes "
                          "are out of circuit.", f"part:{part.ref}",
                          f"Connect {dref}.{cp} to the motor/coil supply (the net of the coil's other end)."))
        if missing:
            out.append(_f("ERC.INDUCTIVE_NO_FLYBACK", W, f"{part.ref} ({part.name}) coil {', '.join(missing)} is switched "
                          "without a flyback diode; the turn-off spike can destroy the transistor/GPIO.",
                          f"part:{part.ref}", "Add a 1N4007 (or 1N4148/1N5819) across the coil, cathode to +."))
    return out


def mosfet_gate_drive(c: Circuit) -> list[Finding]:
    """N-MOSFET gates driven below the VGS at which RDS(on) is specified."""
    a = analysis(c)
    out = []
    for part in c.parts:
        if part.params.get("kind") != "nmos":
            continue
        g, s = a.pin_net(part, "G"), a.pin_net(part, "S")
        if g is None:
            continue
        best = None
        for d in a.drivers():
            sol = a.net.solve({d.net: d.v_min})
            vg = sol.volts(g)
            base = a.base.volts(g)
            if vg is None or (base is not None and abs(vg - base) < 1e-6):
                continue
            vs = a.rails.get(s) if s is not None and s in a.rails else (sol.volts(s) if s is not None else 0.0)
            vgs = vg - (vs or 0.0)
            if best is None or vgs > best[0]:
                best = (vgs, d)
        rated, vth = float(part.params.get("vgs_rated", 10.0)), float(part.params.get("vgs_th_max", 4.0))
        if best is not None and best[0] < rated - 0.01:
            vgs, d = best
            extra = " and may not turn on at all" if vgs < vth else ""
            out.append(_f("ERC.MOSFET_GATE_DRIVE", W, f"{part.ref} ({part.name}) gate gets {vgs:.2f} V from "
                          f"{d.ref.label}, but RDS(on) is only specified from VGS = {rated:g} V (VGS(th) up to "
                          f"{vth:g} V){extra}.", f"part:{part.ref}",
                          "Use a logic-level MOSFET specified at 2.5 V (e.g. AO3400) or a gate driver/NPN level shifter.",
                          vgs=round(vgs, 3), vgs_rated=rated, vgs_th_max=vth))
    return out


def motor_on_gpio(c: Circuit) -> list[Finding]:
    """Motors, coils and servo power pins wired straight to a GPIO."""
    out = []
    for part in c.parts:
        feats = set(part.features)
        names = part.params.get("motor_pins")
        if names is None:
            if not {"motor", "inductive"} & feats:
                continue
            names = [p.name for p in part.pins()]
        for name in names:
            net = c.net_of(PinRef(part, part.pin(name)))
            gpio = [r for r in net.refs if is_board_gpio(r)] if net else []
            if gpio:
                out.append(_f("ERC.MOTOR_ON_GPIO", E, f"{part.ref}.{name} ({part.name}) is connected straight to "
                              f"{_phys(gpio[0])}; motors and coils need far more current than a GPIO (16 mA).",
                              f"part:{part.ref}", "Drive it with an H-bridge (TB6612/L298N), ULN2003 or a MOSFET + "
                              "flyback diode; power it from 5V or a separate supply."))
                break
    return out


RULES: dict[str, Callable[[Circuit], list[Finding]]] = {
    "ERC.UNCONNECTED": unconnected,
    "ERC.SUPPLY_VOLTAGE": supply_voltage,
    "ERC.LEVEL_MISMATCH": level_mismatch,
    "ERC.LEVEL_LOW_DRIVE": level_low_drive,
    "ERC.OUTPUT_CONFLICT": output_conflict,
    "ERC.RAIL_SHORT": rail_short,
    "ERC.I2C_ADDRESS_CONFLICT": i2c_address_conflict,
    "ERC.I2C_PULLUPS": i2c_pullups,
    "ERC.GPIO_OVERCURRENT": gpio_overcurrent,
    "ERC.GPIO_TOTAL_CURRENT": gpio_total_current,
    "ERC.LED_NO_RESISTOR": led_no_resistor,
    "ERC.LED_OVERCURRENT": led_overcurrent,
    "ERC.INDUCTIVE_NO_FLYBACK": inductive_no_flyback,
    "ERC.MOSFET_GATE_DRIVE": mosfet_gate_drive,
    "ERC.FLOATING_INPUT": floating_input,
    "ERC.RESERVED_PIN": reserved_pin,
    "ERC.INTERFACE_PIN_CONFLICT": interface_pin_conflict,
    "ERC.MOTOR_ON_GPIO": motor_on_gpio,
    "ERC.NO_COMMON_GROUND": no_common_ground,
    "ERC.PULL_DIRECTION": pull_direction,
}


def _rule_key(name: str) -> str:
    k = name.strip().upper()
    return k if k.startswith("ERC.") else f"ERC.{k}"


def run_erc(circuit: Circuit, *, rules: list[str] | None = None) -> Report:
    """Run all (or the named) rules; names accept ``"ERC.LEVEL_MISMATCH"`` or ``"level_mismatch"``."""
    keys = list(RULES) if rules is None else [_rule_key(r) for r in rules]
    for k in keys:
        if k not in RULES:
            raise NotFoundError("ERC rule", k, list(RULES))
    rep = Report("ERC")
    for k in keys:
        rep.extend(RULES[k](circuit))
    log.debug("ERC %s: %s", circuit.name, rep)
    return rep
