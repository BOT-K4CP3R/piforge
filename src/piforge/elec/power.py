"""Power budget: current per rail (Pi 5 V header, Pi 3V3, external PSUs, regulator outputs).

The Pi's 5 V header rail gets ``PSU current - Pi board draw (idle/typical/max) - USB reserve``; the
3V3 header rail gets the board's 3V3 budget (its load is reflected onto 5 V through the PMIC buck);
external PSU rails get the PSU rating; regulator rails get the regulator rating and reflect
``P_out / efficiency`` onto their input rail. Loads come from each part's :class:`Supply` (scaled by
``count`` for LED strips); motors behind a driver are charged to the driver's ``load_supply`` rail.

Codes: ``POWER.RAIL_OVERLOAD`` (ERROR, worst case above available), ``POWER.RAIL_MARGIN`` (WARNING,
above 80 %), ``POWER.NO_PSU`` (INFO, assumed the board's recommended PSU), ``POWER.PSU_RATING`` (INFO).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from piforge.core.errors import ValidationError
from piforge.core.report import Report
from piforge.elec.model import Circuit, Net, Part, PartDef, PinRef, PinType
from piforge.elec.signals import analysis

log = logging.getLogger(__name__)

MARGIN = 0.8  # src: task brief (POWER.RAIL_MARGIN above 80 % of the available current)
PI_LOADS = {"idle": "idle_ma", "typical": "typical_ma", "max": "max_ma"}


@dataclass
class RailBudget:
    """One supply rail: ``available_ma`` for user loads, typical/worst-case load and the load list.

    The board's own rails are named by its power pin (``5V``, ``3V3``) whatever the user called the
    net; ``net`` keeps the circuit's net name as an alias (empty when it is the same as ``name``).
    """

    name: str
    voltage: float
    available_ma: float
    typ_ma: float = 0.0
    max_ma: float = 0.0
    loads: list[tuple[str, float, float]] = field(default_factory=list)
    source: str = ""
    net: str = ""

    @property
    def utilization(self) -> float:
        """Worst-case load as a fraction of the available current."""
        return self.max_ma / self.available_ma if self.available_ma > 0 else float("inf")

    def add(self, ref: str, typ: float, mx: float) -> None:
        self.loads.append((ref, typ, mx))
        self.typ_ma += typ
        self.max_ma += mx


@dataclass
class PowerBudget:
    """Result of :func:`power_budget`."""

    rails: dict[str, RailBudget]
    psu: str | None
    report: Report

    def to_markdown(self) -> str:
        lines = ["| Rail | V | Available mA | Typical mA | Worst mA | Use |", "|---|---|---|---|---|---|"]
        for r in self.rails.values():
            use = f"{100 * r.utilization:.0f} %" if r.available_ma > 0 else "n/a"
            lines.append(f"| {r.name} | {r.voltage:g} | {r.available_ma:.0f} | {r.typ_ma:.1f} | {r.max_ma:.1f} | {use} |")
        return "\n".join(lines) + "\n\n" + self.report.to_markdown()


def _psu_def(c: Circuit, psu: str | None, board: Part | None, a) -> tuple[PartDef | None, str | None]:
    if psu is not None:
        from piforge.elec.library import get_def

        d = get_def(psu)
        if "psu" not in d.features:
            raise ValidationError(f"{psu!r} is not a power supply (try psu_usbc_5v3a / psu_usbc_5v5a)")
        return d, d.key
    board_5v = _board_net(c, board, "5V") if board else None
    for part in c.parts:
        if "psu" not in part.features:
            continue
        out = part.pin("V+")
        net = c.net_of(PinRef(part, out))
        if net is None or (board_5v is not None and net is board_5v):
            return part.definition, part.key
    return None, None


def _board_net(c: Circuit, board: Part, name: str) -> Net | None:
    for p in board.pins():
        if p.name == name:
            n = c.net_of(PinRef(board, p))
            if n is not None:
                return n
    return None


def power_budget(circuit: Circuit, *, psu: str | None = None, pi_load: str = "typical",
                 usb_ma: float = 0.0) -> PowerBudget:
    """Current budget of every rail. ``pi_load`` in {"idle", "typical", "max"}; ``usb_ma`` reserves USB current."""
    if pi_load not in PI_LOADS:
        raise ValidationError(f"pi_load={pi_load!r}: use one of {sorted(PI_LOADS)}")
    c = circuit
    a = analysis(c)
    rep = Report("power")
    board = c.board
    psu_def, psu_key = _psu_def(c, psu, board, a)
    rails: dict[str, RailBudget] = {}
    by_net: dict[int, RailBudget] = {}

    def add_rail(net: Net | None, name: str, volts: float, avail: float, source: str, *,
                 by_pin: bool = False) -> RailBudget:
        # board rails keep the pin-type name ("5V"), user net names only label external rails
        key = name if by_pin or net is None else net.name
        if key in rails:
            key = f"{board.ref}.{name}" if board is not None else f"{name}_{len(rails)}"
        alias = net.name if net is not None and net.name != key else ""
        rb = RailBudget(key, volts, avail, source=source, net=alias)
        rails[key] = rb
        if net is not None:
            by_net[id(net)] = rb
        return rb

    # rails driven by PSUs / regulators that are not the Pi's own
    nets_5v = _board_net(c, board, "5V") if board else None
    nets_3v3 = _board_net(c, board, "3V3") if board else None
    for net, sources in a.net.rail_sources.items():
        volts = a.rails.get(net, 0.0)
        if volts <= 0 or net is nets_5v or net is nets_3v3:
            continue
        for ref, v in sources:
            if ref.pin.type != PinType.POWER_OUT or ref.part.category == "board":
                continue
            p = ref.part.params
            avail = float(p.get("i_out_max_ma") or p.get("i_max_ma") or 0.0)
            add_rail(net, net.name, volts, avail, ref.part.ref)
            break

    if board is not None:
        bp = board.params
        if psu_def is None:
            psu_ma = float(bp.get("psu_recommended_ma", 0.0))
            rep.add("POWER.NO_PSU", "info", f"No PSU given: assuming the recommended {psu_ma:.0f} mA supply for "
                    f"{board.name}.", f"part:{board.ref}", "Pass psu='psu_usbc_5v3a' or add a PSU part.",
                    assumed_ma=psu_ma)
        else:
            psu_ma = float(psu_def.params.get("i_max_ma", 0.0))
            rec = float(bp.get("psu_recommended_ma", 0.0))
            if psu_ma < rec:
                note = (f" USB peripherals are limited to {bp['usb_max_ma_3a']:.0f} mA." if "usb_max_ma_3a" in bp
                        else "")
                rep.add("POWER.PSU_RATING", "info", f"{psu_def.name} ({psu_ma:.0f} mA) is below the "
                        f"{rec:.0f} mA recommended for {board.name}.{note}", f"part:{board.ref}",
                        psu_ma=psu_ma, recommended_ma=rec)
        pi_ma = float(bp.get(PI_LOADS[pi_load], 0.0))
        five = add_rail(nets_5v, "5V", 5.0, psu_ma - pi_ma - float(usb_ma), psu_key or "assumed PSU", by_pin=True)
        three = add_rail(nets_3v3, "3V3", 3.3, float(bp.get("rail_3v3_budget_ma", 0.0)), f"{board.ref} 3V3",
                         by_pin=True)
    else:
        five = three = None

    # loads
    driven: dict[int, Part] = {}
    for part in c.parts:
        for name in part.params.get("load_outputs", ()):
            n = c.net_of(PinRef(part, part.pin(name)))
            if n is not None:
                driven[id(n)] = part
    regulators: list[Part] = []
    for part in c.parts:
        if part.category == "board" or "psu" in part.features:
            continue
        if "regulator" in part.features:
            regulators.append(part)
        if "csi" in part.features and five is not None:
            ma = float(part.params.get("current_ma", 250.0))
            five.add(part.ref, ma, ma)
            continue
        s = part.definition.supply
        if s is None:
            continue
        typ, mx = part.supply_ma()
        net = a.pin_net(part, s.pin)
        if net is None:
            continue
        if id(net) in driven:
            drv = driven[id(net)]
            dnet = a.pin_net(drv, drv.params.get("load_supply", ""))
            rb = by_net.get(id(dnet)) if dnet is not None else None
            if rb is not None:
                rb.add(part.ref, typ, mx)
            continue
        rb = by_net.get(id(net))
        if rb is not None:
            rb.add(part.ref, typ, mx)
    # regulators: reflect output power onto the input rail
    for reg in regulators:
        out_net = a.pin_net(reg, reg.params.get("vout_pin", ""))
        in_net = a.pin_net(reg, reg.params.get("requires_input", ""))
        out_rb, in_rb = by_net.get(id(out_net)), by_net.get(id(in_net))
        if out_rb is None or in_rb is None or in_rb.voltage <= 0:
            continue
        # default 0.85: src unverified (typical buck efficiency); LM2596 defines its own (0.8, datasheet)
        k = out_rb.voltage / (float(reg.params.get("efficiency", 0.85)) * in_rb.voltage)
        in_rb.add(reg.ref, out_rb.typ_ma * k, out_rb.max_ma * k)
    if three is not None and five is not None and three.max_ma > 0:
        eff = float(board.params.get("rail_3v3_efficiency", 0.85))
        k = three.voltage / (eff * five.voltage)
        five.add(f"{board.ref} 3V3 rail", three.typ_ma * k, three.max_ma * k)

    for rb in rails.values():
        subj = f"rail:{rb.name}"
        if rb.max_ma > rb.available_ma + 1e-6:
            rep.add("POWER.RAIL_OVERLOAD", "error", f"Rail {rb.name} ({rb.voltage:g} V) worst-case load "
                    f"{rb.max_ma:.0f} mA exceeds the {rb.available_ma:.0f} mA available.", subj,
                    "Use a bigger PSU or a separate supply for motors/servos/LEDs (common GND).",
                    max_ma=rb.max_ma, available_ma=rb.available_ma)
        elif rb.max_ma > MARGIN * rb.available_ma + 1e-6:
            rep.add("POWER.RAIL_MARGIN", "warning", f"Rail {rb.name} ({rb.voltage:g} V) worst-case load "
                    f"{rb.max_ma:.0f} mA is {100 * rb.utilization:.0f} % of the {rb.available_ma:.0f} mA available.",
                    subj, "Keep 20 % margin: brown-outs reset the Pi (below 4.63 V).",
                    max_ma=rb.max_ma, available_ma=rb.available_ma)
    return PowerBudget(rails=rails, psu=psu_key, report=rep)
