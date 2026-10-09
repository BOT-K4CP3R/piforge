"""Exceptions raised by :mod:`piforge.spice`.

These are usage errors (bad values, missing ngspice, simulator failure). Design problems found by a
bench (LED over-current, missing flyback diode, brown-out …) are reported as
:class:`piforge.core.report.Finding` objects instead.
"""

from __future__ import annotations

from piforge.core.errors import PiForgeError, ToolNotFoundError, ValidationError


class SpiceValueError(ValidationError):
    """An element value, node/element name or analysis argument is invalid.

    Raised by the netlist builder *before* ngspice is started (e.g. R = 0, negative C, NaN,
    a node name with spaces, an unknown model).
    """


class BenchParamError(ValidationError):
    """A bench parameter is unknown, has the wrong type, or is outside its range/choices."""


class SpiceError(PiForgeError):
    """ngspice failed: syntax error, singular matrix, timestep too small, timeout, no output.

    ``log`` holds the complete ngspice stdout+stderr and ``netlist`` the deck that was run, so a
    caller (or the GUI) can show more than the excerpt in the message.
    """

    def __init__(self, message: str, *, log: str = "", netlist: str = "") -> None:
        super().__init__(message)
        self.log = log
        self.netlist = netlist


class NgspiceNotFoundError(ToolNotFoundError):
    """The ngspice executable is not installed (or not on PATH / ``PIFORGE_NGSPICE``)."""
