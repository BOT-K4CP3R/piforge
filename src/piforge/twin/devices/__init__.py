"""Twin device models and the :data:`DEVICE_TYPES` registry.

Type names are a binding contract with the electronics library (``PartDef.sim["twin"]``):
button, switch, limit_switch, ir_breakbeam, pir, hcsr04, led, rgb_led, buzzer, relay, servo,
stepper_28byj48, dc_motor, bme280, ssd1306, lcd1602_pcf8574, mcp3008, hx711, rotary_encoder,
neopixel, camera, ds18b20, dht22, shift_register_74hc595, splitflap, ws_feed.
"""

from __future__ import annotations

from piforge.twin.devices import (  # noqa: F401  (registration)
    adc, displays, io, logic, misc, motors, network, sensors, splitflap)
from piforge.twin.devices.base import DEVICE_TYPES, Device, PropSpec, create_device, register

__all__ = ["DEVICE_TYPES", "Device", "PropSpec", "create_device", "register"]
