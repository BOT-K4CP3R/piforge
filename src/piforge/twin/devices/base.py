"""Device model framework: property specs, the :class:`Device` base class and the type registry.

A device model is wired to the :class:`~piforge.twin.gpio.VirtualPi` through *pin roles* (e.g. an
HC-SR04 has ``trigger`` and ``echo``) and/or a bus attachment. It exposes

* ``inputs``  — what the user / a scenario can set (button pressed, distance, temperature…),
* ``outputs`` — what the firmware caused (LED brightness, servo angle, display text…),

each described by a :class:`PropSpec` so the GUI can build widgets automatically.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from piforge.core.errors import NotFoundError, ValidationError
from piforge.twin.config import DeviceConfig

if TYPE_CHECKING:  # pragma: no cover
    from piforge.twin.gpio import VirtualPi

PROP_TYPES = ("bool", "float", "int", "enum", "image", "text")
_TRUE = {"1", "true", "on", "yes", "y", "high", "pressed", "closed"}
_FALSE = {"0", "false", "off", "no", "n", "low", "released", "open", ""}


@dataclass(frozen=True)
class PropSpec:
    """Type and range of one device property.

    ``type`` ∈ bool|float|int|enum|image|text. ``widget`` is an optional GUI hint:
    ``"momentary"`` (press-and-hold button), ``"toggle"``, ``"slider"``, ``"stepper"``
    (−/+ buttons sending relative steps), ``"file"``.
    """

    type: str
    min: float | None = None
    max: float | None = None
    unit: str = ""
    default: Any = None
    choices: tuple = ()
    label: str = ""
    widget: str = ""

    def __post_init__(self) -> None:
        if self.type not in PROP_TYPES:
            raise ValidationError(f"PropSpec.type must be one of {PROP_TYPES}, got {self.type!r}")

    def to_dict(self) -> dict:
        """JSON form used in ``describe()`` / the ``hello`` message."""
        return {"type": self.type, "min": self.min, "max": self.max, "unit": self.unit,
                "default": self.default, "choices": list(self.choices), "label": self.label,
                "widget": self.widget}

    def coerce(self, value: Any, name: str = "value") -> Any:
        """Validate and convert ``value`` to this property's type (ValidationError otherwise)."""
        t = self.type
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
                return value.strip().lower() in _TRUE
            raise ValidationError(f"{name} expects true/false, got {value!r}")
        if t in ("float", "int"):
            if isinstance(value, bool):
                raise ValidationError(f"{name} expects a number, got {value!r}")
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValidationError(f"{name} expects a number, got {value!r}") from None
            if not math.isfinite(num):
                raise ValidationError(f"{name} must be finite, got {value!r}")
            if t == "int":
                if not num.is_integer():
                    raise ValidationError(f"{name} expects an integer, got {value!r}")
                num = int(num)
            lo, hi = self.min, self.max
            if (lo is not None and num < lo) or (hi is not None and num > hi):
                unit = f" {self.unit}" if self.unit else ""
                raise ValidationError(f"{name}={value!r} is outside {lo}…{hi}{unit}")
            return num
        if t == "enum":
            if str(value) not in [str(c) for c in self.choices]:
                raise ValidationError(f"{name} must be one of {list(self.choices)}, got {value!r}")
            return str(value)
        if not isinstance(value, str):
            raise ValidationError(f"{name} expects a string, got {value!r}")
        return value


