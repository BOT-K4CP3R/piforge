"""Part library: Raspberry Pi boards, PSUs, passives, semiconductors, modules and actuators.

``get_def("hcsr04")`` returns the immutable :class:`~piforge.elec.model.PartDef`;
``list_defs("sensor")`` lists a category; :func:`register` adds project-specific parts.

Conventions used by the ERC / power / wiring code (all optional ``PartDef.params`` keys):

- ``pullups``: ``{pin: (ohms, target)}`` on-board pull-ups; ``target`` is a pin name of the same part
  (its net voltage) or a fixed voltage. A ``pullup_ohms`` param overrides the value (``None`` = none).
- ``input_loads``: ``{pin: (ohms, vf, gnd_pin)}`` DC load an input presents (e.g. ULN2003 2.7 kΩ + 2 Vbe).
- ``load_ohms`` + ``load_pins`` or ``loads`` ``((pin_a, pin_b, ohms), ...)``: resistive loads (coils, motors).
- ``coils``: pin pairs of inductive windings (``features`` contains ``"inductive"``).
- ``load_outputs`` / ``load_supply``: driver outputs whose load current flows through ``load_supply``.
- ``passthrough``: ``{"in": {out_pin: in_pin}, "pwm": {out_pin: enable_pin}}`` (driver boards).
- ``vout_pin`` / ``vout`` / ``efficiency`` / ``i_out_max_ma`` / ``requires_input``: regulators.
- ``logic_map``: ``{pin: supply_pin}`` per-pin logic domains (level shifters).
- ``isolated_pins``: pins galvanically isolated from the rest of the part (relay contacts); ignored by the
  common-ground check (``ERC.NO_COMMON_GROUND``).
- ``ground_ref``: ``True`` makes the part's GND pins a 0 V reference like a board/PSU/regulator ground.
- ``sim["pins"]``: digital-twin role -> part pin (or tuple of candidates; ``"PIN:kind"`` follows
  ``passthrough[kind]``), e.g. ``{"trigger": "TRIG", "echo": "ECHO"}``.
"""

from __future__ import annotations

import logging

from piforge.core.errors import NotFoundError, ValidationError
from piforge.elec.model import PartDef, PinType

log = logging.getLogger(__name__)

_REGISTRY: dict[str, PartDef] = {}
_ALIASES = {
    "pi5": "rpi5", "raspberrypi5": "rpi5", "rpi4": "rpi4b", "pi4": "rpi4b", "pi4b": "rpi4b",
    "raspberrypi4": "rpi4b", "rpi3b+": "rpi3bp", "rpi3bplus": "rpi3bp", "pi3b+": "rpi3bp", "pi3bp": "rpi3bp",
    "zero2w": "rpizero2w", "pizero2w": "rpizero2w", "rpi_zero_2w": "rpizero2w",
}


def _validate(defn: PartDef) -> None:
    where = f"part definition {defn.key!r}"
    if not defn.key or not defn.name or not defn.category:
        raise ValidationError(f"{where}: key, name and category are required")
    numbers = [p.number for p in defn.pins]
    dup = sorted({n for n in numbers if numbers.count(n) > 1})
    if dup:
        raise ValidationError(f"{where}: duplicate pin numbers {dup}")
    names = [p.name for p in defn.pins]
    power = (PinType.GND, PinType.POWER_OUT, PinType.POWER_IN)
    for n in {n for n in names if names.count(n) > 1}:
        if not all(p.type in power for p in defn.pins if p.name == n):
            raise ValidationError(f"{where}: duplicate pin name {n!r} (only tied power/GND pins may share names)")
    pin_names = set(names)
    if defn.supply is not None and defn.supply.pin not in pin_names:
        raise ValidationError(f"{where}: supply pin {defn.supply.pin!r} is not a pin")
    if defn.logic_from is not None and defn.logic_from not in pin_names:
        raise ValidationError(f"{where}: logic_from {defn.logic_from!r} is not a pin")
    for group in defn.ties:
        missing = [n for n in group if n not in pin_names]
        if missing:
            raise ValidationError(f"{where}: tied pins {missing} do not exist")


def register(defn: PartDef, *, replace: bool = False) -> None:
    """Add a part definition. Re-registering an identical definition is a no-op; a different one with
    the same key raises ``ValidationError`` unless ``replace=True``."""
    if not isinstance(defn, PartDef):
        raise ValidationError(f"register() expects a PartDef, got {type(defn).__name__}")
    _validate(defn)
    old = _REGISTRY.get(defn.key)
    if old is not None and old is not defn and not replace:
        if old == defn:
            return
        raise ValidationError(f"Part key {defn.key!r} is already registered; pass replace=True to override")
    _REGISTRY[defn.key] = defn


def get_def(key: str) -> PartDef:
    """Library definition by key (case-insensitive, a few aliases such as ``"pi4"``)."""
    if isinstance(key, PartDef):
        return key
    k = str(key).strip()
    if k in _REGISTRY:
        return _REGISTRY[k]
    low = k.lower().replace(" ", "").replace("-", "")
    if low in _REGISTRY:
        return _REGISTRY[low]
    if low in _ALIASES:
        return _REGISTRY[_ALIASES[low]]
    raise NotFoundError("part", key, list(_REGISTRY) + list(_ALIASES))


def list_defs(category: str | None = None) -> list[PartDef]:
    """All definitions (sorted by key), optionally only one category."""
    return [d for k, d in sorted(_REGISTRY.items()) if category is None or d.category == category]


def _load() -> None:
    from piforge.elec.library import actuators, modules, passives, rpi, semis

    for mod in (rpi, passives, semis, modules, actuators):
        for d in mod.DEFS:
            register(d)


_load()

__all__ = ["get_def", "list_defs", "register"]
