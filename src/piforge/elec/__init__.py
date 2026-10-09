"""Electronics: circuit model, part library, ERC, power budget, pin allocation, boot config, BOM,
wiring table/diagram and KiCad netlist export.

Pure Python (no CAD kernel). matplotlib is imported only inside :func:`wiring.wiring_diagram`.

Typical use::

    from piforge.elec import Circuit, run_erc, power_budget
    c = Circuit("demo")
    pi = c.add("rpi4b")
    r, led = c.add("resistor", value=330), c.add("led")
    c.connect(pi["GPIO17"], r["1"]); c.connect(r["2"], led["A"]); c.connect(led["K"], pi["GND"])
    print(run_erc(c).to_markdown())
"""

from piforge.elec.bom import BomLine, bom, bom_csv, bom_markdown
from piforge.elec.bootconfig import boot_config
from piforge.elec.erc import RULES, gpio_loads, run_erc
from piforge.elec.kicad import kicad_netlist
from piforge.elec.library import get_def, list_defs, register
from piforge.elec.model import (
    REQUIRED,
    Circuit,
    CircuitError,
    Net,
    Part,
    PartDef,
    Pin,
    PinNotFoundError,
    PinRef,
    PinType,
    Supply,
)
from piforge.elec.pinout import pinout_markdown
from piforge.elec.pinmap import AllocationError, PinAllocation, Requirement, allocate_pins
from piforge.elec.power import PowerBudget, RailBudget, power_budget
from piforge.elec.wiring import WireRow, wiring_diagram, wiring_markdown, wiring_table

__all__ = [
    "REQUIRED", "RULES", "AllocationError", "BomLine", "Circuit", "CircuitError", "Net", "Part", "PartDef",
    "Pin", "PinAllocation", "PinNotFoundError", "PinRef", "PinType", "PowerBudget", "RailBudget", "Requirement",
    "Supply", "WireRow", "allocate_pins", "bom", "bom_csv", "bom_markdown", "boot_config", "get_def",
    "gpio_loads", "kicad_netlist", "list_defs", "pinout_markdown", "power_budget", "register", "run_erc", "wiring_diagram",
    "wiring_markdown", "wiring_table",
]
