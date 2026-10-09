"""Twin configuration: which devices are wired to which Pi pins / buses.

A :class:`TwinConfig` is plain data and round-trips through JSON (it is handed to the runner
subprocess as a file). Pin numbers are BCM GPIO numbers; bus addresses are 7-bit I2C addresses.

Example::

    TwinConfig(devices=[
        DeviceConfig("SW1", "button", pins={"pin": 27}),
        DeviceConfig("US1", "hcsr04", pins={"trigger": 23, "echo": 24}),
        DeviceConfig("ENV1", "bme280", bus={"kind": "i2c", "bus": 1, "address": 0x76}),
        DeviceConfig("ADC1", "mcp3008", bus={"kind": "spi", "bus": 0, "cs": 0}),
    ], pulls={22: "up"})
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from piforge.core.errors import ValidationError

BOARDS = ("rpi5", "rpi4b", "rpi3bp", "rpizero2w")
_PULLS = {"up", "down"}


def _as_int(value: Any, what: str) -> int:
    """Accept ints and int-like strings (``"0x76"``, ``"17"``)."""
    if isinstance(value, bool):
        raise ValidationError(f"{what} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            pass
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValidationError(f"{what} must be an integer, got {value!r}")


@dataclass
class DeviceConfig:
    """One simulated device.

    ``pins`` maps a device role to a BCM pin, e.g. ``{"pin": 17}``, ``{"trigger": 23, "echo": 24}``,
    ``{"in1": 5, "in2": 6, "in3": 13, "in4": 19}``. ``bus`` describes a bus attachment:
    ``{"kind": "i2c", "bus": 1, "address": 0x76}`` or ``{"kind": "spi", "bus": 0, "cs": 0}``
    (optionally ``"cs_pin": <BCM>`` when chip-select is a plain GPIO). ``params`` are
    device-specific (see each device class' ``params_doc``).
    """

    id: str
    type: str
    pins: dict[str, int] = field(default_factory=dict)
    bus: dict | None = None
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValidationError(f"DeviceConfig.id must be a non-empty string, got {self.id!r}")
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValidationError(f"DeviceConfig.type must be a non-empty string (device {self.id})")
        self.pins = {str(k): _as_int(v, f"{self.id}.pins[{k!r}]") for k, v in dict(self.pins or {}).items()}
        if self.bus is not None:
            bus = dict(self.bus)
            kind = str(bus.get("kind", "")).lower()
            if kind not in ("i2c", "spi"):
                raise ValidationError(f"{self.id}: bus kind must be 'i2c' or 'spi', got {bus.get('kind')!r}")
            bus["kind"] = kind
            for key in ("bus", "address", "cs", "cs_pin"):
                if key in bus and bus[key] is not None:
                    bus[key] = _as_int(bus[key], f"{self.id}.bus[{key!r}]")
            self.bus = bus
        self.params = dict(self.params or {})

    def to_dict(self) -> dict:
        """Plain-JSON form."""
        d: dict = {"id": self.id, "type": self.type, "pins": dict(self.pins), "params": dict(self.params)}
        if self.bus is not None:
            d["bus"] = dict(self.bus)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceConfig":
        """Inverse of :meth:`to_dict` (accepts hex strings for addresses)."""
        if "id" not in d or "type" not in d:
            raise ValidationError(f"device entry needs 'id' and 'type': {d!r}")
        return cls(id=d["id"], type=d["type"], pins=d.get("pins") or {}, bus=d.get("bus"),
                   params=d.get("params") or {})


@dataclass
class TwinConfig:
    """The whole virtual bench: board model, devices and external pull resistors.

    ``pulls`` lists *physical* pull resistors on GPIO lines (BCM → ``"up"``/``"down"``); they are
    always present, unlike the Pi's internal pulls which the firmware configures.
    """

    board: str = "rpi4b"
    devices: list[DeviceConfig] = field(default_factory=list)
    pulls: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.board not in BOARDS:
            from piforge.core.errors import NotFoundError

            raise NotFoundError("board", self.board, BOARDS)
        self.devices = [d if isinstance(d, DeviceConfig) else DeviceConfig.from_dict(d) for d in self.devices]
        pulls: dict[int, str] = {}
        for k, v in dict(self.pulls or {}).items():
            bcm = _as_int(k, "pulls key")
            if str(v).lower() not in _PULLS:
                raise ValidationError(f"pull for GPIO{bcm} must be 'up' or 'down', got {v!r}")
            pulls[bcm] = str(v).lower()
        self.pulls = pulls
        seen: set[str] = set()
        for d in self.devices:
            if d.id in seen:
                raise ValidationError(f"duplicate device id {d.id!r} in twin config")
            seen.add(d.id)

    def device(self, dev_id: str) -> DeviceConfig:
        """Return the device config with ``dev_id`` (NotFoundError with suggestions otherwise)."""
        for d in self.devices:
            if d.id == dev_id:
                return d
        from piforge.core.errors import NotFoundError

        raise NotFoundError("twin device", dev_id, [d.id for d in self.devices])

    def to_dict(self) -> dict:
        """Plain-JSON form (pull keys become strings)."""
        return {"board": self.board, "devices": [d.to_dict() for d in self.devices],
                "pulls": {str(k): v for k, v in sorted(self.pulls.items())}}

    @classmethod
    def from_dict(cls, d: dict) -> "TwinConfig":
        """Inverse of :meth:`to_dict`."""
        return cls(board=d.get("board", "rpi4b"),
                   devices=[DeviceConfig.from_dict(x) for x in d.get("devices", [])],
                   pulls=d.get("pulls") or {})

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, s: str) -> "TwinConfig":
        """Parse a JSON string produced by :meth:`to_json` (or written by hand)."""
        try:
            data = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"twin config is not valid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ValidationError("twin config JSON must be an object")
        return cls.from_dict(data)