class Device:
    """Base class of every twin device model (see module docstring)."""

    type: ClassVar[str] = ""
    label: ClassVar[str] = ""
    inputs: ClassVar[dict[str, PropSpec]] = {}
    outputs: ClassVar[dict[str, PropSpec]] = {}
    pin_roles: ClassVar[tuple[str, ...]] = ()          # required pin roles
    optional_pins: ClassVar[tuple[str, ...]] = ()
    pin_aliases: ClassVar[dict[str, str]] = {}         # lower-case alias → role
    bus_kinds: ClassVar[tuple[str, ...]] = ()          # supported bus kinds ("i2c", "spi")
    default_address: ClassVar[int | None] = None
    defaults: ClassVar[dict[str, Any]] = {}            # default params
    example: ClassVar[dict[str, Any]] = {}             # {"pins": …, "bus": …, "params": …}
    is_display: ClassVar[bool] = False

    def __init__(self, cfg: DeviceConfig, pi: "VirtualPi") -> None:
        self.cfg = cfg
        self.id = cfg.id
        self.pi = pi
        self.params: dict[str, Any] = {**self.defaults, **cfg.params}
        self.pins: dict[str, int] = self._resolve_pins(cfg.pins)
        self.bus: dict | None = self._resolve_bus(cfg.bus)
        self._inputs: dict[str, Any] = {k: spec.default for k, spec in self.inputs.items()}
        self.display_version = 0
        self._listeners: list[tuple[int, Callable]] = []
        self.setup()

    # -- wiring ---------------------------------------------------------------------------------
    @classmethod
    def normalize_role(cls, role: str) -> str:
        """Map an alias (``TRIG``, ``DT``, ``r`` …) to the canonical role name."""
        r = "".join(ch for ch in str(role).lower() if ch.isalnum() or ch == "_")
        return cls.pin_aliases.get(r, r)

    def _resolve_pins(self, pins: dict[str, int]) -> dict[str, int]:
        valid = self.pin_roles + self.optional_pins
        out: dict[str, int] = {}
        for role, bcm in pins.items():
            r = self.normalize_role(role)
            if r not in valid:
                raise ValidationError(f"{self.id} ({self.type}): unknown pin role {role!r}; "
                                      f"valid roles: {', '.join(valid) or 'none (bus device)'}")
            if not 0 <= bcm <= 27:
                raise ValidationError(f"{self.id}: pin {role}=GPIO{bcm} does not exist (BCM 0–27)")
            out[r] = bcm
        if self.uses_pins(out):
            missing = [r for r in self.pin_roles if r not in out]
            if missing:
                raise ValidationError(f"{self.id} ({self.type}) needs pin role(s) {', '.join(missing)}; "
                                      f"got {sorted(out)}")
        return out

    def uses_pins(self, pins: dict[str, int]) -> bool:
        """Whether this instance is wired by pins (vs. only a bus). Override for dual-mode devices."""
        return bool(self.pin_roles)

    def _resolve_bus(self, bus: dict | None) -> dict | None:
        if not self.bus_kinds:
            if bus is not None:
                raise ValidationError(f"{self.id} ({self.type}) does not attach to a bus")
            return None
        if bus is None:
            if not self.needs_bus():
                return None
            kind = self.bus_kinds[0]
            bus = ({"kind": "i2c", "bus": 1, "address": self.default_address} if kind == "i2c"
                   else {"kind": "spi", "bus": 0, "cs": 0})
        bus = dict(bus)
        if bus["kind"] not in self.bus_kinds:
            raise ValidationError(f"{self.id} ({self.type}) supports bus {self.bus_kinds}, got {bus['kind']!r}")
        if bus["kind"] == "i2c":
            bus.setdefault("bus", 1)
            if bus.get("address") is None:
                bus["address"] = self.default_address
        else:
            bus.setdefault("bus", 0)
            bus.setdefault("cs", 0)
        return bus

    def needs_bus(self) -> bool:
        """True when the device must be attached to a bus (default: whenever it supports one)."""
        return bool(self.bus_kinds) and not self.pins

    def listen(self, bcm: int, cb: Callable[[int, int, float], None]) -> None:
        """Register a VirtualPi listener that :meth:`close` removes again."""
        self.pi.add_listener(bcm, cb)
        self._listeners.append((bcm, cb))

    # -- lifecycle hooks --------------------------------------------------------------------------
    def setup(self) -> None:
        """Attach to buses, register listeners, drive initial levels (override)."""

    def close(self) -> None:
        """Detach listeners and release driven lines."""
        for bcm, cb in self._listeners:
            self.pi.remove_listener(bcm, cb)
        self._listeners.clear()
        for bcm in set(self.pins.values()):
            self.pi.drive(bcm, self.id, None)

    def on_input(self, prop: str, value: Any) -> None:
        """React to a new input value (override)."""

    def outputs_state(self) -> dict:
        """Current output values (override)."""
        return {}

    def tick(self, t: float, dt: float) -> None:
        """Continuous dynamics; called ≥ 100 Hz by the twin's sim thread (override)."""

    def link(self, devices: dict[str, "Device"]) -> None:
        """Resolve references to other devices (override). Called once every device of the twin exists,
        e.g. a stepper fed by a shift register (``coil_source``) or a split-flap reading its stepper.
        Raise ``ValidationError`` for a reference that cannot be resolved."""

    def on_start(self) -> None:
        """The twin starts running (before the firmware is launched): start servers/threads (override).
        Never called for twins that are only built to validate a config."""

    def on_stop(self) -> None:
        """The twin stops: release whatever :meth:`on_start` acquired (override)."""

    def env(self) -> dict[str, str]:
        """Environment variables this device contributes to the firmware process (override).

        The runner calls it after :meth:`on_start` and before the firmware starts, and merges every
        device's dict into ``os.environ`` (e.g. ``ws_feed`` → ``MONEY_COUNTER_URL``)."""
        return {}

    # -- public API -------------------------------------------------------------------------------
    def set_input(self, prop: str, value: Any) -> None:
        """Set input ``prop`` (validated against its :class:`PropSpec`)."""
        spec = self.inputs.get(prop)
        if spec is None:
            raise NotFoundError(f"input of {self.id} ({self.type})", prop, self.inputs)
        v = spec.coerce(value, f"{self.id}.{prop}")
        with self.pi.lock:
            self._inputs[prop] = v
            self.on_input(prop, v)

    def get_input(self, prop: str) -> Any:
        """Current value of input ``prop``."""
        return self._inputs[prop]

    def state(self) -> dict:
        """Inputs and outputs as one JSON-able dict."""
        with self.pi.lock:
            return {**self._inputs, **self.outputs_state()}

    def describe(self) -> dict:
        """``{"id","type","label","inputs":{prop: spec},"outputs":{…},"pins","bus","display"}``."""
        return {"id": self.id, "type": self.type, "label": self.label or self.type,
                "inputs": {k: s.to_dict() for k, s in self.inputs.items()},
                "outputs": {k: s.to_dict() for k, s in self.outputs.items()},
                "pins": dict(self.pins), "bus": dict(self.bus) if self.bus else None,
                "display": self.is_display}

    def render_png(self) -> tuple[int, int, bytes]:
        """(width, height, PNG bytes) of a display device."""
        raise NotImplementedError(f"{self.type} is not a display")

    @classmethod
    def example_config(cls, dev_id: str) -> DeviceConfig:
        """A minimal valid :class:`DeviceConfig` for this type (docs, tests, CLI ``info devices``)."""
        ex = cls.example
        return DeviceConfig(dev_id, cls.type, pins=dict(ex.get("pins", {})), bus=ex.get("bus"),
                            params=dict(ex.get("params", {})))


