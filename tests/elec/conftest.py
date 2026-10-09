"""Fixtures shared by the electronics tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from piforge.elec.model import Circuit


@pytest.fixture
def pi4():
    """A fresh circuit with a Raspberry Pi 4 Model B; returns ``(circuit, pi_part)``."""
    c = Circuit("test")
    pi = c.add("rpi4b")
    return c, pi


def _wire_led(c: Circuit, pi, gpio: str, ohms: float | None, *, color: str = "red",
              supply: str | None = None):
    """``gpio`` (or the rail ``supply``) -> [R] -> LED -> GND. Returns ``(resistor | None, led)``."""
    led = c.add("led", color=color)
    src = pi[supply] if supply else pi[gpio]
    if ohms is None:
        c.connect(src, led["A"])
        r = None
    else:
        r = c.add("resistor", value=ohms)
        c.connect(src, r["1"])
        c.connect(r["2"], led["A"])
    c.connect(led["K"], pi["GND"])
    return r, led


@pytest.fixture
def wire_led() -> Callable:
    """Helper: ``wire_led(c, pi, "GPIO17", 330)`` builds GPIO -> R -> LED -> GND."""
    return _wire_led
