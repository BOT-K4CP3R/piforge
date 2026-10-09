"""``w1thermsensor`` shim (1.x and 2.x APIs) reading twin ``ds18b20`` devices."""

from __future__ import annotations

from enum import Enum
from typing import Any

from piforge.twin.runtime import get_twin


class W1ThermSensorError(Exception):
    pass


class NoSensorFoundError(W1ThermSensorError):
    pass


class SensorNotReadyError(W1ThermSensorError):
    pass


class Unit(Enum):
    DEGREES_C = "celsius"
    DEGREES_F = "fahrenheit"
    KELVIN = "kelvin"


class Sensor(Enum):
    DS18S20 = 0x10
    DS1822 = 0x22
    DS18B20 = 0x28
    DS1825 = 0x3B
    DS28EA00 = 0x42
    MAX31850K = 0x3B


def _convert(celsius: float, unit: Any) -> float:
    name = getattr(unit, "value", unit)
    if name in ("fahrenheit", 0x02):
        return celsius * 9.0 / 5.0 + 32.0
    if name in ("kelvin", 0x03):
        return celsius + 273.15
    return celsius


def _devices() -> list[Any]:
    return [d for d in get_twin().devices.values() if d.type == "ds18b20"]


class W1ThermSensor:
    """A DS18B20 on the 1-Wire bus (``sensor_id`` without the ``28-`` prefix)."""

    DEGREES_C = Unit.DEGREES_C
    DEGREES_F = Unit.DEGREES_F
    KELVIN = Unit.KELVIN
    THERM_SENSOR_DS18B20 = Sensor.DS18B20

    def __init__(self, sensor_type: Any = None, sensor_id: str | None = None) -> None:
        devs = _devices()
        if sensor_id is not None:
            devs = [d for d in devs if d.rom.split("-", 1)[-1] == sensor_id or d.rom == sensor_id]
        if not devs:
            raise NoSensorFoundError("Could not find any sensor" + (f" with id {sensor_id}" if sensor_id else ""))
        self._dev = devs[0]
        self.id = self._dev.rom.split("-", 1)[-1]
        self.name = self._dev.rom
        self.type = Sensor.DS18B20

    @classmethod
    def get_available_sensors(cls, types: Any = None) -> list["W1ThermSensor"]:
        return [cls(sensor_id=d.rom.split("-", 1)[-1]) for d in _devices()]

    def get_temperature(self, unit: Any = Unit.DEGREES_C) -> float:
        return _convert(self._dev.millicelsius() / 1000.0, unit)

    def get_temperatures(self, units: list[Any]) -> list[float]:
        c = self._dev.millicelsius() / 1000.0
        return [_convert(c, u) for u in units]

    def get_resolution(self) -> int:
        return int(self._dev.params.get("resolution", 12))

    def set_resolution(self, resolution: int, persist: bool = False) -> None:
        if resolution not in (9, 10, 11, 12):
            raise ValueError("The given sensor resolution is not supported (9..12)")
        self._dev.params["resolution"] = int(resolution)

    @property
    def raw_sensor_strings(self) -> list[str]:
        return self._dev.w1_slave().splitlines()

    def __repr__(self) -> str:
        return f"W1ThermSensor(sensor_type={self.type!r}, sensor_id='{self.id}')"
