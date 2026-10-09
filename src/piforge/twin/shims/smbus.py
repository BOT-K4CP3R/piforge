"""Legacy ``smbus`` (python-smbus / i2c-tools) shim: same calls as smbus2, backed by the twin."""

from __future__ import annotations

from typing import Any

from smbus2 import SMBus as _SMBus2


class SMBus(_SMBus2):
    """``smbus.SMBus(bus)``; ``read_i2c_block_data`` defaults to 32 bytes like python-smbus."""

    def __init__(self, bus: Any = None) -> None:
        super().__init__(None if bus in (None, -1) else bus)

    def read_i2c_block_data(self, addr: int, cmd: int, len: int = 32, force: Any = None) -> list[int]:  # noqa: A002
        return super().read_i2c_block_data(addr, cmd, len)