DEVICE_TYPES: dict[str, type[Device]] = {}


def register(cls: type[Device]) -> type[Device]:
    """Class decorator adding a device model to :data:`DEVICE_TYPES`."""
    if not cls.type:
        raise ValidationError(f"{cls.__name__} has no type name")
    DEVICE_TYPES[cls.type] = cls
    return cls


def linked_device(owner: Device, devices: dict[str, Device], ref: Any, what: str,
                  types: tuple[str, ...] = ()) -> Device:
    """Resolve the device id ``ref`` named in ``owner``'s params (ValidationError with candidates)."""
    dev = devices.get(str(ref)) if ref is not None else None
    if dev is None:
        cands = [d.id for d in devices.values() if not types or d.type in types]
        raise ValidationError(f"{owner.id} ({owner.type}): {what} {ref!r} is not a device of this twin; "
                              f"candidates: {', '.join(cands) or 'none'}")
    if types and dev.type not in types:
        raise ValidationError(f"{owner.id} ({owner.type}): {what} {ref!r} is a {dev.type}, expected "
                              f"{' or '.join(types)}")
    return dev


def create_device(cfg: DeviceConfig, pi: "VirtualPi") -> Device:
    """Instantiate the model for ``cfg.type`` (NotFoundError lists close matches)."""
    cls = DEVICE_TYPES.get(cfg.type)
    if cls is None:
        raise NotFoundError("twin device type", cfg.type, DEVICE_TYPES)
    return cls(cfg, pi)


def hexcolor(r: float, g: float, b: float) -> str:
    """0..1 channel values → ``#rrggbb``."""
    def c(v: float) -> int:
        return max(0, min(255, int(round(v * 255))))
    return f"#{c(r):02x}{c(g):02x}{c(b):02x}"
