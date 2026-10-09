"""Shared helpers for the ERC rule modules (:mod:`piforge.elec.erc`, :mod:`piforge.elec.erc_pins`)."""

from __future__ import annotations

from piforge.core.report import Finding, Severity
from piforge.elec.model import PinRef

E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO


def _f(code: str, sev: Severity, msg: str, subject: str = "", hint: str = "", **data) -> Finding:
    return Finding(code=code, severity=sev, message=msg, subject=subject, data=data, hint=hint, source="ERC")


def _pin(ref: PinRef) -> str:
    return f"pin:{ref.part.ref}.{ref.pin.name}"


def _phys(ref: PinRef) -> str:
    return f"{ref.label} (pin {ref.pin.number})" if ref.part.category == "board" else ref.label


def _ma(ma: float) -> str:
    return "over 1 A (nothing limits the current)" if ma > 1000 else f"{ma:.1f} mA"
